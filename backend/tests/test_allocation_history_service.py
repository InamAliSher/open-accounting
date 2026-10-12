from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import json

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import CompanyBase
from app.db.migrations import allocation_baseline_integrity_hash, run_company_migrations
from app.models import company as _company_models  # noqa: F401
from app.models import outgoing as _outgoing_models  # noqa: F401
from app.models.company import AllocationHistoryBaselineFinalization
from app.services import allocation_history
from app.services.allocation_history import (
    AllocationHistoryCaptureError,
    capture_legacy_allocation_baseline,
)


def _database(path):
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    CompanyBase.metadata.create_all(engine)
    run_company_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO contacts (id, kind, name, active, created_at) "
            "VALUES (1, 'supplier', 'Synthetic contact', 1, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO invoices (id, direction, contact_id, invoice_number, issue_date, "
            "currency, subtotal, gst_amount, total, gst_inclusive, status, paid_amount, "
            "source, created_at, updated_at) VALUES "
            "(1, 'AP', 1, 'SYN-AP', '2026-09-01', 'AUD', 10, 1, 11, 1, 'draft', 10, "
            "'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
            "(2, 'AR', 1, 'SYN-AR', '2026-09-01', 'AUD', 10, 1, 11, 1, 'draft', 10, "
            "'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO bank_accounts (id, name, opening_balance, is_active) "
            "VALUES (1, 'Synthetic bank', 0, 1)"
        ))
        connection.execute(text(
            "INSERT INTO bank_transactions "
            "(id, bank_account_id, direction, amount, occurred_at, gst_amount, tax_code, "
            "unapplied_amount) VALUES "
            "(1, 1, 'out', 10, '2026-10-01', 1, 'standard', 0), "
            "(2, 1, 'in', 10, '2026-10-01', 1, 'standard', 0)"
        ))
        connection.execute(text(
            "INSERT INTO invoice_payment_allocations "
            "(id, bank_transaction_id, invoice_id, amount, gst_amount, created_at) VALUES "
            "(11, 1, 1, 10, 1, '2026-10-01 08:00:00'), "
            "(22, 2, 2, 10, 1, '2026-10-01 08:01:00')"
        ))
        connection.execute(text(
            "INSERT INTO invoice_payment_tax_components "
            "(id, allocation_id, tax_code, gross_amount, gst_amount) VALUES "
            "(111, 11, 'standard', 10, 1), (222, 22, 'standard', 10, 1)"
        ))
    return engine


def _snapshot(engine):
    tables = (
        "bank_transactions",
        "invoice_payment_allocations",
        "invoice_payment_tax_components",
        "invoices",
        "credit_notes",
        "credit_note_applications",
        "credit_note_refunds",
        "gst_adjustment_events",
        "journal_entries",
    )
    with engine.connect() as connection:
        return {
            table: connection.execute(text(f"SELECT * FROM {table} ORDER BY rowid")).all()
            for table in tables
        }


