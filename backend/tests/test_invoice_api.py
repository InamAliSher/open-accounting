from __future__ import annotations

import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _request_headers import manual_transaction_headers

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
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/v1/companies", json={"id": "tc", "marn": "1234567", "registered_agent_name": "Test Agent", "name": "Test Pty Ltd"})
        assert r.status_code == 201, r.text
        HEAD["X-Company-Generation"] = r.json()["generation_id"]
        yield c


@pytest.fixture()
def accounts(client):
    r = client.get("/api/v1/accounts", headers=HEAD)
    assert r.status_code == 200
    return {a["code"]: a for a in r.json()}


def _invoice_payload(accounts, *, number="INV-API-1"):
    return {
        "direction": "AR",
        "contact_name": "API Customer",
        "invoice_number": number,
        "issue_date": "2026-05-31",
        "subtotal": "100.00",
        "gst_amount": "10.00",
        "total": "110.00",
        "lines": [
            {
                "description": "Services",
                "account_id": accounts["4000"]["id"],
                "quantity": "1",
                "unit_price": "100.00",
                "gst_rate": "0.10",
                "line_subtotal": "100.00",
                "line_gst": "10.00",
                "line_total": "110.00",
            }
        ],
    }


def _create_invoice(client, accounts, *, number="INV-API-1"):
    r = client.post("/api/v1/invoices", headers=HEAD, json=_invoice_payload(accounts, number=number))
    assert r.status_code == 201, r.text
    return r.json()


def _explicit_payload(
    accounts,
    *,
    number,
    amount_mode,
    tax_code="standard",
    direction="AR",
    account_code=None,
    quantity="1",
    unit_price="100.00",
    subtotal="100.00",
    gst="10.00",
    total="110.00",
):
    account_code = account_code or ("4000" if direction == "AR" else "6100")
    return {
        "direction": direction,
        "contact_name": "Explicit Mode Contact",
        "invoice_number": number,
        "issue_date": "2026-05-31",
        "subtotal": subtotal,
        "gst_amount": gst,
        "total": total,
        "gst_inclusive": True,
        "amount_mode": amount_mode,
        "lines": [
            {
                "description": "Fictional service",
                "account_id": accounts[account_code]["id"],
                "quantity": quantity,
                "unit_price": unit_price,
                "line_subtotal": subtotal,
                "line_gst": gst,
                "line_total": total,
                "tax_code": tax_code,
            }
        ],
    }


def test_explicit_exclusive_inclusive_and_no_tax_examples_create_drafts(client, accounts):
    examples = (
        _explicit_payload(
            accounts,
            number="MODE-EXCLUSIVE",
            amount_mode="exclusive",
            unit_price="1000.00",
            subtotal="1000.00",
            gst="100.00",
            total="1100.00",
        ),
        _explicit_payload(
            accounts,
            number="MODE-INCLUSIVE",
            amount_mode="inclusive",
            unit_price="1100.00",
            subtotal="1000.00",
            gst="100.00",
            total="1100.00",
        ),
        _explicit_payload(
            accounts,
            number="MODE-GST-FREE",
            amount_mode="inclusive",
            tax_code="gst_free",
            quantity="2",
            unit_price="55.00",
            subtotal="110.00",
            gst="0.00",
            total="110.00",
        ),
    )

    for payload in examples:
        response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
        assert response.status_code == 201, response.text
        invoice = response.json()
        assert invoice["status"] == "draft"
        assert invoice["journal_entries"] == []
        assert (invoice["subtotal"], invoice["gst_amount"], invoice["total"]) == (
            payload["subtotal"], payload["gst_amount"], payload["total"]
        )

    journals = client.get(
        "/api/v1/journal/entries?source_type=invoice_ar", headers=HEAD
    )
    assert journals.status_code == 200
    assert journals.json() == []


