from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
import json

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import CompanyBase
from app.db.company import SQLiteImmediateLockError, begin_sqlite_immediate
from app.db.errors import DataRecoveryRequiredError
from app.db.migrations import allocation_baseline_integrity_hash, run_company_migrations
from app.models import company as _company_models  # noqa: F401
from app.models import outgoing as _outgoing_models  # noqa: F401
from app.models.company import (
    AllocationHistoryBaselineFinalization,
    BankTxnDirection,
    BankTransaction,
)
from app.services import allocation_history
from app.services import bank_accounts, bank_import, invoice_payments
from app.services.allocation_history import (
    AllocationHistoryCaptureError,
    AllocationHistoryConflict,
    capture_legacy_allocation_baseline,
    translate_allocation_history_db_error,
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


def _insert_correction_history(connection, *, transaction_id, allocation_id, invoice_id):
    source_kind = "bill" if invoice_id == 1 else "invoice"
    snapshot_id = connection.execute(
        text(
            "INSERT INTO allocation_history_snapshots "
            "(source_kind, invoice_id, gross_cents, gst_cents, created_at) "
            "VALUES (:source_kind, :invoice_id, 1000, 100, '2026-10-02 09:00:00') "
            "RETURNING id"
        ),
        {"source_kind": source_kind, "invoice_id": invoice_id},
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO allocation_history_tax_components "
            "(snapshot_id, tax_code, gross_cents, gst_cents) "
            "VALUES (:snapshot_id, 'standard', 1000, 100)"
        ),
        {"snapshot_id": snapshot_id},
    )
    connection.execute(
        text(
            "INSERT INTO allocation_history_finalizations "
            "(snapshot_id, component_count, gross_cents, gst_cents, finalized_at) "
            "VALUES (:snapshot_id, 1, 1000, 100, '2026-10-02 09:01:00')"
        ),
        {"snapshot_id": snapshot_id},
    )
    connection.execute(
        text(
            "INSERT INTO allocation_history_events "
            "(operation, bank_transaction_id, allocation_id, snapshot_id, version, "
            "effective_date, recorded_at, idempotency_key, expected_prior_version, "
            "actor_placeholder, reason, evidence_reference, linked_allocation_id) "
            "VALUES ('allocation_correction', :transaction_id, :allocation_id, "
            ":snapshot_id, 1, '2026-10-02', '2026-10-02 09:02:00', "
            "'synthetic-backfill-correction', 0, 'Synthetic operator', "
            "'Synthetic prior correction', 'synthetic://allocation-evidence', "
            ":allocation_id)"
        ),
        {
            "transaction_id": transaction_id,
            "allocation_id": allocation_id,
            "snapshot_id": snapshot_id,
        },
    )


