from __future__ import annotations

import shutil
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROJECT_ROOT = ROOT.parent
HEAD = {"X-Company-Id": "tc"}


@pytest.fixture()
def client(monkeypatch, request):
    test_data = PROJECT_ROOT / "tmp" / "tests" / request.node.name
    if test_data.exists():
        shutil.rmtree(test_data)
    test_data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(test_data))
    for module in list(sys.modules):
        if module.startswith("app"):
            del sys.modules[module]
    from app.main import app

    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/companies",
            json={
                "id": "tc",
                "marn": "1234567",
                "registered_agent_name": "Fictional Test Agent",
                "name": "Fictional Test Pty Ltd",
            },
        )
        assert response.status_code == 201, response.text
        HEAD["X-Company-Generation"] = response.json()["generation_id"]
        yield test_client


@pytest.fixture()
def accounts(client):
    response = client.get("/api/v1/accounts", headers=HEAD)
    assert response.status_code == 200, response.text
    return {account["code"]: account for account in response.json()}


def _source_line(accounts, *, tax_code="standard", quantity="2", unit_price="100.00", account_code="4000", mode="exclusive"):
    quantity_d = Decimal(quantity)
    unit_price_d = Decimal(unit_price)
    extended = (quantity_d * unit_price_d).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if tax_code in {"gst_free", "input_taxed", "none"}:
        subtotal, gst, total = extended, Decimal("0.00"), extended
        gst_rate = "0.0000"
    elif mode == "inclusive":
        total = extended
        gst = (total / Decimal("11")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        subtotal = total - gst
        gst_rate = "0.1000"
    else:
        subtotal = extended
        gst = (subtotal * Decimal("0.10")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        total = subtotal + gst
        gst_rate = "0.1000"
    return {
        "description": f"Fictional {tax_code} source item",
        "account_id": accounts[account_code]["id"],
        "quantity": quantity,
        "unit_price": unit_price,
        "gst_rate": gst_rate,
        "line_subtotal": f"{subtotal:.2f}",
        "line_gst": f"{gst:.2f}",
        "line_total": f"{total:.2f}",
        "tax_code": tax_code,
    }


def _create_source(
    client,
    accounts,
    *,
    number="FICTIONAL-SOURCE-1",
    direction="AR",
    contact_name="Fictional Customer",
    mode="exclusive",
    lines=None,
):
    if lines is None:
        lines = [_source_line(accounts, mode=mode)]
    subtotal = sum((Decimal(line["line_subtotal"]) for line in lines), Decimal("0"))
    gst = sum((Decimal(line["line_gst"]) for line in lines), Decimal("0"))
    total = sum((Decimal(line["line_total"]) for line in lines), Decimal("0"))
    payload = {
        "direction": direction,
        "contact_name": contact_name,
        "invoice_number": number,
        "issue_date": "2026-05-31",
        "subtotal": f"{subtotal:.2f}",
        "gst_amount": f"{gst:.2f}",
        "total": f"{total:.2f}",
        "gst_inclusive": mode == "inclusive",
        "amount_mode": mode,
        "lines": lines,
    }
    response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert response.status_code == 201, response.text
    invoice = response.json()
    posted = client.post(f"/api/v1/invoices/{invoice['id']}/post", headers=HEAD)
    assert posted.status_code == 200, posted.text
    posted_invoice = posted.json()["invoice"]
    snapshot = _get_source_snapshot(client, posted_invoice["id"])
    return {**posted_invoice, "lines": snapshot["lines"]}


def _get_source_snapshot(client, invoice_id):
    response = client.get(
        f"/api/v1/credit-notes/source-invoices/{invoice_id}", headers=HEAD
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_credit_note(
    client,
    invoice,
    *,
    number="FICTIONAL-CN-1",
    lines=None,
    **overrides,
):
    if lines is None:
        lines = [
            {
                "source_invoice_line_id": invoice["lines"][0]["source_invoice_line_id"],
                "quantity": "1.0000",
            }
        ]
    payload = {
        "source_invoice_id": invoice["id"],
        "credit_note_number": number,
        "issue_date": "2026-06-01",
        "lines": lines,
        **overrides,
    }
    return client.post("/api/v1/credit-notes", headers=HEAD, json=payload)


def _journal_entries(client):
    response = client.get("/api/v1/journal/entries", headers=HEAD)
    assert response.status_code == 200, response.text
    return response.json()


def test_source_snapshot_lifecycle_and_ar_ap_sources(client, accounts):
    missing = client.get("/api/v1/credit-notes/source-invoices/999", headers=HEAD)
    assert missing.status_code == 404

    draft_payload = {
        "direction": "AR",
        "contact_name": "Fictional Draft Customer",
        "invoice_number": "FICTIONAL-DRAFT-SOURCE",
        "issue_date": "2026-05-31",
        "subtotal": "200.00",
        "gst_amount": "20.00",
        "total": "220.00",
        "lines": [
            {
                "description": "Fictional draft item",
                "account_id": accounts["4000"]["id"],
                "quantity": "2",
                "unit_price": "100.00",
                "line_subtotal": "200.00",
                "line_gst": "20.00",
                "line_total": "220.00",
            }
        ],
    }
    draft = client.post("/api/v1/invoices", headers=HEAD, json=draft_payload)
    assert draft.status_code == 201, draft.text
    draft_snapshot = client.get(
        f"/api/v1/credit-notes/source-invoices/{draft.json()['id']}", headers=HEAD
    )
    assert draft_snapshot.status_code == 409

    ar = _create_source(client, accounts, number="FICTIONAL-AR-SNAPSHOT")
    ap = _create_source(
        client,
        accounts,
        number="FICTIONAL-AP-SNAPSHOT",
        direction="AP",
        contact_name="Fictional Supplier",
        lines=[_source_line(accounts, account_code="6100")],
    )
    before_invoice = client.get(f"/api/v1/invoices/{ar['id']}", headers=HEAD).json()
    before_journals = _journal_entries(client)
    for invoice, direction in ((ar, "AR"), (ap, "AP")):
        source = _get_source_snapshot(client, invoice["id"])
        assert source["direction"] == direction
        assert source["source_invoice_id"] == invoice["id"]
        assert source["lines"][0]["source_invoice_line_id"] == invoice["lines"][0]["source_invoice_line_id"]
        assert source["lines"][0]["remaining_creditable_quantity"] == "2.0000"
    assert _journal_entries(client) == before_journals

    void_source = _create_source(client, accounts, number="FICTIONAL-VOID-SOURCE")
    voided = client.post(
        f"/api/v1/invoices/{void_source['id']}/void", headers=HEAD
    )
    assert voided.status_code == 200, voided.text
    void_snapshot = client.get(
        f"/api/v1/credit-notes/source-invoices/{void_source['id']}", headers=HEAD
    )
    assert void_snapshot.status_code == 409
    assert client.get(f"/api/v1/invoices/{ar['id']}", headers=HEAD).json() == before_invoice


@pytest.mark.parametrize(
    ("mode", "tax_code", "direction", "account_code", "unit_price", "expected"),
    [
        ("exclusive", "standard", "AR", "4000", "100.00", ("100.00", "10.00", "110.00")),
        ("inclusive", "standard", "AR", "4000", "110.00", ("100.00", "10.00", "110.00")),
        ("exclusive", "gst_free", "AR", "4000", "10.00", ("10.00", "0.00", "10.00")),
        ("inclusive", "input_taxed", "AR", "4000", "10.00", ("10.00", "0.00", "10.00")),
        ("exclusive", "none", "AR", "4000", "10.00", ("10.00", "0.00", "10.00")),
        ("inclusive", "capital", "AP", "1700", "110.00", ("100.00", "10.00", "110.00")),
    ],
)
def test_authoritative_calculation_and_source_derivation(
    client, accounts, mode, tax_code, direction, account_code, unit_price, expected
):
    source_line = _source_line(
        accounts,
        tax_code=tax_code,
        quantity="2",
        unit_price=unit_price,
        account_code=account_code,
        mode=mode,
    )
    source = _create_source(
        client,
        accounts,
        number=f"FICTIONAL-{direction}-{tax_code}-{mode}",
        direction=direction,
        contact_name=f"Fictional {direction} {tax_code} Contact",
        mode=mode,
        lines=[source_line],
    )
    source_line_snapshot = source["lines"][0]
    before_invoice = client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json()
    before_journals = _journal_entries(client)
    response = _create_credit_note(client, source)
    assert response.status_code == 201, response.text
    note = response.json()
    line = note["lines"][0]
    assert (note["direction"], note["contact_id"]) == (
        direction,
        source["contact_id"],
    )
    assert note["status"] == "draft"
    assert note["currency"] == "AUD"
    assert (note["subtotal"], note["gst_amount"], note["total"]) == expected
    assert line["quantity"] == "1.0000"
    assert line["source_invoice_line_id"] == source_line_snapshot["source_invoice_line_id"]
    assert line["description"] == source_line_snapshot["description"]
    assert line["account_id"] == source_line_snapshot["account_id"]
    assert line["unit_price"] == source_line_snapshot["unit_price"]
    assert line["gst_rate"] == source_line_snapshot["gst_rate"]
    assert line["tax_code"] == source_line_snapshot["tax_code"]
    assert all(Decimal(value) > 0 for value in (line["quantity"], line["line_subtotal"], line["line_total"]))
    assert note["lines"][0]["line_gst"] == expected[1]
    assert client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json() == before_invoice
    assert _journal_entries(client) == before_journals


def test_multiple_tax_treatments_reserve_quantities_and_update_draft(client, accounts):
    source_lines = [
        _source_line(accounts, tax_code="standard", unit_price="10.00"),
        _source_line(accounts, tax_code="gst_free", unit_price="10.00", account_code="4010"),
        _source_line(accounts, tax_code="input_taxed", unit_price="10.00", account_code="4010"),
        _source_line(accounts, tax_code="none", unit_price="10.00", account_code="4010"),
    ]
    source = _create_source(
        client,
        accounts,
        number="FICTIONAL-MIXED-TAX-SOURCE",
        mode="exclusive",
        lines=source_lines,
    )
    requested = [
        {"source_invoice_line_id": line["source_invoice_line_id"], "quantity": "1.0000"}
        for line in source["lines"]
    ]
    created = _create_credit_note(client, source, number="FICTIONAL-MIXED-CN", lines=requested)
    assert created.status_code == 201, created.text
    note = created.json()
    assert [line["tax_code"] for line in note["lines"]] == [
        "standard", "gst_free", "input_taxed", "none"
    ]
    assert note["gst_amount"] == "1.00"
    snapshot = client.get(
        f"/api/v1/credit-notes/source-invoices/{source['id']}", headers=HEAD
    ).json()
    assert all(line["remaining_creditable_quantity"] == "1.0000" for line in snapshot["lines"])

    replacement = [
        {
            "source_invoice_line_id": source["lines"][0]["source_invoice_line_id"],
            "quantity": "1.5000",
        }
    ]
    updated = client.patch(
        f"/api/v1/credit-notes/{note['id']}",
        headers=HEAD,
        json={
            "credit_note_number": "FICTIONAL-MIXED-CN-UPDATED",
            "issue_date": "2026-06-02",
            "notes": "Fictional draft note",
            "lines": replacement,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["credit_note_number"] == "FICTIONAL-MIXED-CN-UPDATED"
    assert updated.json()["notes"] == "Fictional draft note"
    assert updated.json()["lines"][0]["quantity"] == "1.5000"
    assert updated.json()["total"] == "16.50"
    assert updated.json()["gst_amount"] == "1.50"

    second = _create_credit_note(
        client,
        source,
        number="FICTIONAL-MIXED-CN-SECOND",
        lines=[{
            "source_invoice_line_id": source["lines"][0]["source_invoice_line_id"],
            "quantity": "0.5000",
        }],
    )
    assert second.status_code == 201, second.text
    over_reserved = _create_credit_note(
        client,
        source,
        number="FICTIONAL-MIXED-CN-OVER",
        lines=[{
            "source_invoice_line_id": source["lines"][0]["source_invoice_line_id"],
            "quantity": "0.0001",
        }],
    )
    assert over_reserved.status_code == 409


def test_validation_rejects_bad_quantities_lines_overrides_and_number_conflicts(client, accounts):
    source = _create_source(client, accounts, number="FICTIONAL-VALIDATION-SOURCE")
    other = _create_source(
        client,
        accounts,
        number="FICTIONAL-OTHER-SOURCE",
        contact_name="Fictional Other Customer",
    )
    line_id = source["lines"][0]["source_invoice_line_id"]

    assert _create_credit_note(
        client,
        source,
        number="FICTIONAL-ZERO-QTY",
        lines=[{"source_invoice_line_id": line_id, "quantity": "0"}],
    ).status_code == 422
    assert _create_credit_note(
        client,
        source,
        number="FICTIONAL-FOREIGN-LINE",
        lines=[{
            "source_invoice_line_id": other["lines"][0]["source_invoice_line_id"],
            "quantity": "1",
        }],
    ).status_code == 422
    assert _create_credit_note(
        client,
        source,
        number="FICTIONAL-DUPLICATE-LINE",
        lines=[
            {"source_invoice_line_id": line_id, "quantity": "0.5"},
            {"source_invoice_line_id": line_id, "quantity": "0.5"},
        ],
    ).status_code == 409
    assert _create_credit_note(
        client,
        source,
        number="FICTIONAL-OVER-QTY",
        lines=[{"source_invoice_line_id": line_id, "quantity": "2.0001"}],
    ).status_code == 409
    forbidden_header_fields = {
        "direction": "AP",
        "contact_id": source["contact_id"],
        "currency": "USD",
        "gst_inclusive": False,
        "subtotal": "1.00",
        "gst_amount": "0.00",
        "total": "1.00",
        "status": "draft",
    }
    for index, (field, value) in enumerate(forbidden_header_fields.items()):
        response = _create_credit_note(
            client,
            source,
            number=f"FICTIONAL-HEADER-OVERRIDE-{index}",
            **{field: value},
        )
        assert response.status_code == 422, (field, response.text)
    forbidden_line_fields = {
        "account_id": source["lines"][0]["account_id"],
        "description": "Caller supplied description",
        "unit_price": "1.00",
        "gst_rate": "0.0000",
        "tax_code": "none",
        "line_subtotal": "1.00",
        "line_gst": "0.00",
        "line_total": "1.00",
    }
    for index, (field, value) in enumerate(forbidden_line_fields.items()):
        response = _create_credit_note(
            client,
            source,
            number=f"FICTIONAL-LINE-OVERRIDE-{index}",
            lines=[
                {
                    "source_invoice_line_id": line_id,
                    "quantity": "1",
                    field: value,
                }
            ],
        )
        assert response.status_code == 422, (field, response.text)
    assert _create_credit_note(client, source, number="FICTIONAL-DUP-NUMBER").status_code == 201
    assert _create_credit_note(client, source, number="FICTIONAL-DUP-NUMBER").status_code == 409

    same_number_ap = _create_source(
        client,
        accounts,
        number="FICTIONAL-SAME-NUMBER-AP-SOURCE",
        direction="AP",
        contact_name="Fictional Customer",
        lines=[_source_line(accounts, account_code="6100")],
    )
    permitted_other_direction = _create_credit_note(
        client,
        same_number_ap,
        number="FICTIONAL-DUP-NUMBER",
    )
    assert permitted_other_direction.status_code == 201, permitted_other_direction.text

    assert client.patch(
        f"/api/v1/credit-notes/{permitted_other_direction.json()['id']}",
        headers=HEAD,
        json={"status": "authorised"},
    ).status_code == 422
    assert client.patch(
        f"/api/v1/credit-notes/{permitted_other_direction.json()['id']}",
        headers=HEAD,
        json={"source_invoice_id": source["id"]},
    ).status_code == 422
    assert client.patch(
        f"/api/v1/credit-notes/{permitted_other_direction.json()['id']}",
        headers=HEAD,
        json={"unit_price": "1.00"},
    ).status_code == 422


def test_source_rejects_positive_paid_amount_and_explicit_allocation(client, accounts):
    from app.db.company import company_session
    from app.models.company import Invoice

    paid_source = _create_source(client, accounts, number="FICTIONAL-PAID-SOURCE")
    with company_session("tc") as db:
        invoice = db.get(Invoice, paid_source["id"])
        invoice.paid_amount = Decimal("1.00")
        db.commit()
    paid = client.get(
        f"/api/v1/credit-notes/source-invoices/{paid_source['id']}", headers=HEAD
    )
    assert paid.status_code == 409

    allocated_source = _create_source(client, accounts, number="FICTIONAL-ALLOCATED-SOURCE")
    bank_accounts = client.get("/api/v1/bank-accounts", headers=HEAD)
    assert bank_accounts.status_code == 200, bank_accounts.text
    bank_account_id = bank_accounts.json()[0]["id"]
    transaction = client.post(
        f"/api/v1/bank-accounts/{bank_account_id}/transactions",
        headers={**HEAD, "Idempotency-Key": "fictional-credit-note-settlement"},
        json={
            "direction": "in",
            "amount": "1.00",
            "occurred_at": "2026-06-01",
            "memo": "Fictional settlement evidence",
            "counter_party_name": "Fictional Customer",
            "account_id": accounts["1100"]["id"],
            "gst_amount": "0.00",
            "tax_code": "none",
            "invoice_allocations": [
                {"invoice_id": allocated_source["id"], "amount": "1.00"}
            ],
        },
    )
    assert transaction.status_code == 201, transaction.text
    allocated = client.get(
        f"/api/v1/credit-notes/source-invoices/{allocated_source['id']}", headers=HEAD
    )
    assert allocated.status_code == 409


def test_draft_delete_and_no_accounting_or_bas_effect(client, accounts):
    source = _create_source(client, accounts, number="FICTIONAL-NO-EFFECT-SOURCE")
    invoice_before = client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json()
    journals_before = _journal_entries(client)
    bas_before = client.get(
        "/api/v1/reports/bas?fy_year=2026&quarter=4", headers=HEAD
    )
    assert bas_before.status_code == 200, bas_before.text

    created = _create_credit_note(client, source, number="FICTIONAL-NO-EFFECT-CN")
    assert created.status_code == 201, created.text
    assert _journal_entries(client) == journals_before
    assert client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json() == invoice_before
    bas_created = client.get(
        "/api/v1/reports/bas?fy_year=2026&quarter=4", headers=HEAD
    )
    assert bas_created.status_code == 200, bas_created.text
    assert bas_created.json() == bas_before.json()
    updated = client.patch(
        f"/api/v1/credit-notes/{created.json()['id']}",
        headers=HEAD,
        json={"notes": "Still a draft"},
    )
    assert updated.status_code == 200, updated.text
    assert _journal_entries(client) == journals_before
    assert client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json() == invoice_before
    bas_updated = client.get(
        "/api/v1/reports/bas?fy_year=2026&quarter=4", headers=HEAD
    )
    assert bas_updated.status_code == 200, bas_updated.text
    assert bas_updated.json() == bas_before.json()
    deleted = client.delete(
        f"/api/v1/credit-notes/{created.json()['id']}", headers=HEAD
    )
    assert deleted.status_code == 204
    assert client.delete("/api/v1/credit-notes/999", headers=HEAD).status_code == 404
    assert client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json() == invoice_before
    assert _journal_entries(client) == journals_before
    from app.db.company import company_session
    from app.models.company import Contact, CreditNote, CreditNoteLine, InvoiceLine

    with company_session("tc") as db:
        assert db.query(CreditNote).count() == 0
        assert db.query(CreditNoteLine).count() == 0
        assert db.query(InvoiceLine).filter_by(invoice_id=source["id"]).count() == 1
        assert db.get(Contact, source["contact_id"]) is not None
    bas_after = client.get(
        "/api/v1/reports/bas?fy_year=2026&quarter=4", headers=HEAD
    )
    assert bas_after.status_code == 200, bas_after.text
    assert bas_after.json() == bas_before.json()


def test_startup_creates_credit_note_tables_and_named_constraints(client):
    from app.db.company import get_company_engine

    database = inspect(get_company_engine("tc"))
    assert "credit_notes" in database.get_table_names()
    assert "credit_note_lines" in database.get_table_names()
    header_unique = database.get_unique_constraints("credit_notes")
    assert any(
        constraint["name"] == "uq_credit_note_dir_contact_no"
        for constraint in header_unique
    )
    line_unique = database.get_unique_constraints("credit_note_lines")
    assert any(
        constraint["name"] == "uq_credit_note_line_note_source_line"
        and constraint["column_names"] == ["credit_note_id", "source_invoice_line_id"]
        for constraint in line_unique
    )
    header_checks = {
        constraint["name"] for constraint in database.get_check_constraints("credit_notes")
    }
    line_checks = {
        constraint["name"] for constraint in database.get_check_constraints("credit_note_lines")
    }
    assert {
        "ck_credit_note_direction",
        "ck_credit_note_currency_aud",
        "ck_credit_note_status_allowed",
        "ck_credit_note_subtotal_nonneg",
        "ck_credit_note_gst_nonneg",
        "ck_credit_note_total_nonneg",
        "ck_credit_note_total_positive",
    } <= header_checks
    status_check = next(
        constraint
        for constraint in database.get_check_constraints("credit_notes")
        if constraint["name"] == "ck_credit_note_status_allowed"
    )
    assert status_check["sqltext"] == "status IN ('draft', 'authorised', 'void')"
    assert {
        "ck_credit_note_line_quantity_positive",
        "ck_credit_note_line_unit_price_nonneg",
        "ck_credit_note_line_subtotal_nonneg",
        "ck_credit_note_line_gst_nonneg",
        "ck_credit_note_line_total_nonneg",
        "ck_credit_note_line_subtotal_positive",
        "ck_credit_note_line_total_positive",
        "ck_credit_note_line_gst_within",
        "ck_credit_note_line_tax_code",
    } <= line_checks
    line_columns = {
        column["name"]: column for column in database.get_columns("credit_note_lines")
    }
    assert line_columns["account_id"]["nullable"] is False
    header_fks = database.get_foreign_keys("credit_notes")
    assert any(
        foreign_key["referred_table"] == "invoices"
        and foreign_key["options"].get("ondelete") == "RESTRICT"
        for foreign_key in header_fks
    )
    line_fks = database.get_foreign_keys("credit_note_lines")
    assert any(
        foreign_key["referred_table"] == "credit_notes"
        and foreign_key["constrained_columns"] == ["credit_note_id"]
        and foreign_key["options"].get("ondelete") == "CASCADE"
        for foreign_key in line_fks
    )
    assert any(
        foreign_key["referred_table"] == "invoice_lines"
        and foreign_key["constrained_columns"] == ["source_invoice_line_id"]
        and foreign_key["options"].get("ondelete") == "RESTRICT"
        for foreign_key in line_fks
    )