def test_explicit_tax_code_mode_matrix_and_no_tax_normalization(client, accounts):
    cases = []
    for amount_mode, unit_price, subtotal, gst, total in (
        ("exclusive", "100.00", "100.00", "10.00", "110.00"),
        ("inclusive", "110.00", "100.00", "10.00", "110.00"),
    ):
        for tax_code in ("standard", "capital"):
            cases.append((amount_mode, tax_code, unit_price, subtotal, gst, total))
        for tax_code in ("gst_free", "input_taxed", "none"):
            cases.append((amount_mode, tax_code, "55.00", "55.00", "0.00", "55.00"))

    for amount_mode, tax_code, unit_price, subtotal, gst, total in cases:
        direction = "AP" if tax_code == "capital" else "AR"
        account_code = "1700" if tax_code == "capital" else "4000"
        payload = _explicit_payload(
            accounts,
            number=f"MATRIX-{amount_mode}-{tax_code}",
            amount_mode=amount_mode,
            tax_code=tax_code,
            direction=direction,
            account_code=account_code,
            unit_price=unit_price,
            subtotal=subtotal,
            gst=gst,
            total=total,
        )
        response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
        assert response.status_code == 201, response.text

    no_tax = _explicit_payload(
        accounts,
        number="MATRIX-NO-TAX",
        amount_mode="none",
        tax_code="standard",
        unit_price="55.00",
        subtotal="55.00",
        gst="0.00",
        total="55.00",
    )
    response = client.post("/api/v1/invoices", headers=HEAD, json=no_tax)
    assert response.status_code == 201, response.text
    assert response.json()["gst_inclusive"] is False

    from app.db.company import company_session
    from app.models.company import Invoice

    with company_session("tc") as db:
        stored = db.query(Invoice).filter_by(invoice_number="MATRIX-NO-TAX").one()
        assert stored.lines[0].tax_code == "none"


def test_explicit_amount_mode_rejects_incomplete_or_mismatched_lines(client, accounts):
    valid = _explicit_payload(
        accounts,
        number="MODE-VALIDATION-BASE",
        amount_mode="exclusive",
    )
    invalid_payloads = []

    wrong_subtotal = {**valid, "invoice_number": "MODE-WRONG-SUBTOTAL"}
    wrong_subtotal["lines"] = [{**valid["lines"][0], "line_subtotal": "99.00"}]
    invalid_payloads.append(wrong_subtotal)

    wrong_gst = {**valid, "invoice_number": "MODE-WRONG-GST"}
    wrong_gst["lines"] = [{**valid["lines"][0], "line_gst": "9.00"}]
    invalid_payloads.append(wrong_gst)

    wrong_header = {**valid, "invoice_number": "MODE-WRONG-HEADER", "total": "109.00"}
    invalid_payloads.append(wrong_header)

    missing_price = {**valid, "invoice_number": "MODE-MISSING-PRICE"}
    missing_price["lines"] = [{key: value for key, value in valid["lines"][0].items() if key != "unit_price"}]
    invalid_payloads.append(missing_price)

    invalid_tax_code = {**valid, "invoice_number": "MODE-INVALID-TAX-CODE"}
    invalid_tax_code["lines"] = [{**valid["lines"][0], "tax_code": "gst_free", "line_gst": "10.00"}]
    invalid_payloads.append(invalid_tax_code)

    null_tax_code = {**valid, "invoice_number": "MODE-NULL-TAX-CODE"}
    null_tax_code["lines"] = [{**valid["lines"][0], "tax_code": None}]
    invalid_payloads.append(null_tax_code)

    capital_ar = _explicit_payload(
        accounts,
        number="MODE-CAPITAL-AR",
        amount_mode="exclusive",
        tax_code="capital",
        direction="AR",
        account_code="4000",
    )
    invalid_payloads.append(capital_ar)

    capital_expense = _explicit_payload(
        accounts,
        number="MODE-CAPITAL-EXPENSE",
        amount_mode="exclusive",
        tax_code="capital",
        direction="AP",
        account_code="6100",
    )
    invalid_payloads.append(capital_expense)

    ar_expense = _explicit_payload(
        accounts,
        number="MODE-AR-EXPENSE",
        amount_mode="exclusive",
        direction="AR",
        account_code="6100",
    )
    invalid_payloads.append(ar_expense)

    ap_income = _explicit_payload(
        accounts,
        number="MODE-AP-INCOME",
        amount_mode="exclusive",
        direction="AP",
        account_code="4000",
    )
    invalid_payloads.append(ap_income)

    for payload in invalid_payloads:
        response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
        assert response.status_code == 422, (payload["invoice_number"], response.text)