def _backfill_database(path, *, history_kind=None):
    engine = _database(path)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO invoice_lines "
            "(id, invoice_id, description, quantity, unit_price, gst_rate, "
            "line_subtotal, line_gst, line_total, tax_code) VALUES "
            "(1, 1, 'Synthetic AP line', 1, 10, 0.1, 10, 1, 11, 'standard'), "
            "(2, 2, 'Synthetic AR line', 1, 10, 0.1, 10, 1, 11, 'standard')"
        ))
        connection.execute(text(
            "UPDATE invoices SET status='authorised', paid_amount=0"
        ))
    if history_kind == "baseline":
        with Session(engine) as session:
            capture_legacy_allocation_baseline(
                session, 2, datetime(2026, 10, 12)
            )
            session.commit()
    elif history_kind == "correction":
        with engine.begin() as connection:
            _insert_correction_history(
                connection, transaction_id=2, allocation_id=22, invoice_id=2
            )
    with engine.begin() as connection:
        if history_kind is None:
            connection.execute(text("DELETE FROM invoice_payment_tax_components"))
        else:
            connection.execute(text(
                "DELETE FROM invoice_payment_tax_components WHERE allocation_id=11"
            ))
        connection.execute(text(
            "UPDATE bank_transactions SET gst_amount=0, tax_code='none'"
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


@pytest.mark.parametrize("history_kind", ["baseline", "event"])
@pytest.mark.parametrize("specs", [[], [{"invoice_id": 1, "amount": "1.00"}]])
def test_history_blocks_allocation_clear_and_replacement_without_state_change(
    tmp_path, history_kind, specs
):
    engine = _database(tmp_path / f"guard-{history_kind}-{len(specs)}.db")
    with Session(engine) as session:
        if history_kind == "baseline":
            capture_legacy_allocation_baseline(
                session, 1, datetime(2026, 10, 12)
            )
            session.commit()
            transaction_id = 1
        else:
            session.execute(text(
                "INSERT INTO allocation_history_events "
                "(operation, bank_transaction_id) "
                "VALUES ('legacy_history_unknown', 2)"
            ))
            session.commit()
            transaction_id = 2
    before = _snapshot(engine)

    with Session(engine) as session:
        begin_sqlite_immediate(session)
        transaction = session.get(BankTransaction, transaction_id)
        with pytest.raises(AllocationHistoryConflict) as caught:
            invoice_payments.replace_transaction_allocations(
                session, transaction, specs
            )
        assert caught.value.http_status == 409
        assert "SQLite" not in str(caught.value)
        assert "trigger" not in str(caught.value).lower()

    assert _snapshot(engine) == before
    engine.dispose()


@pytest.mark.parametrize("history_kind", ["baseline", "event"])
def test_historical_none_allocation_request_is_read_only(
    tmp_path, history_kind
):
    engine = _database(tmp_path / f"readonly-{history_kind}.db")
    with Session(engine) as session:
        if history_kind == "baseline":
            capture_legacy_allocation_baseline(
                session, 1, datetime(2026, 10, 12)
            )
            session.commit()
            transaction_id = 1
        else:
            session.execute(text(
                "INSERT INTO allocation_history_events "
                "(operation, bank_transaction_id) "
                "VALUES ('legacy_history_unknown', 2)"
            ))
            session.commit()
            transaction_id = 2
    before = _snapshot(engine)

    with Session(engine) as session:
        begin_sqlite_immediate(session)
        transaction = session.get(BankTransaction, transaction_id)
        invoice_payments.replace_transaction_allocations(
            session, transaction, None
        )
        session.commit()

    assert _snapshot(engine) == before
    engine.dispose()


def test_only_recognized_history_trigger_errors_translate(tmp_path):
    engine = _database(tmp_path / "history-error-translation.db")
    recognized = IntegrityError(
        "DELETE FROM bank_transactions",
        {},
        sqlite3.IntegrityError("Bank transaction has allocation history"),
    )
    with Session(engine) as session:
        with pytest.raises(AllocationHistoryConflict):
            translate_allocation_history_db_error(session, recognized)

    for message in ("database is locked", "UNIQUE constraint failed: invoices.id"):
        unrelated = IntegrityError(
            "INSERT INTO invoices",
            {},
            sqlite3.IntegrityError(message),
        )
        with Session(engine) as session:
            assert translate_allocation_history_db_error(session, unrelated) is None
    engine.dispose()


def test_backfill_rebuilds_missing_components_without_history(tmp_path):
    engine = _backfill_database(tmp_path / "backfill-no-history.db")
    before = _snapshot(engine)
    with Session(engine) as session:
        assert invoice_payments.backfill_missing_tax_components(session) == 2
        assert session.in_transaction()
        with Session(engine) as observer:
            assert _snapshot(engine) == before
        session.commit()

    with Session(engine) as session:
        allocations = session.execute(text(
            "SELECT bank_transaction_id, CAST(gst_amount AS TEXT) "
            "FROM invoice_payment_allocations "
            "ORDER BY bank_transaction_id"
        )).all()
        assert allocations == [(1, "0.91"), (2, "0.91")]
        components = session.execute(text(
            "SELECT allocation_id, tax_code, CAST(gross_amount AS TEXT), "
            "CAST(gst_amount AS TEXT) "
            "FROM invoice_payment_tax_components ORDER BY allocation_id"
        )).all()
        assert components == [
            (11, "standard", "10", "0.91"),
            (22, "standard", "10", "0.91"),
        ]
        transactions = session.execute(text(
            "SELECT CAST(gst_amount AS TEXT), tax_code "
            "FROM bank_transactions ORDER BY id"
        )).all()
        assert transactions == [
            ("0.91", "standard"),
            ("0.91", "standard"),
        ]
        invoices = session.execute(text(
            "SELECT status, CAST(paid_amount AS TEXT) FROM invoices ORDER BY id"
        )).all()
        assert invoices == [
            ("partial", "10"),
            ("partial", "10"),
        ]
    engine.dispose()


@pytest.mark.parametrize("history_kind", ["baseline", "correction"])
def test_backfill_history_preflight_rejects_entire_mixed_set(
    tmp_path, history_kind
):
    engine = _backfill_database(
        tmp_path / f"backfill-history-{history_kind}.db",
        history_kind=history_kind,
    )
    before = _snapshot(engine)
    with Session(engine) as session:
        with pytest.raises(
            invoice_payments.PaymentAllocationError,
            match="backfill is blocked because allocation history exists",
        ) as caught:
            invoice_payments.backfill_missing_tax_components(session)
        assert "SQLite" not in str(caught.value)
        assert "trigger" not in str(caught.value).lower()
        assert not session.in_transaction()

    assert _snapshot(engine) == before
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_history_events"
        )).scalar_one() == 1
    engine.dispose()