@pytest.mark.parametrize(
    ("transaction_id", "allocation_id", "component_id", "status_direction"),
    [(1, 11, 111, "AP"), (2, 22, 222, "AR")],
)
def test_captures_complete_ap_and_ar_baselines(
    tmp_path, transaction_id, allocation_id, component_id, status_direction
):
    engine = _database(tmp_path / f"{status_direction}.db")
    before = _snapshot(engine)
    observed_at = datetime(2026, 10, 12, 9, 30, 15, 123456)
    if status_direction == "AR":
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    with Session(engine) as session:
        result = capture_legacy_allocation_baseline(session, transaction_id, observed_at)
        assert result.status == "complete"
        assert result.incomplete_reasons == ()
        assert result.history_boundary_date == date(2026, 10, 1)
        assert result.observation_timestamp == observed_at
        assert result.allocation_count == 1
        assert result.component_count == 1
        assert result.gross_total_cents == 1000
        assert result.gst_total_cents == 100
        assert result.pre_baseline_history_complete is False

        binding = session.execute(text(
            "SELECT allocation_row_id, source_kind, invoice_or_bill_id, "
            "allocated_gross_cents, allocated_gross_source, allocated_gst_cents, "
            "allocated_gst_source, allocation_created_at "
            "FROM allocation_history_baseline_bindings WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        assert tuple(binding) == (
            allocation_id,
            "bill" if status_direction == "AP" else "invoice",
            1 if status_direction == "AP" else 2,
            1000,
            "10",
            100,
            "1",
            "2026-10-01 08:00:00" if status_direction == "AP" else "2026-10-01 08:01:00",
        )
        component = session.execute(text(
            "SELECT source_component_row_id, tax_code, component_gross_cents, "
            "component_gross_source, component_gst_cents, component_gst_source "
            "FROM allocation_history_baseline_tax_bindings tb "
            "JOIN allocation_history_baseline_bindings b ON b.id=tb.allocation_binding_id "
            "WHERE b.baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        assert tuple(component) == (component_id, "standard", 1000, "10", 100, "1")
        persisted_hash = session.execute(text(
            "SELECT integrity_hash FROM allocation_history_baseline_finalizations "
            "WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).scalar_one()
        assert persisted_hash == result.integrity_hash
        assert session.get(AllocationHistoryBaselineFinalization, result.baseline_event_id)
        session.rollback()
    assert _snapshot(engine) == before
    engine.dispose()


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("missing_components", ("missing_tax_components",)),
        ("unsupported_tax", ("unsupported_tax_evidence",)),
        ("subcent", ("invalid_amount", "unreconciled_totals")),
        ("invalid", ("invalid_amount", "unreconciled_totals")),
        ("unreconciled", ("unreconciled_totals",)),
        ("incompatible_invoice", ("insufficient_source_data",)),
        ("incompatible_transaction", ("insufficient_source_data",)),
        ("malformed_payment_date", ("insufficient_source_data",)),
    ],
)
def test_incomplete_legacy_evidence_is_captured_without_rounding(tmp_path, setup, expected):
    engine = _database(tmp_path / f"{setup}.db")
    with engine.begin() as connection:
        if setup == "missing_components":
            connection.execute(text(
                "DELETE FROM invoice_payment_tax_components WHERE allocation_id=11"
            ))
        elif setup == "unsupported_tax":
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.execute(text(
                "UPDATE invoice_payment_tax_components SET tax_code='unsupported' "
                "WHERE allocation_id=11"
            ))
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        elif setup == "subcent":
            connection.execute(text(
                "UPDATE invoice_payment_allocations SET amount=10.001 WHERE id=11"
            ))
        elif setup == "invalid":
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.execute(text(
                "UPDATE invoice_payment_allocations SET amount=-10 WHERE id=11"
            ))
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        elif setup == "unreconciled":
            connection.execute(text(
                "UPDATE invoice_payment_tax_components SET gross_amount=9 "
                "WHERE allocation_id=11"
            ))
        elif setup == "incompatible_invoice":
            connection.execute(text("UPDATE invoices SET direction='XX' WHERE id=1"))
        elif setup == "incompatible_transaction":
            connection.execute(text("UPDATE bank_transactions SET direction='OUT' WHERE id=1"))
        elif setup == "malformed_payment_date":
            connection.execute(text(
                "UPDATE bank_transactions SET occurred_at='not-a-date' WHERE id=1"
            ))

    with Session(engine) as session:
        result = capture_legacy_allocation_baseline(
            session, 1, datetime(2026, 10, 12, 9)
        )
        assert result.status == "incomplete"
        assert result.incomplete_reasons == expected
        if setup in {"incompatible_invoice", "incompatible_transaction"}:
            assert result.direction_evidence == (
                ("OUT" if setup == "incompatible_transaction" else "out", "XX" if setup == "incompatible_invoice" else "AP"),
            )
        stored_reasons = session.execute(text(
            "SELECT incomplete_reason_codes FROM allocation_history_baseline_finalizations "
            "WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).scalar_one()
        assert tuple(json.loads(stored_reasons)) == result.incomplete_reasons
        stored_hash = session.execute(text(
            "SELECT integrity_hash FROM allocation_history_baseline_finalizations "
            "WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).scalar_one()
        assert result.integrity_hash == stored_hash
        if setup == "malformed_payment_date":
            assert result.history_boundary_date is None
        allocation_source = session.execute(text(
            "SELECT allocated_gross_source, allocated_gross_cents "
            "FROM allocation_history_baseline_bindings WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        if setup == "subcent":
            assert allocation_source == ("10.001", None)
        elif setup == "invalid":
            assert allocation_source == ("-10", None)
        elif setup in {"incompatible_invoice", "incompatible_transaction"}:
            source_kind = session.execute(text(
                "SELECT source_kind FROM allocation_history_baseline_bindings "
                "WHERE baseline_event_id=:id"
            ), {"id": result.baseline_event_id}).scalar_one()
            if setup == "incompatible_invoice":
                assert source_kind is None
        session.rollback()
    engine.dispose()


_VALID_MONEY_CASES = (
    ("0.29", 29),
    ("0.57", 57),
    ("1", 100),
    ("1.2", 120),
    ("1.20", 120),
    ("999999.99", 99_999_999),
)


@pytest.mark.parametrize(("raw_amount", "expected_cents"), _VALID_MONEY_CASES)
def test_exact_cent_service_and_trigger_agreement(tmp_path, raw_amount, expected_cents):
    engine = _database(tmp_path / f"valid-{raw_amount.replace('.', '-')}.db")
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE invoice_payment_allocations "
            "SET amount=:amount, gst_amount=:amount WHERE id=11"
        ), {"amount": raw_amount})
        connection.execute(text(
            "UPDATE invoice_payment_tax_components "
            "SET gross_amount=:amount, gst_amount=:amount WHERE id=111"
        ), {"amount": raw_amount})
        source_values = connection.execute(text(
            "SELECT CAST(a.amount AS TEXT), CAST(a.gst_amount AS TEXT), "
            "CAST(c.gross_amount AS TEXT), CAST(c.gst_amount AS TEXT) "
            "FROM invoice_payment_allocations a "
            "JOIN invoice_payment_tax_components c ON c.allocation_id=a.id WHERE a.id=11"
        )).one()
        assert tuple(connection.execute(text(
            "SELECT allocation_baseline_cents(:gross), allocation_baseline_cents(:gst), "
            "allocation_baseline_cents(:component_gross), "
            "allocation_baseline_cents(:component_gst)"
        ), {
            "gross": source_values[0],
            "gst": source_values[1],
            "component_gross": source_values[2],
            "component_gst": source_values[3],
        }).one()) == (expected_cents,) * 4

    with Session(engine) as session:
        result = capture_legacy_allocation_baseline(
            session, 1, datetime(2026, 10, 12, 9)
        )
        assert result.status == "complete"
        assert result.incomplete_reasons == ()
        assert result.gross_total_cents == expected_cents
        assert result.gst_total_cents == expected_cents
        allocation = session.execute(text(
            "SELECT allocated_gross_cents, allocated_gross_source, "
            "allocated_gst_cents, allocated_gst_source "
            "FROM allocation_history_baseline_bindings WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        component = session.execute(text(
            "SELECT component_gross_cents, component_gross_source, "
            "component_gst_cents, component_gst_source "
            "FROM allocation_history_baseline_tax_bindings tb "
            "JOIN allocation_history_baseline_bindings b ON b.id=tb.allocation_binding_id "
            "WHERE b.baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        assert allocation == (expected_cents, source_values[0], expected_cents, source_values[1])
        assert component == (expected_cents, source_values[2], expected_cents, source_values[3])
        finalization = session.execute(text(
            "SELECT incomplete_reason_codes, integrity_hash "
            "FROM allocation_history_baseline_finalizations WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        assert json.loads(finalization.incomplete_reason_codes) == []
        assert finalization.integrity_hash == result.integrity_hash
        session.rollback()
    engine.dispose()


_INVALID_MONEY_CASES = (
    "0.001",
    "1.001",
    "1.005",
    "1.009",
    "10.999",
    "-0.01",
    "NaN",
    "Infinity",
    "-Infinity",
    "92233720368547758.08",
)


@pytest.mark.parametrize("raw_amount", _INVALID_MONEY_CASES)
def test_invalid_legacy_money_stays_raw_and_is_incomplete(tmp_path, raw_amount):
    engine = _database(tmp_path / f"invalid-{raw_amount.replace('.', '-')}.db")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(text(
            "UPDATE invoice_payment_allocations SET amount=:amount, gst_amount=0 WHERE id=11"
        ), {"amount": raw_amount})
        connection.execute(text(
            "UPDATE invoice_payment_tax_components "
            "SET gross_amount=:amount, gst_amount=0 WHERE id=111"
        ), {"amount": raw_amount})
        source_values = connection.execute(text(
            "SELECT CAST(a.amount AS TEXT), CAST(c.gross_amount AS TEXT) "
            "FROM invoice_payment_allocations a "
            "JOIN invoice_payment_tax_components c ON c.allocation_id=a.id WHERE a.id=11"
        )).one()
        assert connection.execute(text(
            "SELECT allocation_baseline_cents(:raw)"
        ), {"raw": source_values[0]}).scalar_one() is None
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")

    with Session(engine) as session:
        result = capture_legacy_allocation_baseline(
            session, 1, datetime(2026, 10, 12, 9)
        )
        assert result.status == "incomplete"
        assert result.incomplete_reasons == ("invalid_amount", "unreconciled_totals")
        allocation = session.execute(text(
            "SELECT allocated_gross_cents, allocated_gross_source "
            "FROM allocation_history_baseline_bindings WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        component = session.execute(text(
            "SELECT component_gross_cents, component_gross_source "
            "FROM allocation_history_baseline_tax_bindings tb "
            "JOIN allocation_history_baseline_bindings b ON b.id=tb.allocation_binding_id "
            "WHERE b.baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        assert allocation == (None, source_values[0])
        assert component == (None, source_values[1])
        finalization = session.execute(text(
            "SELECT incomplete_reason_codes, integrity_hash "
            "FROM allocation_history_baseline_finalizations WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        assert tuple(json.loads(finalization.incomplete_reason_codes)) == result.incomplete_reasons
        assert finalization.integrity_hash == result.integrity_hash
        session.rollback()
    engine.dispose()


def test_missing_transaction_and_zero_allocations_fail_without_artifacts(tmp_path):
    engine = _database(tmp_path / "missing-source.db")
    with Session(engine) as session:
        with pytest.raises(AllocationHistoryCaptureError, match="does not exist"):
            capture_legacy_allocation_baseline(session, 999, datetime(2026, 10, 12))
        session.rollback()

    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO bank_transactions "
            "(id, bank_account_id, direction, amount, occurred_at, gst_amount, tax_code, "
            "unapplied_amount) VALUES (3, 1, 'out', 1, '2026-10-02', 0, 'none', 0)"
        ))
    with Session(engine) as session:
        with pytest.raises(AllocationHistoryCaptureError, match="without allocations"):
            capture_legacy_allocation_baseline(session, 3, datetime(2026, 10, 12))
        assert session.execute(text(
            "SELECT COUNT(*) FROM allocation_history_events WHERE bank_transaction_id=3"
        )).scalar_one() == 0
        session.rollback()
    engine.dispose()


def test_duplicate_and_preexisting_history_fail_closed(tmp_path):
    engine = _database(tmp_path / "duplicate.db")
    with Session(engine) as session:
        capture_legacy_allocation_baseline(session, 1, datetime(2026, 10, 12))
        with pytest.raises(AllocationHistoryCaptureError, match="already has allocation history"):
            capture_legacy_allocation_baseline(session, 1, datetime(2026, 10, 12))
        session.rollback()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO allocation_history_events (operation, bank_transaction_id) "
            "VALUES ('legacy_history_unknown', 2)"
        ))
    with Session(engine) as session:
        with pytest.raises(AllocationHistoryCaptureError, match="already has allocation history"):
            capture_legacy_allocation_baseline(session, 2, datetime(2026, 10, 12))
        assert session.execute(text(
            "SELECT COUNT(*) FROM allocation_history_baseline_finalizations "
            "WHERE bank_transaction_id=2"
        )).scalar_one() == 0
        session.rollback()
    engine.dispose()


def test_source_mutation_aborts_capture_atomically(tmp_path):
    engine = _database(tmp_path / "mutation.db")
    with Session(engine) as session:
        mutated = False

        def mutate_before_finalization(current_session, _flush_context, _instances):
            nonlocal mutated
            if not mutated and any(
                isinstance(obj, AllocationHistoryBaselineFinalization)
                for obj in current_session.new
            ):
                current_session.connection().exec_driver_sql(
                    "UPDATE invoice_payment_allocations SET amount=9 WHERE id=11"
                )
                mutated = True

        event.listen(session, "before_flush", mutate_before_finalization)
        try:
            with pytest.raises(IntegrityError, match="Legacy allocation baseline"):
                capture_legacy_allocation_baseline(
                    session, 1, datetime(2026, 10, 12)
                )
        finally:
            event.remove(session, "before_flush", mutate_before_finalization)
        assert mutated
        assert session.execute(text(
            "SELECT COUNT(*) FROM allocation_history_events WHERE bank_transaction_id=1"
        )).scalar_one() == 0
        assert session.execute(text(
            "SELECT COUNT(*) FROM allocation_history_baseline_bindings"
        )).scalar_one() == 0
        assert session.execute(text(
            "SELECT COUNT(*) FROM allocation_history_baseline_tax_bindings"
        )).scalar_one() == 0
        assert session.execute(text(
            "SELECT COUNT(*) FROM allocation_history_baseline_finalizations"
        )).scalar_one() == 0
        assert session.execute(text(
            "SELECT amount FROM invoice_payment_allocations WHERE id=11"
        )).scalar_one() == 10
        session.rollback()
    engine.dispose()


def test_capture_does_not_commit_or_change_operational_rows(tmp_path):
    engine = _database(tmp_path / "preservation.db")
    before = _snapshot(engine)
    with Session(engine) as session:
        result = capture_legacy_allocation_baseline(
            session, 1, datetime(2026, 10, 12)
        )
        assert session.in_transaction()
        with Session(engine) as observer:
            assert observer.execute(text(
                "SELECT COUNT(*) FROM allocation_history_events "
                "WHERE id=:id"
            ), {"id": result.baseline_event_id}).scalar_one() == 0
        session.rollback()
    assert _snapshot(engine) == before
    engine.dispose()


def test_two_sessions_cannot_both_capture_one_transaction(tmp_path, monkeypatch):
    engine = _database(tmp_path / "race.db")
    first_acquired = threading.Event()
    first_release = threading.Event()
    second_reached_begin = threading.Event()
    second_acquired = threading.Event()
    session_ids = {}
    begin_calls = []
    original_begin = allocation_history.begin_sqlite_immediate

    def instrumented_begin(session):
        label = session.info["race_label"]
        if session.info.get("race_begin_seen"):
            return original_begin(session)
        begin_calls.append(label)
        if label == "second":
            second_reached_begin.set()
        original_begin(session)
        session.info["race_begin_seen"] = True
        if label == "first":
            first_acquired.set()
            if not first_release.wait(timeout=5):
                raise AssertionError("first session was not released from its immediate lock")
        else:
            second_acquired.set()

    monkeypatch.setattr(allocation_history, "begin_sqlite_immediate", instrumented_begin)

    def capture(label):
        with Session(engine) as session:
            session.info["race_label"] = label
            session_ids[label] = id(session)
            try:
                result = capture_legacy_allocation_baseline(
                    session, 1, datetime(2026, 10, 12)
                )
                session.commit()
                return ("success", result.baseline_event_id)
            except AllocationHistoryCaptureError:
                session.rollback()
                return ("duplicate", None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(capture, "first")
        second_future = None
        try:
            assert first_acquired.wait(timeout=5), "first session did not acquire BEGIN IMMEDIATE"
            second_future = pool.submit(capture, "second")
            assert second_reached_begin.wait(timeout=5), "second session did not reach BEGIN IMMEDIATE"
            assert not second_acquired.wait(timeout=0.25), (
                "second session acquired BEGIN IMMEDIATE while first still held it"
            )
        finally:
            first_release.set()
        first_outcome = first_future.result(timeout=10)
        assert second_future is not None, "second capture was not scheduled"
        outcomes = [first_outcome, second_future.result(timeout=10)]
    assert session_ids["first"] != session_ids["second"]
    assert set(begin_calls) == {"first", "second"}
    assert sum(outcome[0] == "success" for outcome in outcomes) == 1
    assert sum(outcome[0] == "duplicate" for outcome in outcomes) == 1
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_history_events "
            "WHERE bank_transaction_id=1"
        )).scalar_one() == 1
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_history_baseline_finalizations "
            "WHERE bank_transaction_id=1"
        )).scalar_one() == 1
    engine.dispose()


def test_unrelated_transaction_and_operational_values_are_preserved(tmp_path):
    engine = _database(tmp_path / "unrelated.db")
    before = _snapshot(engine)
    with Session(engine) as session:
        capture_legacy_allocation_baseline(session, 1, datetime(2026, 10, 12))
        assert _snapshot(engine) == before
        session.rollback()
    assert _snapshot(engine) == before
    engine.dispose()