def test_explicit_rounds_each_line_before_header_summation(client, accounts):
    payload = _explicit_payload(
        accounts,
        number="MODE-LINE-ROUNDING",
        amount_mode="exclusive",
        unit_price="0.05",
        subtotal="0.10",
        gst="0.02",
        total="0.12",
    )
    payload["lines"] = [
        {
            **payload["lines"][0],
            "description": f"Line {index}",
            "line_subtotal": "0.05",
            "line_gst": "0.01",
            "line_total": "0.06",
        }
        for index in (1, 2)
    ]
    response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert response.status_code == 201, response.text


def test_explicit_quantity_scale_and_half_up_extended_amount(client, accounts):
    payload = _explicit_payload(
        accounts,
        number="MODE-QUANTITY-SCALE",
        amount_mode="exclusive",
        quantity="1.2345",
        unit_price="0.01",
        subtotal="0.01",
        gst="0.00",
        total="0.01",
    )
    response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert response.status_code == 201, response.text

    half_cent = _explicit_payload(
        accounts,
        number="MODE-QUANTITY-HALF-CENT",
        amount_mode="exclusive",
        quantity="1.5",
        unit_price="0.01",
        subtotal="0.02",
        gst="0.00",
        total="0.02",
    )
    response = client.post("/api/v1/invoices", headers=HEAD, json=half_cent)
    assert response.status_code == 201, response.text


def test_legacy_payload_without_amount_mode_keeps_existing_contract(client, accounts):
    payload = _invoice_payload(accounts, number="LEGACY-AMOUNT-MODE-OMITTED")
    payload["gst_inclusive"] = True
    response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert response.status_code == 201, response.text
    invoice = response.json()
    assert invoice["status"] == "draft"
    assert invoice["gst_inclusive"] is True
    assert invoice["journal_entries"] == []


def test_post_endpoint_creates_journal_and_list_filter_finds_it(client, accounts):
    inv = _create_invoice(client, accounts)
    assert inv["status"] == "draft"
    r = client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["invoice"]["status"] == "authorised"
    assert body["journal_entry"]["source_type"] == "invoice_ar"

    r = client.get("/api/v1/journal/entries?source_type=invoice_ar", headers=HEAD)
    assert r.status_code == 200, r.text
    assert [e["id"] for e in r.json()] == [body["journal_entry"]["id"]]


def test_post_already_authorised_returns_409(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-2")
    assert client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD).status_code == 200
    r = client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    assert r.status_code == 409