def test_startup_wraps_history_backfill_rejection_as_recovery_error(
    tmp_path, monkeypatch
):
    engine = _backfill_database(
        tmp_path / "startup-backfill-history.db", history_kind="baseline"
    )
    before = _snapshot(engine)
    from app.db import company as company_db
    from app.db import migrations as migration_module
    from app.db import schema_sync
    from app.services import chart_of_accounts

    monkeypatch.setattr(
        company_db, "get_company_engine", lambda *_args, **_kwargs: engine
    )
    monkeypatch.setattr(schema_sync, "sync_missing_columns", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(migration_module, "run_company_migrations", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(schema_sync, "detect_drift", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(schema_sync, "require_clean_schema", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chart_of_accounts, "seed_default_coa", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(migration_module, "reconcile_system_accounts", lambda *_args, **_kwargs: [])

    with pytest.raises(DataRecoveryRequiredError) as caught:
        company_db.init_company_db("synthetic-backfill")
    assert str(caught.value) == (
        "Legacy invoice payment allocations could not be reconciled safely: "
        "Legacy tax-component backfill is blocked because allocation history exists. "
        "Restore the pre-migration backup or review the recorded payment "
        "reconciliation events before retrying."
    )
    assert "SQLite" not in str(caught.value)
    assert _snapshot(engine) == before
    engine.dispose()


def test_direct_mutation_services_lock_before_relevant_reads(tmp_path):
    engine = _database(tmp_path / "service-lock-order.db")
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", record_statement)

    def assert_first_statement_is_lock(action):
        statements.clear()
        action()
        assert statements[0] == "BEGIN IMMEDIATE", statements

    with Session(engine) as session:
        assert_first_statement_is_lock(
            lambda: bank_accounts.record_manual_transaction(
                session,
                idempotency_key="direct-lock-manual",
                payload_hash="payload",
                bank_account_id=1,
                direction=BankTxnDirection.IN,
                amount=Decimal("1.00"),
                occurred_at=date(2026, 10, 1),
                tax_code="none",
                gst_registered=True,
            )
        )

    with Session(engine) as session:
        assert_first_statement_is_lock(
            lambda: bank_import.commit_import(
                session, bank_account_id=1, rows=[], gst_registered=True
            )
        )

    with Session(engine) as session:
        def missing_recategorisation():
            with pytest.raises(bank_accounts.BankAccountNotFound):
                bank_accounts.recategorise_transaction(
                    session, txn_id=999, gst_registered=True
                )

        assert_first_statement_is_lock(missing_recategorisation)

    with Session(engine) as session:
        def missing_deletion():
            with pytest.raises(bank_accounts.BankAccountNotFound):
                bank_accounts.delete_manual_transaction(session, txn_id=999)

        assert_first_statement_is_lock(missing_deletion)

    with Session(engine) as session:
        transaction = BankTransaction(
            id=1,
            bank_account_id=1,
            direction=BankTxnDirection.OUT,
            amount=Decimal("10.00"),
            occurred_at=date(2026, 10, 1),
            invoice_allocations=[],
        )
        statements.clear()
        invoice_payments.replace_transaction_allocations(session, transaction, [])
        assert statements[0] == "BEGIN IMMEDIATE", statements
        session.rollback()

    event.remove(engine, "before_cursor_execute", record_statement)
    engine.dispose()


def test_immediate_lock_ownership_tracks_root_commit_and_rollback(tmp_path):
    engine = _database(tmp_path / "lock-ownership-lifecycle.db")
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", record_statement)
    with Session(engine) as session:
        begin_sqlite_immediate(session)
        assert statements.count("BEGIN IMMEDIATE") == 1

        bank_import.commit_import(
            session, bank_account_id=1, rows=[], gst_registered=True
        )
        assert statements.count("BEGIN IMMEDIATE") == 1
        assert session.in_transaction()

        session.commit()
        assert "sqlite_immediate_lock_owner" not in session.info
        session.execute(text("SELECT 1"))
        with pytest.raises(SQLiteImmediateLockError):
            begin_sqlite_immediate(session)
        assert statements.count("BEGIN IMMEDIATE") == 1
        session.rollback()

        begin_sqlite_immediate(session)
        assert statements.count("BEGIN IMMEDIATE") == 2
        session.rollback()
        assert "sqlite_immediate_lock_owner" not in session.info

        session.execute(text("SELECT 1"))
        with pytest.raises(SQLiteImmediateLockError):
            begin_sqlite_immediate(session)
        assert statements.count("BEGIN IMMEDIATE") == 2
        session.rollback()

        begin_sqlite_immediate(session)
        assert statements.count("BEGIN IMMEDIATE") == 3
        session.rollback()

    class _NonSqliteDialect:
        name = "postgresql"

    class _NonSqliteBind:
        dialect = _NonSqliteDialect()

    class _NonSqliteSession:
        info = {}

        def get_bind(self):
            return _NonSqliteBind()

    non_sqlite_session = _NonSqliteSession()
    begin_sqlite_immediate(non_sqlite_session)
    assert non_sqlite_session.info == {}
    event.remove(engine, "before_cursor_execute", record_statement)
    engine.dispose()


def test_direct_service_fails_closed_after_autobegun_read(tmp_path):
    engine = _database(tmp_path / "read-before-lock.db")
    before = _snapshot(engine)
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", record_statement)
    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        assert statements == ["SELECT 1"]
        with pytest.raises(SQLiteImmediateLockError):
            bank_accounts.recategorise_transaction(
                session,
                txn_id=1,
                tax_code="none",
                gst_amount=Decimal("0.00"),
                gst_registered=True,
            )
        assert statements == ["SELECT 1"]
        session.commit()

    assert _snapshot(engine) == before
    event.remove(engine, "before_cursor_execute", record_statement)
    engine.dispose()


def test_direct_mutation_services_leave_commit_to_caller(tmp_path):
    engine = _database(tmp_path / "caller-owned-transaction.db")
    from app.services.bank_accounts import record_manual_transaction

    with Session(engine) as session:
        record_manual_transaction(
            session,
            idempotency_key="caller-owned-manual",
            payload_hash="caller-owned-payload",
            bank_account_id=1,
            direction=BankTxnDirection.IN,
            amount=Decimal("1.00"),
            occurred_at=date(2026, 10, 2),
            tax_code="none",
            gst_registered=True,
        )
        assert session.in_transaction()
        with Session(engine) as observer:
            assert observer.query(BankTransaction).count() == 2
        session.commit()
    with Session(engine) as observer:
        assert observer.query(BankTransaction).count() == 3

    before = _snapshot(engine)
    with Session(engine) as session:
        bank_accounts.recategorise_transaction(
            session,
            txn_id=1,
            account_id=None,
            tax_code="none",
            gst_amount=Decimal("0.00"),
            gst_registered=True,
        )
        assert session.in_transaction()
        with Session(engine) as observer:
            assert _snapshot(engine) == before
        session.rollback()
    assert _snapshot(engine) == before

    with Session(engine) as session:
        bank_accounts.delete_manual_transaction(session, txn_id=2)
        assert session.in_transaction()
        with Session(engine) as observer:
            assert _snapshot(engine) == before
        session.rollback()
    assert _snapshot(engine) == before

    import_row = {
        "occurred_at": date(2026, 10, 2),
        "direction": "in",
        "amount": Decimal("2.00"),
        "dedup_key": "caller-owned-import",
        "account_id": None,
        "tax_code": "none",
        "gst_amount": Decimal("0.00"),
        "memo": "Caller-owned import",
        "counter_party_name": None,
        "invoice_allocations": [],
        "unapplied_account_id": None,
    }
    with Session(engine) as session:
        assert bank_import.commit_import(
            session,
            bank_account_id=1,
            rows=[import_row],
            gst_registered=True,
        ) == {"created": 1, "skipped_duplicates": 0}
        assert session.in_transaction()
        with Session(engine) as observer:
            assert _snapshot(engine) == before
        session.rollback()
    assert _snapshot(engine) == before
    engine.dispose()


def test_failed_direct_mutations_cannot_commit_partial_work(tmp_path):
    engine = _database(tmp_path / "failed-caller-owned-transaction.db")
    before = _snapshot(engine)
    from app.services.bank_accounts import InvoiceSettlementConflict, record_manual_transaction

    with Session(engine) as session:
        with pytest.raises(InvoiceSettlementConflict):
            record_manual_transaction(
                session,
                idempotency_key="failed-caller-owned-manual",
                payload_hash="failed-payload",
                bank_account_id=1,
                direction=BankTxnDirection.IN,
                amount=Decimal("1.00"),
                occurred_at=date(2026, 10, 2),
                invoice_allocations=[{"invoice_id": 2, "amount": "1.00"}],
                tax_code="none",
                gst_registered=True,
            )
        assert not session.in_transaction()
        session.commit()
    assert _snapshot(engine) == before

    with Session(engine) as session:
        with pytest.raises(bank_accounts.BankTxnError):
            bank_accounts.recategorise_transaction(
                session,
                txn_id=1,
                tax_code="gst_free",
                gst_registered=True,
            )
        assert not session.in_transaction()
        session.commit()
    assert _snapshot(engine) == before

    import_rows = [
        {
            "occurred_at": date(2026, 10, 2),
            "direction": "in",
            "amount": Decimal("2.00"),
            "dedup_key": "partial-import-first",
            "account_id": None,
            "tax_code": "none",
            "gst_amount": Decimal("0.00"),
            "memo": "First import row",
            "counter_party_name": None,
            "invoice_allocations": [],
            "unapplied_account_id": None,
        },
        {
            "occurred_at": date(2026, 10, 3),
            "direction": "out",
            "amount": Decimal("3.00"),
            "dedup_key": "partial-import-second",
            "account_id": 999,
            "tax_code": "none",
            "gst_amount": Decimal("0.00"),
            "memo": "Invalid second import row",
            "counter_party_name": None,
            "invoice_allocations": [],
            "unapplied_account_id": None,
        },
    ]
    with Session(engine) as session:
        with pytest.raises(ValueError, match="account_id does not reference"):
            bank_import.commit_import(
                session,
                bank_account_id=1,
                rows=import_rows,
                gst_registered=True,
            )
        assert not session.in_transaction()
        session.commit()
    assert _snapshot(engine) == before

    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO allocation_history_events "
            "(operation, bank_transaction_id) "
            "VALUES ('legacy_history_unknown', 2)"
        ))
    delete_before = _snapshot(engine)
    with Session(engine) as session:
        with pytest.raises(AllocationHistoryConflict):
            bank_accounts.delete_manual_transaction(session, txn_id=2)
        assert not session.in_transaction()
        session.commit()
    assert _snapshot(engine) == delete_before
    engine.dispose()


def test_failed_allocation_replacement_with_history_cannot_commit_partial_work(tmp_path):
    engine = _database(tmp_path / "failed-history-replacement.db")
    with engine.begin() as connection:
        _insert_correction_history(
            connection, transaction_id=2, allocation_id=22, invoice_id=2
        )

    tables = (
        "bank_transactions",
        "invoice_payment_allocations",
        "invoice_payment_tax_components",
        "invoices",
        "allocation_history_snapshots",
        "allocation_history_tax_components",
        "allocation_history_finalizations",
        "allocation_history_events",
    )

    def persisted_state(connection):
        return {
            table: connection.execute(
                text(f"SELECT * FROM {table} ORDER BY rowid")
            ).all()
            for table in tables
        }

    with engine.connect() as connection:
        before = persisted_state(connection)

    with Session(engine) as session:
        begin_sqlite_immediate(session)
        transaction = session.get(BankTransaction, 2)
        assert transaction is not None
        with pytest.raises(AllocationHistoryConflict):
            invoice_payments.replace_transaction_allocations(
                session, transaction, []
            )
        assert not session.in_transaction()
        session.commit()

    with Session(engine) as fresh_session:
        assert persisted_state(fresh_session.connection()) == before
    engine.dispose()


def test_baseline_capture_and_direct_recategorisation_serialize(tmp_path, monkeypatch):
    engine = _database(tmp_path / "baseline-mutation-race.db")
    before = _snapshot(engine)
    baseline_locked = threading.Event()
    release_baseline = threading.Event()
    mutation_waiting = threading.Event()
    mutation_locked = threading.Event()
    original_begin = allocation_history.begin_sqlite_immediate

    def coordinated_begin(session):
        if session.info.get("guard_race_lock_seen"):
            original_begin(session)
            return
        label = session.info["guard_race_label"]
        if label == "mutation":
            mutation_waiting.set()
        original_begin(session)
        session.info["guard_race_lock_seen"] = True
        if label == "baseline":
            baseline_locked.set()
            if not release_baseline.wait(timeout=5):
                raise AssertionError("baseline writer was not released")
        else:
            mutation_locked.set()

    monkeypatch.setattr(
        allocation_history, "begin_sqlite_immediate", coordinated_begin
    )
    monkeypatch.setattr(bank_accounts, "begin_sqlite_immediate", coordinated_begin)

    def capture():
        with Session(engine) as session:
            session.info["guard_race_label"] = "baseline"
            result = capture_legacy_allocation_baseline(
                session, 1, datetime(2026, 10, 12)
            )
            session.commit()
            return result

    def mutate():
        with Session(engine) as session:
            session.info["guard_race_label"] = "mutation"
            try:
                bank_accounts.recategorise_transaction(
                    session,
                    txn_id=1,
                    tax_code="none",
                    gst_amount=Decimal("0.00"),
                    gst_registered=True,
                )
            except AllocationHistoryConflict:
                return "conflict"
            return "changed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        capture_future = pool.submit(capture)
        try:
            assert baseline_locked.wait(timeout=5)
            mutation_future = pool.submit(mutate)
            assert mutation_waiting.wait(timeout=5)
            assert not mutation_locked.wait(timeout=0.25)
        finally:
            release_baseline.set()
        capture_future.result(timeout=10)
        assert mutation_future.result(timeout=10) == "conflict"

    assert _snapshot(engine) == before
    assert mutation_locked.is_set()
    engine.dispose()


@pytest.mark.parametrize("mutation_kind", ["allocation_clear", "import_new", "delete"])
def test_baseline_capture_serializes_with_allocation_import_and_delete(
    tmp_path, monkeypatch, mutation_kind
):
    engine = _database(tmp_path / f"baseline-{mutation_kind}-race.db")
    before = _snapshot(engine)
    baseline_locked = threading.Event()
    release_baseline = threading.Event()
    mutation_waiting = threading.Event()
    mutation_locked = threading.Event()
    original_begin = allocation_history.begin_sqlite_immediate

    def coordinated_begin(session):
        if session.info.get("allocation_race_lock_seen"):
            original_begin(session)
            return
        label = session.info["allocation_race_label"]
        if label == "mutation":
            mutation_waiting.set()
        original_begin(session)
        session.info["allocation_race_lock_seen"] = True
        if label == "baseline":
            baseline_locked.set()
            if not release_baseline.wait(timeout=5):
                raise AssertionError("baseline writer was not released")
        else:
            mutation_locked.set()

    for module in (allocation_history, invoice_payments, bank_accounts, bank_import):
        monkeypatch.setattr(module, "begin_sqlite_immediate", coordinated_begin)

    def capture():
        with Session(engine) as session:
            session.info["allocation_race_label"] = "baseline"
            result = capture_legacy_allocation_baseline(
                session, 1, datetime(2026, 10, 12)
            )
            session.commit()
            return result.baseline_event_id

    def mutate():
        with Session(engine) as session:
            session.info["allocation_race_label"] = "mutation"
            try:
                if mutation_kind == "allocation_clear":
                    transaction = BankTransaction(
                        id=1,
                        bank_account_id=1,
                        direction=BankTxnDirection.OUT,
                        amount=Decimal("10.00"),
                        occurred_at=date(2026, 10, 1),
                        invoice_allocations=[],
                    )
                    invoice_payments.replace_transaction_allocations(
                        session, transaction, []
                    )
                    return "changed"
                if mutation_kind == "delete":
                    bank_accounts.delete_manual_transaction(session, txn_id=1)
                    return "deleted"
                result = bank_import.commit_import(
                    session,
                    bank_account_id=1,
                    rows=[{
                        "occurred_at": date(2026, 10, 2),
                        "direction": "in",
                        "amount": Decimal("3.00"),
                        "dedup_key": "baseline-race-new-import",
                        "account_id": None,
                        "tax_code": "none",
                        "gst_amount": Decimal("0.00"),
                        "memo": "new concurrent import",
                        "counter_party_name": None,
                        "invoice_allocations": [],
                        "unapplied_account_id": None,
                    }],
                    gst_registered=True,
                )
                session.commit()
                return result
            except AllocationHistoryConflict as exc:
                session.commit()
                message = str(exc)
                assert "SQLite" not in message
                assert "trigger" not in message.lower()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        capture_future = pool.submit(capture)
        mutation_future = None
        try:
            assert baseline_locked.wait(timeout=5)
            mutation_future = pool.submit(mutate)
            assert mutation_waiting.wait(timeout=5)
            assert not mutation_locked.wait(timeout=0.25)
        finally:
            release_baseline.set()
        baseline_event_id = capture_future.result(timeout=10)
        assert mutation_future is not None
        mutation_result = mutation_future.result(timeout=10)

    assert baseline_event_id > 0
    after = _snapshot(engine)
    if mutation_kind == "import_new":
        assert mutation_result == {"created": 1, "skipped_duplicates": 0}
        assert len(after["bank_transactions"]) == 3
        assert after["bank_transactions"][:2] == before["bank_transactions"]
        for table in set(before) - {"bank_transactions"}:
            assert after[table] == before[table]
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT COUNT(*) FROM bank_transactions "
                "WHERE memo='new concurrent import'"
            )).scalar_one() == 1
    else:
        assert mutation_result == "conflict"
    assert mutation_locked.is_set()
    if mutation_kind != "import_new":
        assert after == before
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_history_events "
            "WHERE bank_transaction_id=1"
        )).scalar_one() == 1
        assert connection.execute(text(
            "SELECT COUNT(*) FROM bank_transactions"
        )).scalar_one() == (3 if mutation_kind == "import_new" else 2)
    engine.dispose()