def test_void_endpoint_creates_reversal(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-3")
    client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    r = client.post(f"/api/v1/invoices/{inv['id']}/void", headers=HEAD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["invoice"]["status"] == "void"
    assert body["journal_entry"]["source_type"] == "invoice_reversal"
    assert body["journal_entry"]["reverses_entry_id"] is not None


def test_patch_financial_field_on_authorised_invoice_rejected(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-4")
    client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    r = client.patch(f"/api/v1/invoices/{inv['id']}", headers=HEAD, json={"subtotal": "90.00"})
    assert r.status_code == 422


def test_delete_draft_hard_deletes_without_journal(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-5")
    r = client.delete(f"/api/v1/invoices/{inv['id']}", headers=HEAD)
    assert r.status_code == 204, r.text
    assert client.get(f"/api/v1/invoices/{inv['id']}", headers=HEAD).status_code == 404
    r = client.get("/api/v1/journal/entries?source_type=invoice_ar", headers=HEAD)
    assert r.status_code == 200
    assert r.json() == []


def test_delete_authorised_voids_and_posts_reversal(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-6")
    client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    r = client.delete(f"/api/v1/invoices/{inv['id']}", headers=HEAD)
    assert r.status_code == 204, r.text
    r = client.get(f"/api/v1/invoices/{inv['id']}", headers=HEAD)
    assert r.status_code == 200
    assert r.json()["status"] == "void"
    r = client.get("/api/v1/journal/entries?source_type=invoice_reversal", headers=HEAD)
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_concurrent_post_one_wins_other_409_not_500(client, accounts):
    """Round-3 P2: two simultaneous /post calls → [200, 409], one journal entry.
    The loser must not surface the DB unique-index IntegrityError as a 500.
    """
    inv = _create_invoice(client, accounts, number="INV-API-RACE-POST")

    def post():
        return client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(), range(2)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], [r.text for r in responses]

    r = client.get("/api/v1/journal/entries?source_type=invoice_ar", headers=HEAD)
    assert r.status_code == 200
    entries = [e for e in r.json() if e["source_id"] == inv["id"]]
    assert len(entries) == 1


def test_concurrent_void_one_wins_other_409_not_500(client, accounts):
    """Round-3 P2: two simultaneous /void calls → [200, 409], one reversal entry."""
    inv = _create_invoice(client, accounts, number="INV-API-RACE-VOID")
    assert client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD).status_code == 200

    def void():
        return client.post(f"/api/v1/invoices/{inv['id']}/void", headers=HEAD)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: void(), range(2)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], [r.text for r in responses]

    r = client.get("/api/v1/journal/entries?source_type=invoice_reversal", headers=HEAD)
    assert r.status_code == 200
    entries = [e for e in r.json() if e["source_id"] == inv["id"]]
    assert len(entries) == 1


def test_concurrent_settlement_and_void_have_one_safe_winner(client, accounts):
    """A settle/void race must never leave both the bank settlement and the
    invoice reversal committed. SQLite's immediate lock serialises the two
    invariant checks; the loser receives a recoverable 409."""
    inv = _create_invoice(client, accounts, number="INV-API-RACE-SETTLE-VOID")
    assert client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD).status_code == 200
    bank = client.get("/api/v1/bank-accounts", headers=HEAD).json()[0]

    def settle():
        return client.post(
            f"/api/v1/bank-accounts/{bank['id']}/transactions",
            headers=manual_transaction_headers(HEAD),
            json={
                "direction": "in",
                "amount": "50.00",
                "occurred_at": "2026-06-01",
                "account_id": accounts["1100"]["id"],
                "tax_code": "standard",
                "gst_amount": "0",
            },
        )

    def void():
        return client.post(f"/api/v1/invoices/{inv['id']}/void", headers=HEAD)

    with ThreadPoolExecutor(max_workers=2) as pool:
        settle_future = pool.submit(settle)
        void_future = pool.submit(void)
        settle_response = settle_future.result()
        void_response = void_future.result()

    assert sorted((settle_response.status_code, void_response.status_code)) in (
        [200, 409],
        [201, 409],
    ), (settle_response.text, void_response.text)

    current = client.get(f"/api/v1/invoices/{inv['id']}", headers=HEAD).json()
    transactions = client.get(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=HEAD,
    ).json()
    matching_settlements = [
        txn
        for txn in transactions
        if txn["account_id"] == accounts["1100"]["id"]
        and txn["amount"] == "50.00"
    ]
    if current["status"] == "void":
        assert matching_settlements == []
        assert settle_response.status_code == 409
    else:
        assert len(matching_settlements) == 1
        assert settle_response.status_code == 201
        assert void_response.status_code == 409
