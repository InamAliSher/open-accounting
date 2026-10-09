"""Tests for bank statement import + auto-categorisation rules (M3).

Covers:
  - CSV parsing with separate debit/credit columns
  - CSV parsing with signed amount column
  - Header auto-mapping
  - Dedup detection on re-import
  - Rule matching: priority, memo regex, amount range
  - Commit endpoint actually creates the txns and skips duplicates
  - Rules CRUD
"""

from __future__ import annotations

import io
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from decimal import Decimal

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
        import shutil
        shutil.rmtree(test_data)
    test_data.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATA_DIR", str(test_data))
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]
    from app.main import app
    with TestClient(app) as c:
        company = c.post("/api/v1/companies", json={"id": "tc", "marn": "1234567", "registered_agent_name": "Test Agent", "name": "Test Pty Ltd"})
        HEAD["X-Company-Generation"] = company.json()["generation_id"]
        yield c


@pytest.fixture()
def biz_bank(client):
    r = client.get("/api/v1/bank-accounts", headers=HEAD)
    return r.json()[0]


@pytest.fixture()
def accounts(client):
    r = client.get("/api/v1/accounts", headers=HEAD)
    return {a["code"]: a for a in r.json()}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _upload(client, bank_id, csv_text: str, *, filename: str = "stmt.csv"):
    return client.post(
        f"/api/v1/bank-accounts/{bank_id}/import/preview",
        headers=HEAD,
        files={"file": (filename, io.BytesIO(csv_text.encode("utf-8")), "text/csv")},
    )


def _commit_preview(
    client,
    bank_id: int,
    content: str | bytes,
    *,
    preview: dict | None = None,
    filename: str = "stmt.csv",
    bank_format: str | None = None,
    import_mode: str = "new_import",
    row_overrides: dict[int, dict] | None = None,
    mapping_override: dict | None = None,
    preview_key_override: str | None = None,
):
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if preview is None:
        response = _upload(client, bank_id, raw.decode("utf-8"), filename=filename)
        assert response.status_code == 200, response.text
        preview = response.json()
    decisions = []
    for index, row in enumerate(preview["rows"]):
        override = (row_overrides or {}).get(index, {})
        include = override.get(
            "include",
            row["ok"]
            and not row.get("is_duplicate")
            and not row.get("requires_review")
            and not row.get("review_blocked"),
        )
        decisions.append(
            {
                "row_key": override.get("row_key", row["row_key"]),
                "include": include,
                "account_id": override.get(
                    "account_id", row.get("suggested_account_id")
                ),
                "tax_code": override.get(
                    "tax_code", row.get("suggested_tax_code") or "standard"
                ),
                "gst_amount": override.get(
                    "gst_amount", row.get("suggested_gst_amount") or "0.00"
                ),
                "invoice_allocations": override.get("invoice_allocations", []),
            }
        )
    payload = {
        "preview_key": preview_key_override or preview["preview_key"],
        "mapping": mapping_override or preview["mapping"],
        "import_mode": import_mode,
        "rows": decisions,
    }
    data = {"payload_json": json.dumps(payload)}
    if bank_format is not None:
        data["bank_format"] = bank_format
    return client.post(
        f"/api/v1/bank-accounts/{bank_id}/import/commit",
        headers=HEAD,
        files={"file": (filename, io.BytesIO(raw), "application/octet-stream")},
        data=data,
    )


def test_preview_separate_debit_credit_columns(client, biz_bank):
    csv = (
        "Date,Description,Debit,Credit\n"
        "2026-05-01,Zzqq refund,,5000.00\n"
        "2026-05-02,Office rent,1500.00,\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mapping"]["occurred_at"] is not None
    assert body["mapping"]["debit"] is not None
    assert body["mapping"]["credit"] is not None
    rows = body["rows"]
    assert len(rows) == 2
    # First row: credit 5000 → IN
    assert rows[0]["parsed"]["direction"] == "in"
    assert rows[0]["parsed"]["amount"] == "5000.00"
    # Second row: debit 1500 → OUT
    assert rows[1]["parsed"]["direction"] == "out"
    assert rows[1]["parsed"]["amount"] == "1500.00"
    # New, no rules + a memo that matches no heuristic → no suggestion,
    # not duplicate. (Uses a nonsense memo: real words like "salary" now
    # legitimately match the salary/wages heuristic.)
    assert rows[0]["is_duplicate"] is False
    assert rows[0]["suggested_account_id"] is None


def test_preview_debit_amount_credit_amount_headers_keep_direction(client, biz_bank):
    csv = (
        "Bank Account,Date,Narrative,Debit Amount,Credit Amount,Balance\n"
        "123,2026-05-01,Office rent,1500.00,,8500.00\n"
        "123,2026-05-02,Customer payment,,2200.00,10700.00\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mapping"]["debit"] == 3
    assert body["mapping"]["credit"] == 4
    assert body["mapping"]["amount"] is None
    assert body["rows"][0]["parsed"]["direction"] == "out"
    assert body["rows"][0]["parsed"]["amount"] == "1500.00"
    assert body["rows"][1]["parsed"]["direction"] == "in"
    assert body["rows"][1]["parsed"]["amount"] == "2200.00"


def test_preview_chinese_amount_headers_keep_direction(client, biz_bank):
    csv = (
        "\u65e5\u671f,\u6458\u8981,\u652f\u51fa\u91d1\u989d,\u6536\u5165\u91d1\u989d\n"
        "2026-05-01,\u623f\u79df,1500.00,\n"
        "2026-05-02,\u6536\u6b3e,,2200.00\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mapping"]["debit"] == 2
    assert body["mapping"]["credit"] == 3
    assert body["mapping"]["amount"] is None
    assert body["rows"][0]["parsed"]["direction"] == "out"
    assert body["rows"][1]["parsed"]["direction"] == "in"


def test_short_hints_do_not_steal_amount_columns():
    """Bare 'in'/'out' hints must match whole words only: 'in' ⊂ 'incl' /
    'spending' used to let credit claim a lone amount column and import every
    expense as money-in."""
    from app.services.bank_import import propose_mapping

    m = propose_mapping(["Date", "Description", "Amount (incl GST)"])
    assert m["amount"] == 2 and m["credit"] is None and m["debit"] is None

    m = propose_mapping(["Date", "Description", "Spending Amount"])
    assert m["amount"] == 2 and m["credit"] is None and m["debit"] is None

    m = propose_mapping(["Date", "Description", "Payout Amount"])
    assert m["amount"] == 2 and m["debit"] is None


def test_money_in_money_out_headers_still_map_as_debit_credit():
    from app.services.bank_import import propose_mapping

    m = propose_mapping(["Date", "Description", "Money Out", "Money In"])
    assert m["debit"] == 2
    assert m["credit"] == 3
    assert m["amount"] is None


def test_preview_signed_amount_column(client, biz_bank):
    csv = (
        "Date,Narrative,Amount\n"
        "2026-05-01,Consulting,1100.00\n"
        "2026-05-02,Rent,-1500.00\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    body = r.json()
    assert body["rows"][0]["parsed"]["direction"] == "in"
    assert body["rows"][1]["parsed"]["direction"] == "out"
    assert body["rows"][1]["parsed"]["amount"] == "1500.00"  # unsigned


def test_preview_zero_amount_row_has_specific_issue(client, biz_bank):
    csv = "Date,Description,Amount\n2026-05-01,Zero amount adjustment,0.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["ok"] is False
    assert row["issue"] == "Zero-amount rows are skipped; bank transactions must be non-zero"


def test_dedup_marks_existing(client, biz_bank):
    csv = "Date,Description,Credit\n2026-05-01,Salary,5000.00\n"
    # First import: commit it.
    r = _upload(client, biz_bank["id"], csv)
    rows = r.json()["rows"]
    commit = _commit_preview(client, biz_bank["id"], csv, preview=r.json())
    assert commit.status_code == 200, commit.text
    assert commit.json() == {"created": 1, "skipped_duplicates": 0}

    # An identical no-ID statement is intentionally ambiguous until reviewed.
    r2 = _upload(client, biz_bank["id"], csv)
    assert r2.json()["statement_review_required"] is True
    assert r2.json()["rows"][0]["requires_review"] is True

    commit2 = _commit_preview(
        client,
        biz_bank["id"],
        csv,
        preview=r2.json(),
        import_mode="same_import",
    )
    assert commit2.status_code == 200, commit2.text
    assert commit2.json() == {"created": 0, "skipped_duplicates": 1}


def test_dedup_flags_row_matching_manual_transaction(client, biz_bank):
    """A CSV row that duplicates a MANUALLY-entered transaction (which has no
    dedup_key) is flagged — while a same-amount/same-day row with different text
    is not. Guards the gap where re-importing over hand-typed rows silently
    double-counted money."""
    # Manually-entered transaction (no dedup_key), memo "Acme Client Payment".
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={
            "direction": "in",
            "amount": "5000.00",
            "occurred_at": "2026-07-01",
            "memo": "Acme Client Payment",
            "tax_code": "none",
        },
    )
    assert r.status_code == 201, r.text

    csv = (
        "Date,Description,Credit,Payee\n"
        # Same amount/date/direction; payee matches the manual memo → duplicate.
        "2026-07-01,Invoice payment received,5000.00,Acme Client Payment\n"
        # Same amount/date/direction but no text overlap → NOT a duplicate.
        "2026-07-01,Totally different deposit,5000.00,Someone Else\n"
    )
    r2 = _upload(client, biz_bank["id"], csv)
    assert r2.status_code == 200, r2.text
    rows = r2.json()["rows"]
    assert rows[0]["is_duplicate"] is False, rows[0]
    assert rows[0]["requires_review"] is True, rows[0]
    assert rows[1]["is_duplicate"] is False, rows[1]


def test_named_month_date_formats_parse(client, biz_bank):
    """`15-Jul-2026` (day-Mon-year) and `Jul 15 2026` (month-first) parse."""
    csv = (
        "Date,Description,Credit\n"
        "15-Jul-2026,Row A,100.00\n"
        "Jul 15 2026,Row B,200.00\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert rows[0]["ok"] is True and rows[0]["parsed"]["occurred_at"] == "2026-07-15", rows[0]
    assert rows[1]["ok"] is True and rows[1]["parsed"]["occurred_at"] == "2026-07-15", rows[1]


def test_far_future_date_rejected_on_manual_entry(client, biz_bank):
    """A transaction dated beyond the reportable BAS window (FY2000–FY2100) is
    rejected, so it can't become an orphan that shows in the trial balance but
    no BAS quarter."""
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={"direction": "in", "amount": "100.00", "occurred_at": "9999-12-31"},
    )
    assert r.status_code == 422, r.text
    # A normal date still works.
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={"direction": "in", "amount": "100.00", "occurred_at": "2026-07-01"},
    )
    assert r.status_code == 201, r.text


def test_ambiguous_debit_and_credit_row_flagged(client, biz_bank):
    """A row with BOTH a debit and a credit is flagged, not silently resolved to
    one direction."""
    csv = "Date,Description,Debit,Credit\n2026-05-01,Weird row,50.00,80.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["ok"] is False
    assert "both a debit and a credit" in row["issue"].lower()


def test_intra_file_duplicate_rows_flagged(client, biz_bank):
    """Identical row occurrences in one statement remain distinct identities."""
    csv = (
        "Date,Description,Credit\n"
        "2026-05-01,Salary,5000.00\n"
        "2026-05-01,Salary,5000.00\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert rows[0]["is_duplicate"] is False, rows[0]
    assert rows[1]["is_duplicate"] is False, rows[1]
    assert rows[0]["row_key"] != rows[1]["row_key"]
    committed = _commit_preview(client, biz_bank["id"], csv, preview=r.json())
    assert committed.status_code == 200, committed.text
    assert committed.json() == {"created": 2, "skipped_duplicates": 0}


def test_reordered_rows_keep_occurrence_identity(client, biz_bank):
    original = (
        "Date,Description,Credit\n"
        "2026-05-01,Repeated,10.00\n"
        "2026-05-02,Other,20.00\n"
        "2026-05-01,Repeated,10.00\n"
    )
    reordered = (
        "Date,Description,Credit\n"
        "2026-05-01,Repeated,10.00\n"
        "2026-05-01,Repeated,10.00\n"
        "2026-05-02,Other,20.00\n"
    )
    first = _upload(client, biz_bank["id"], original).json()
    second = _upload(client, biz_bank["id"], reordered).json()
    assert first["import_statement_key"] == second["import_statement_key"]

    def identities(preview):
        grouped = {}
        for row in preview["rows"]:
            key = (
                row["parsed"]["occurred_at"],
                row["parsed"]["memo"],
                row["parsed"]["amount"],
            )
            grouped.setdefault(key, []).append(row["row_key"])
        return {key: sorted(values) for key, values in grouped.items()}

    assert identities(first) == identities(second)


def test_identical_no_id_statement_requires_same_or_independent_choice(
    client, biz_bank
):
    csv = "Date,Description,Credit\n2026-05-03,Review me,25.00\n"
    first = _commit_preview(client, biz_bank["id"], csv)
    assert first.status_code == 200, first.text
    repeated = _upload(client, biz_bank["id"], csv).json()
    assert repeated["statement_review_required"] is True

    independent = _commit_preview(
        client,
        biz_bank["id"],
        csv,
        preview=repeated,
        import_mode="independent_import",
        row_overrides={0: {"include": True}},
    )
    assert independent.status_code == 200, independent.text
    assert independent.json() == {"created": 1, "skipped_duplicates": 0}

    from app.db.company import company_session
    from app.models.company import BankTransaction

    with company_session("tc") as db:
        imported = (
            db.query(BankTransaction)
            .filter(BankTransaction.memo == "Review me")
            .order_by(BankTransaction.id)
            .all()
        )
        assert len(imported) == 2
        assert imported[0].import_instance_id != imported[1].import_instance_id


def test_provider_id_conflict_requires_review_and_is_blocked(client, biz_bank):
    first_csv = (
        "Date,Description,Transaction ID,Credit\n"
        "2026-05-04,Settlement,provider-77,40.00\n"
    )
    first = _commit_preview(client, biz_bank["id"], first_csv)
    assert first.status_code == 200, first.text

    conflict_csv = (
        "Date,Description,Transaction ID,Credit\n"
        "2026-05-04,Changed settlement,provider-77,41.00\n"
    )
    conflict = _upload(client, biz_bank["id"], conflict_csv).json()
    row = conflict["rows"][0]
    assert row["requires_review"] is True
    assert row["review_blocked"] is True
    assert row["review_reason"] == "provider_id_conflict"

    rejected = _commit_preview(
        client,
        biz_bank["id"],
        conflict_csv,
        preview=conflict,
        row_overrides={0: {"include": True}},
    )
    assert rejected.status_code == 400
    assert "provider transaction ID conflicts" in rejected.json()["detail"]


@pytest.mark.parametrize("binding", ["file", "mapping", "row"])
def test_commit_decisions_are_bound_to_preview(client, biz_bank, binding):
    csv = "Date,Description,Credit\n2026-05-05,Bound statement,12.00\n"
    preview = _upload(client, biz_bank["id"], csv).json()
    changed_csv = csv.replace("12.00", "13.00") if binding == "file" else csv
    changed_mapping = dict(preview["mapping"])
    if binding == "mapping":
        changed_mapping["memo"] = None
    row_override = {0: {"include": True}}
    if binding == "row":
        row_override[0]["row_key"] = "f" * 64

    response = _commit_preview(
        client,
        biz_bank["id"],
        changed_csv,
        preview=preview,
        mapping_override=changed_mapping,
        row_overrides=row_override,
    )
    assert response.status_code == 400
    assert response.json()["detail"] in {
        "The uploaded file or column mapping differs from the preview",
        "Commit decisions do not match the previewed row identities",
    }


def test_commit_skips_row_duplicating_existing_manual_transaction(client, biz_bank):
    """A manual transaction match is surfaced for explicit review, never skipped."""
    m = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={
            "direction": "in",
            "amount": "5000.00",
            "occurred_at": "2026-07-01",
            "memo": "Acme Client Payment",
            "tax_code": "none",
        },
    )
    assert m.status_code == 201, m.text

    csv = (
        "Date,Description,Credit,Payee\n"
        "2026-07-01,Invoice payment received,5000.00,Acme Client Payment\n"
    )
    preview = _upload(client, biz_bank["id"], csv).json()
    assert preview["rows"][0]["requires_review"] is True
    commit = _commit_preview(
        client,
        biz_bank["id"],
        csv,
        preview=preview,
        row_overrides={0: {"include": True}},
    )
    assert commit.status_code == 200, commit.text
    assert commit.json() == {"created": 1, "skipped_duplicates": 0}


def test_commit_imports_two_distinct_same_day_same_payee_payments(client, biz_bank):
    """Distinct rows sharing amount/date/payee both retain their identities."""
    csv = (
        "Date,Description,Payee,Credit\n"
        "2026-07-01,Invoice 123,Acme Pty Ltd,100.00\n"
        "2026-07-01,Invoice 456,Acme Pty Ltd,100.00\n"
    )
    commit = _commit_preview(client, biz_bank["id"], csv)
    assert commit.status_code == 200, commit.text
    assert commit.json() == {"created": 2, "skipped_duplicates": 0}


def test_headerless_commbank_csv(client, biz_bank):
    """CommBank NetBank CSV export has NO header row and signed '+'/'-' amounts:
    Date, Amount, Description, Balance. The first transaction must not be eaten
    as a header, and '+5000.00' must parse as money-in."""
    csv = (
        '06/07/2026,"+5000.00","Fast Transfer From Someone","+5004.43"\n'
        '05/07/2026,"-180.00","Transfer to xx0000 CommBank app","+4.43"\n'
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    rows = [x for x in r.json()["rows"] if x.get("ok")]
    assert len(rows) == 2, r.json()  # first row kept, not consumed as a header
    first = rows[0]["parsed"]
    assert first["occurred_at"] == "2026-07-06"
    assert first["direction"] == "in"
    assert first["amount"] == "5000.00"
    assert "Fast Transfer" in (first["memo"] or "")
    assert rows[1]["parsed"]["direction"] == "out"


def test_pdf_statement_preview_and_commit(client, biz_bank):
    """A PDF statement flows through the same preview → commit pipeline, with a
    bank_format form field."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("Courier", 10)
    y = 800
    for ln in [
        "Commonwealth Bank - Transaction listing",
        "01/07/2026  Salary ACME PTY LTD   5,000.00   6,000.00",
        "03/07/2026  Rent payment          1,200.00   4,800.00",
    ]:
        c.drawString(40, y, ln)
        y -= 16
    c.save()
    pdf = buf.getvalue()

    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/preview",
        headers=HEAD,
        files={"file": ("statement.pdf", io.BytesIO(pdf), "application/pdf")},
        data={"bank_format": "auto"},
    )
    assert r.status_code == 200, r.text
    rows = [row for row in r.json()["rows"] if row.get("ok")]
    assert len(rows) == 2, r.json()
    dirs = {row["parsed"]["direction"] for row in rows}
    assert dirs == {"in", "out"}

    commit = _commit_preview(
        client,
        biz_bank["id"],
        pdf,
        preview=r.json(),
        filename="statement.pdf",
        bank_format="auto",
    )
    assert commit.status_code == 200, commit.text
    assert commit.json()["created"] == 2


def test_dedup_unique_index_exists_and_reimport_skips(client, biz_bank):
    from sqlalchemy import text

    from app.db.company import get_company_engine

    with get_company_engine("tc").connect() as conn:
        indexes = conn.execute(text("PRAGMA index_list(bank_transactions)")).fetchall()
    index_by_name = {row[1]: row for row in indexes}
    assert "uq_bank_txn_dedup" in index_by_name
    assert index_by_name["uq_bank_txn_dedup"][2] == 1

    csv = "Date,Description,Credit\n2026-05-01,Salary,5000.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]

    r = _commit_preview(client, biz_bank["id"], csv, preview=r.json())
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 1, "skipped_duplicates": 0}

    r = _commit_preview(
        client,
        biz_bank["id"],
        csv,
        preview=_upload(client, biz_bank["id"], csv).json(),
        import_mode="same_import",
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 0, "skipped_duplicates": 1}


def test_commit_ignores_empty_and_forged_client_dedup_keys(client, biz_bank):
    csv = (
        "Date,Description,Transaction ID,Credit\n"
        "2026-05-20,Server owned identity,txn-001,321.00\n"
    )
    preview = _upload(client, biz_bank["id"], csv).json()
    response = _commit_preview(client, biz_bank["id"], csv, preview=preview)
    assert response.status_code == 200, response.text
    assert response.json() == {"created": 1, "skipped_duplicates": 0}

    from app.db.company import company_session
    from app.models.company import BankTransaction

    with company_session("tc") as db:
        stored = (
            db.query(BankTransaction)
            .filter(BankTransaction.memo == "Server owned identity")
            .one()
        )
        assert stored.dedup_key is None
        assert stored.provider_transaction_id == "txn-001"
        assert stored.provider_namespace
        assert stored.import_statement_key == preview["import_statement_key"]
        assert stored.import_instance_id
        assert stored.import_row_key == preview["rows"][0]["row_key"]

    retry_preview = _upload(client, biz_bank["id"], csv).json()
    assert retry_preview["rows"][0]["is_duplicate"] is True
    retry = _commit_preview(
        client,
        biz_bank["id"],
        csv,
        preview=retry_preview,
    )
    assert retry.status_code == 200, retry.text
    assert retry.json() == {"created": 0, "skipped_duplicates": 1}


def test_commit_canonical_key_includes_counterparty(client, biz_bank):
    csv = (
        "Date,Description,Payee,Credit\n"
        "2026-05-21,Settlement,Customer Alpha,87.00\n"
        "2026-05-21,Settlement,Customer Beta,87.00\n"
    )
    preview = _upload(client, biz_bank["id"], csv).json()
    first = _commit_preview(client, biz_bank["id"], csv, preview=preview)
    assert first.status_code == 200, first.text
    assert first.json() == {"created": 2, "skipped_duplicates": 0}

    retry_preview = _upload(client, biz_bank["id"], csv).json()
    retry = _commit_preview(
        client,
        biz_bank["id"],
        csv,
        preview=retry_preview,
        import_mode="same_import",
    )
    assert retry.status_code == 200, retry.text
    assert retry.json() == {"created": 0, "skipped_duplicates": 2}


def test_concurrent_no_id_commits_cannot_create_second_instance(
    client, biz_bank, monkeypatch
):
    csv = (
        "Date,Description,Payee,Debit\n"
        "2026-05-22,Concurrent marker A,One Supplier,42.50\n"
        "2026-05-23,Concurrent marker B,Other Supplier,19.25\n"
    )
    preview = _upload(client, biz_bank["id"], csv).json()
    first_lock_acquired = threading.Event()
    second_at_begin = threading.Event()
    second_begin_returned = threading.Event()
    first_release_barrier = threading.Barrier(2)
    session_lock = threading.Lock()
    sessions = []

    from app.api.v1 import bank_accounts as bank_accounts_api
    from app.db.company import company_session

    begin_immediate = bank_accounts_api.begin_sqlite_immediate

    def independent_company_session():
        db = company_session("tc")
        try:
            yield db
        finally:
            db.rollback()
            db.close()

    monkeypatch.setitem(
        client.app.dependency_overrides,
        bank_accounts_api.get_company_db,
        independent_company_session,
    )

    def coordinated_begin(db):
        with session_lock:
            call_number = len(sessions)
            sessions.append(db)

        if call_number == 0:
            begin_immediate(db)
            first_lock_acquired.set()
            assert second_at_begin.wait(timeout=10), (
                "Second session did not reach begin_sqlite_immediate while "
                "the first transaction held the lock"
            )
            try:
                first_release_barrier.wait(timeout=10)
            except threading.BrokenBarrierError as exc:
                raise AssertionError(
                    "Test did not release the first transaction after the second "
                    "session reached BEGIN IMMEDIATE"
                ) from exc
            return

        assert first_lock_acquired.wait(timeout=10), (
            "Second session reached begin_sqlite_immediate before the first "
            "session acquired its transaction"
        )
        second_at_begin.set()
        begin_immediate(db)
        second_begin_returned.set()

    monkeypatch.setattr(
        bank_accounts_api, "begin_sqlite_immediate", coordinated_begin
    )

    def submit(request_client):
        return _commit_preview(
            request_client,
            biz_bank["id"],
            csv,
            preview=preview,
        )

    with TestClient(client.app) as second_client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(submit, client)
            second_future = None
            try:
                assert first_lock_acquired.wait(timeout=10), (
                    "First session did not acquire BEGIN IMMEDIATE"
                )
                second_future = pool.submit(submit, second_client)
                assert second_at_begin.wait(timeout=10), (
                    "Second session did not reach begin_sqlite_immediate"
                )
                assert not second_begin_returned.wait(timeout=0.25), (
                    "Second BEGIN IMMEDIATE returned while the first session "
                    "still held the SQLite write lock"
                )
            finally:
                if first_release_barrier.broken:
                    pass
                else:
                    try:
                        first_release_barrier.wait(timeout=10)
                    except threading.BrokenBarrierError:
                        first_release_barrier.abort()

            first_response = first_future.result(timeout=20)
            assert second_future is not None, (
                "Second commit was not started after first lock acquisition"
            )
            second_response = second_future.result(timeout=20)
            responses = [first_response, second_response]

    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]
    assert first_lock_acquired.is_set()
    assert second_at_begin.is_set()
    assert second_begin_returned.is_set()
    assert sorted(response.status_code for response in responses) == [200, 400], [
        response.text for response in responses
    ]
    successful = next(response for response in responses if response.status_code == 200)
    rejected = next(response for response in responses if response.status_code == 400)
    assert successful.json() == {"created": 2, "skipped_duplicates": 0}
    assert "Choose whether this is the same or an independent import" in rejected.json()["detail"]

    listed = client.get(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=HEAD,
    ).json()
    assert len(listed) == 2
    imported = [
        (
            txn["memo"],
            txn["occurred_at"],
            txn["direction"],
            txn["amount"],
            txn["counter_party_name"],
        )
        for txn in listed
        if txn["memo"] in {"Concurrent marker A", "Concurrent marker B"}
    ]
    expected_rows = [
        ("Concurrent marker A", "2026-05-22", "out", "42.50", "One Supplier"),
        ("Concurrent marker B", "2026-05-23", "out", "19.25", "Other Supplier"),
    ]
    assert len(imported) == 2
    for expected_row in expected_rows:
        assert imported.count(expected_row) == 1


def test_provider_namespace_is_stable_across_csv_xlsx_and_pdf_sources(
    client, biz_bank, monkeypatch
):
    from openpyxl import Workbook
    from app.services import bank_pdf

    headers = ["Date", "Description", "Transaction ID", "Credit"]
    alternate_headers = ["Transaction Date", "Narrative", "Reference ID", "Deposit"]
    values = ["2026-08-10", "Provider cross-format", "cross-format-1", "45.00"]
    csv = ",".join(headers) + "\n" + ",".join(values) + "\n"
    namespace = "open-accounting:generic-bank-import:v1"

    first = _upload(client, biz_bank["id"], csv).json()
    assert first["rows"][0]["provider_namespace"] == namespace
    committed = _commit_preview(client, biz_bank["id"], csv, preview=first)
    assert committed.status_code == 200, committed.text
    assert committed.json()["created"] == 1

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(alternate_headers)
    sheet.append(values)
    xlsx_buffer = io.BytesIO()
    workbook.save(xlsx_buffer)
    xlsx_response = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/preview",
        headers=HEAD,
        files={
            "file": (
                "renamed-source.xlsx",
                io.BytesIO(xlsx_buffer.getvalue()),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert xlsx_response.status_code == 200, xlsx_response.text
    xlsx_row = xlsx_response.json()["rows"][0]
    assert xlsx_row["provider_namespace"] == namespace
    assert xlsx_row["is_duplicate"] is True

    monkeypatch.setattr(
        bank_pdf,
        "parse_pdf",
        lambda _content, _bank_format: (alternate_headers, [values]),
    )
    pdf_response = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/preview",
        headers=HEAD,
        files={"file": ("other-bank.pdf", io.BytesIO(b"synthetic-pdf"), "application/pdf")},
        data={"bank_format": "westpac"},
    )
    assert pdf_response.status_code == 200, pdf_response.text
    pdf_row = pdf_response.json()["rows"][0]
    assert pdf_row["provider_namespace"] == namespace
    assert pdf_row["is_duplicate"] is True


def test_distinct_provider_namespaces_may_reuse_transaction_id(client, biz_bank):
    from app.db.company import company_session
    from app.models.company import BankTransaction

    with company_session("tc") as db:
        db.add(
            BankTransaction(
                bank_account_id=biz_bank["id"],
                direction="in",
                amount=Decimal("45.00"),
                occurred_at=date(2026, 8, 10),
                memo="Generic provider transaction",
                gst_amount=Decimal("0.00"),
                tax_code="none",
                unapplied_amount=Decimal("0.00"),
                provider_namespace="another-stable-provider:v1",
                provider_transaction_id="shared-transaction-id",
            )
        )
        db.commit()

    csv = (
        "Date,Description,Transaction ID,Credit\n"
        "2026-08-10,Generic provider transaction,shared-transaction-id,45.00\n"
    )
    preview = _upload(client, biz_bank["id"], csv).json()
    row = preview["rows"][0]
    assert row["provider_namespace"] == "open-accounting:generic-bank-import:v1"
    assert row["is_duplicate"] is False
    assert row["review_blocked"] is False
    assert row["requires_review"] is True

    committed = _commit_preview(
        client,
        biz_bank["id"],
        csv,
        preview=preview,
        row_overrides={0: {"include": True}},
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["created"] == 1

    with company_session("tc") as db:
        rows = (
            db.query(BankTransaction.provider_namespace)
            .filter(
                BankTransaction.provider_transaction_id == "shared-transaction-id"
            )
            .all()
        )
    assert {row[0] for row in rows} == {
        "another-stable-provider:v1",
        "open-accounting:generic-bank-import:v1",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", "not-a-number"),
        ("amount", "0"),
        ("amount", "-1.00"),
        ("amount", "1.001"),
        ("amount", "100000000000000.00"),
        ("gst_amount", "-0.01"),
        ("gst_amount", "1.001"),
        ("occurred_at", "not-a-date"),
        ("occurred_at", "1999-06-30"),
    ],
)
def test_commit_schema_rejects_bad_money_and_dates_without_500_or_row_echo(
    client, biz_bank, field, value,
):
    csv = "Date,Description,Credit\n2026-05-23,Bound row,10.00\n"
    preview = _upload(client, biz_bank["id"], csv).json()
    decision = {
        "row_key": preview["rows"][0]["row_key"],
        "include": True,
        "gst_amount": "0.00",
        field: value,
    }
    response = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        files={"file": ("stmt.csv", io.BytesIO(csv.encode()), "text/csv")},
        data={
            "payload_json": json.dumps(
                {
                    "preview_key": preview["preview_key"],
                    "mapping": preview["mapping"],
                    "import_mode": "new_import",
                    "rows": [decision],
                }
            )
        },
    )
    assert response.status_code == 422, response.text


def test_commit_business_error_contains_only_row_index_and_field_reason(
    client, biz_bank,
):
    csv = "Date,Description,Credit\n2026-05-24,Private memo,10.00\n"
    preview = _upload(client, biz_bank["id"], csv).json()
    response = _commit_preview(
        client,
        biz_bank["id"],
        csv,
        preview=preview,
        row_overrides={0: {"include": True, "gst_amount": "11.00"}},
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        "Row 1: gst_amount must not exceed amount"
    )
    assert "Private memo" not in response.text


# ---------------------------------------------------------------------------
# Rules CRUD + matching
# ---------------------------------------------------------------------------


def test_rule_crud_round_trip(client, accounts):
    rent = accounts["6100"]

    r = client.post("/api/v1/bank-rules", headers=HEAD, json={
        "priority": 50,
        "description": "Office rent → 6100",
        "match_direction": "out",
        "match_memo_regex": "(?i)rent",
        "set_account_id": rent["id"],
        "set_tax_code": "standard",
    })
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    r = client.get("/api/v1/bank-rules", headers=HEAD)
    assert len(r.json()) == 1

    r = client.patch(f"/api/v1/bank-rules/{rid}", headers=HEAD, json={"priority": 10})
    assert r.json()["priority"] == 10

    r = client.delete(f"/api/v1/bank-rules/{rid}", headers=HEAD)
    assert r.status_code == 204
    assert client.get("/api/v1/bank-rules", headers=HEAD).json() == []


def test_rule_patch_can_clear_nullable_match_predicates(client, accounts):
    rent = accounts["6100"]
    r = client.post("/api/v1/bank-rules", headers=HEAD, json={
        "description": "Clearable predicates",
        "match_direction": "out",
        "match_amount_min": "100",
        "match_amount_max": "200",
        "match_memo_regex": "(?i)rent",
        "match_counter_party_regex": "Landlord",
        "set_account_id": rent["id"],
    })
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    # Explicit null clears nullable predicates.  Clearing min in the same
    # request means max=50 is a valid range; omitted fields remain unchanged.
    r = client.patch(f"/api/v1/bank-rules/{rid}", headers=HEAD, json={
        "match_direction": None,
        "match_amount_min": None,
        "match_amount_max": "50",
        "match_memo_regex": None,
        "match_counter_party_regex": None,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["match_direction"] is None
    assert body["match_amount_min"] is None
    assert body["match_amount_max"] == "50.00"
    assert body["match_memo_regex"] is None
    assert body["match_counter_party_regex"] is None
    assert body["description"] == "Clearable predicates"


def test_rule_patch_rejects_null_for_required_columns(client, accounts):
    rent = accounts["6100"]
    r = client.post("/api/v1/bank-rules", headers=HEAD, json={
        "description": "Required fields",
        "set_account_id": rent["id"],
    })
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    for field in (
        "priority", "is_active", "description", "set_account_id", "set_tax_code"
    ):
        r = client.patch(
            f"/api/v1/bank-rules/{rid}", headers=HEAD, json={field: None}
        )
        assert r.status_code == 422, (field, r.text)


def test_rule_bad_amount_range_rejected(client, accounts):
    rent = accounts["6100"]
    r = client.post("/api/v1/bank-rules", headers=HEAD, json={
        "description": "Bad range",
        "match_amount_min": "100",
        "match_amount_max": "50",
        "set_account_id": rent["id"],
    })
    assert r.status_code == 400


def test_rule_matching_suggests_account_on_preview(client, accounts, biz_bank):
    rent = accounts["6100"]
    # Create rule: any OUT memo matching "rent" → account 6100 (Rent)
    client.post("/api/v1/bank-rules", headers=HEAD, json={
        "priority": 50,
        "description": "Office rent",
        "match_direction": "out",
        "match_memo_regex": "(?i)rent",
        "set_account_id": rent["id"],
        "set_tax_code": "standard",
    })

    csv = (
        "Date,Description,Debit,Credit\n"
        "2026-05-01,Office rent May,1500.00,\n"
        "2026-05-02,Random other,200.00,\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    rows = r.json()["rows"]
    assert rows[0]["suggested_account_id"] == rent["id"]
    assert rows[0]["matched_rule_description"] == "Office rent"
    assert rows[1]["suggested_account_id"] is None


def test_rule_matching_suggests_gst_amount_for_gst_bearing_tax_codes(client, accounts, biz_bank):
    sales = accounts["4000"]
    r = client.post("/api/v1/bank-rules", headers=HEAD, json={
        "priority": 50,
        "description": "Invoice payment",
        "match_direction": "in",
        "match_memo_regex": "SINV",
        "set_account_id": sales["id"],
        "set_tax_code": "standard",
    })
    assert r.status_code == 201, r.text

    csv = "Date,Description,Credit\n2024-07-05,Invoice payment SINV-001,110.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["suggested_account_id"] == sales["id"]
    assert row["suggested_tax_code"] == "standard"
    assert row["suggested_gst_amount"] == "10.00"
    assert row["suggestion_source"] == "rule"


def test_rule_matching_suggests_zero_gst_for_non_gst_tax_codes(client, accounts, biz_bank):
    other = accounts["6900"]
    tax_codes = ["gst_free", "input_taxed", "none"]
    for i, tax_code in enumerate(tax_codes):
        r = client.post("/api/v1/bank-rules", headers=HEAD, json={
            "priority": 50 + i,
            "description": f"No GST {tax_code}",
            "match_direction": "out",
            "match_memo_regex": f"NO_GST_{i}",
            "set_account_id": other["id"],
            "set_tax_code": tax_code,
        })
        assert r.status_code == 201, r.text

    csv = (
        "Date,Description,Debit\n"
        "2024-07-05,NO_GST_0 item,110.00\n"
        "2024-07-06,NO_GST_1 item,220.00\n"
        "2024-07-07,NO_GST_2 item,330.00\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert [row["suggested_tax_code"] for row in rows] == tax_codes
    assert [row["suggested_gst_amount"] for row in rows] == ["0.00", "0.00", "0.00"]


def test_preview_uses_memo_heuristics_when_no_rule_matches(client, accounts, biz_bank):
    csv = (
        "Date,Description,Debit,Credit\n"
        "2024-07-05,Telstra bill,110.00,\n"
        "2024-07-06,Officeworks receipt,55.00,\n"
        "2024-07-07,Rent payment,220.00,\n"
        "2024-07-08,Random transfer,10.00,\n"
        "2024-07-09,Payroll July,500.00,\n"
        "2024-07-10,Fresh morning tea,76.80,\n"
        "2024-07-11,Laptop purchase capital,2420.00,\n"
        "2024-07-12,Card settlement micro advisory,,990.00\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]

    assert rows[0]["suggested_account_id"] == accounts["6200"]["id"]
    assert rows[0]["suggested_tax_code"] == "standard"
    assert rows[0]["suggested_gst_amount"] == "10.00"
    assert rows[0]["suggestion_source"] == "heuristic"

    assert rows[1]["suggested_account_id"] == accounts["6400"]["id"]
    assert rows[1]["suggested_tax_code"] == "standard"
    assert rows[1]["suggestion_source"] == "heuristic"

    assert rows[2]["suggested_account_id"] == accounts["6100"]["id"]
    assert rows[2]["suggested_tax_code"] == "gst_free"
    assert rows[2]["suggested_gst_amount"] == "0.00"

    assert rows[3]["suggested_account_id"] is None
    assert rows[3]["suggestion_source"] is None

    assert rows[4]["suggested_account_id"] == accounts["6000"]["id"]
    assert rows[4]["suggested_tax_code"] == "none"
    assert rows[4]["suggested_gst_amount"] == "0.00"

    assert rows[5]["suggested_account_id"] == accounts["6400"]["id"]
    assert rows[5]["suggested_tax_code"] == "gst_free"

    assert rows[6]["suggested_account_id"] == accounts["1700"]["id"]
    assert rows[6]["suggested_tax_code"] == "capital"
    assert rows[6]["suggested_gst_amount"] == "220.00"

    assert rows[7]["suggested_account_id"] == accounts["4000"]["id"]
    assert rows[7]["suggested_tax_code"] == "standard"
    assert rows[7]["suggestion_source"] == "heuristic"


def test_inbound_client_payment_heuristic_stays_out_of_expense_accounts(client, accounts, biz_bank):
    csv = "Date,Description,Credit\n2024-07-12,Client payment invoice 123,3500.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["parsed"]["direction"] == "in"
    assert row["suggested_account_id"] == accounts["4000"]["id"]
    assert row["suggested_tax_code"] == "standard"
    assert row["suggestion_source"] == "heuristic"


def test_direction_unsafe_rule_is_ignored_before_bas(client, accounts, biz_bank):
    """A misconfigured automatic rule must not turn a customer receipt into a
    purchase refund. That cascades into negative G11/1B on BAS, so preview
    ignores direction-unsafe rule accounts and falls back to safe heuristics."""
    travel = accounts["6310"]
    sales = accounts["4000"]
    r = client.post("/api/v1/bank-rules", headers=HEAD, json={
        "priority": 1,
        "description": "Bad inbound client rule",
        "match_direction": "in",
        "match_memo_regex": "(?i)client payment",
        "set_account_id": travel["id"],
        "set_tax_code": "standard",
    })
    assert r.status_code == 201, r.text

    csv = "Date,Description,Credit\n2026-05-10,Client payment invoice 123,1100.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["parsed"]["direction"] == "in"
    assert row["suggested_account_id"] == sales["id"]
    assert row["suggested_tax_code"] == "standard"
    assert row["suggested_gst_amount"] == "100.00"
    assert row["suggestion_source"] == "heuristic"

    r = _commit_preview(
        client,
        biz_bank["id"],
        csv,
        preview=r.json(),
        row_overrides={
            0: {
                "account_id": row["suggested_account_id"],
                "tax_code": row["suggested_tax_code"],
                "gst_amount": row["suggested_gst_amount"],
            }
        },
    )
    assert r.status_code == 200, r.text

    r = client.get(
        "/api/v1/reports/bas",
        headers=HEAD,
        params={"fy_year": 2026, "quarter": 4},
    )
    assert r.status_code == 200, r.text
    bas = r.json()
    assert Decimal(bas["g1_total_sales"]) == Decimal("1100.00")
    assert Decimal(bas["one_a_gst_on_sales"]) == Decimal("100.00")
    assert Decimal(bas["total_purchases"]) == Decimal("0.00")
    assert Decimal(bas["one_b_gst_on_purchases"]) == Decimal("0.00")
    assert Decimal(bas["net_gst_payable"]) == Decimal("100.00")


def test_counter_party_header_with_hyphen_imports_and_persists(client, biz_bank):
    csv = "Date,Description,Counter-party,Credit\n2024-07-12,Invoice 123,Jane Sample,3500.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mapping"]["counter_party_name"] is not None
    row = body["rows"][0]
    assert row["parsed"]["counter_party_name"] == "Jane Sample"

    r = _commit_preview(
        client,
        biz_bank["id"],
        csv,
        preview=body,
        row_overrides={
            0: {
                "account_id": row["suggested_account_id"],
                "tax_code": row["suggested_tax_code"],
                "gst_amount": row["suggested_gst_amount"] or "0.00",
            }
        },
    )
    assert r.status_code == 200, r.text

    r = client.get(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=HEAD,
    )
    assert r.status_code == 200, r.text
    assert r.json()[0]["counter_party_name"] == "Jane Sample"


def test_rule_priority_wins(client, accounts, biz_bank):
    rent = accounts["6100"]
    other = accounts["6900"]   # Other Expenses
    # Two rules both match "rent"; lower priority should win.
    client.post("/api/v1/bank-rules", headers=HEAD, json={
        "priority": 100,
        "description": "Catch-all OUT → Other Expenses",
        "match_direction": "out",
        "set_account_id": other["id"],
    })
    client.post("/api/v1/bank-rules", headers=HEAD, json={
        "priority": 10,
        "description": "Rent → 6100",
        "match_direction": "out",
        "match_memo_regex": "(?i)rent",
        "set_account_id": rent["id"],
    })

    csv = "Date,Description,Debit,Credit\n2026-05-01,Office rent,1500.00,\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.json()["rows"][0]["suggested_account_id"] == rent["id"]


def test_commit_applies_account_and_tax_code(client, accounts, biz_bank):
    rent = accounts["6100"]
    csv = "Date,Description,Debit,Credit\n2026-05-01,Office rent,1500.00,\n"
    r = _upload(client, biz_bank["id"], csv)
    rows = r.json()["rows"]
    commit = _commit_preview(
        client,
        biz_bank["id"],
        csv,
        preview=r.json(),
        row_overrides={
            0: {
                "account_id": rent["id"],
                "tax_code": "standard",
                "gst_amount": "136.36",
            }
        },
    )
    assert commit.json()["created"] == 1
    # Verify the txn lands with all the fields wired.
    listed = client.get(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=HEAD,
    ).json()
    assert len(listed) == 1
    txn = listed[0]
    assert txn["account_id"] == rent["id"]
    assert txn["tax_code"] == "standard"
    assert Decimal(txn["gst_amount"]) == Decimal("136.36")


def test_default_bank_account_maps_to_operating_cash_ledger(client, biz_bank):
    accounts = client.get("/api/v1/accounts", headers=HEAD).json()
    ledger = next(account for account in accounts if account["code"] == "1000")
    assert ledger["active"] is True
    assert ledger["type"] == "ASSET"
    assert ledger["is_gst"] is False
    assert biz_bank["ledger_account_id"] == ledger["id"]


def test_create_bank_account_happy_path_and_list(client):
    ledger = client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={"code": "1010", "name": "Savings cash", "type": "ASSET"},
    ).json()
    r = client.post(
        "/api/v1/bank-accounts",
        headers=HEAD,
        json={
            "name": "NAB savings",
            "ledger_account_id": ledger["id"],
            "opening_balance": "20000.00",
            "bsb": "082-001",
            "account_number": "123456789",
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["name"] == "NAB savings"
    assert Decimal(created["opening_balance"]) == Decimal("20000.00")
    assert created["ledger_account_id"] == ledger["id"]

    listed = client.get("/api/v1/bank-accounts", headers=HEAD).json()
    assert any(a["id"] == created["id"] for a in listed)


def test_create_bank_account_duplicate_name_returns_409(client):
    ledger = client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={"code": "1010", "name": "Savings cash", "type": "ASSET"},
    ).json()
    payload = {"name": "ANZ receivables", "ledger_account_id": ledger["id"]}
    first = client.post("/api/v1/bank-accounts", headers=HEAD, json=payload)
    assert first.status_code == 201, first.text

    duplicate = client.post("/api/v1/bank-accounts", headers=HEAD, json=payload)
    assert duplicate.status_code == 409, duplicate.text


def test_bank_account_requires_a_valid_unique_ledger_mapping(client, biz_bank):
    missing = client.post(
        "/api/v1/bank-accounts", headers=HEAD, json={"name": "Missing mapping"}
    )
    assert missing.status_code == 422

    accounts = {
        account["code"]: account
        for account in client.get("/api/v1/accounts", headers=HEAD).json()
    }
    invalid = client.post(
        "/api/v1/bank-accounts",
        headers=HEAD,
        json={"name": "Protected mapping", "ledger_account_id": accounts["1100"]["id"]},
    )
    assert invalid.status_code == 400

    reused = client.post(
        "/api/v1/bank-accounts",
        headers=HEAD,
        json={
            "name": "Reused mapping",
            "ledger_account_id": biz_bank["ledger_account_id"],
        },
    )
    assert reused.status_code == 409


def test_bank_account_rejects_inactive_non_asset_and_gst_ledgers(client):
    inactive = client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={"code": "1010", "name": "Inactive cash", "type": "ASSET"},
    ).json()
    client.patch(
        f"/api/v1/accounts/{inactive['id']}",
        headers=HEAD,
        json={"active": False},
    )
    expense = next(
        account
        for account in client.get("/api/v1/accounts", headers=HEAD).json()
        if account["code"] == "6100"
    )
    gst = client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={
            "code": "1020",
            "name": "GST cash",
            "type": "ASSET",
            "is_gst": True,
        },
    ).json()

    for name, ledger_id in (
        ("Inactive mapping", inactive["id"]),
        ("Non-asset mapping", expense["id"]),
        ("GST mapping", gst["id"]),
        ("Missing ledger", 999999),
    ):
        response = client.post(
            "/api/v1/bank-accounts",
            headers=HEAD,
            json={"name": name, "ledger_account_id": ledger_id},
        )
        assert response.status_code == 400, (name, response.text)


def test_legacy_unresolved_bank_can_be_assigned_but_not_cleared(client, biz_bank):
    from app.db.company import company_session
    from app.models.company import BankAccount

    ledger = client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={"code": "1010", "name": "Savings cash", "type": "ASSET"},
    ).json()
    with company_session("tc") as db:
        bank = db.get(BankAccount, biz_bank["id"])
        bank.ledger_account_id = None
        db.commit()

    assigned = client.patch(
        f"/api/v1/bank-accounts/{biz_bank['id']}",
        headers=HEAD,
        json={"ledger_account_id": ledger["id"]},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["ledger_account_id"] == ledger["id"]

    cleared = client.patch(
        f"/api/v1/bank-accounts/{biz_bank['id']}",
        headers=HEAD,
        json={"ledger_account_id": None},
    )
    assert cleared.status_code == 422


def test_patch_bank_account_renames_and_updates_details(client, biz_bank):
    r = client.patch(
        f"/api/v1/bank-accounts/{biz_bank['id']}",
        headers=HEAD,
        json={"name": "CBA business cheque", "bsb": "062-001", "account_number": "987654321"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "CBA business cheque"
    assert body["bsb"] == "062-001"
    assert body["account_number"] == "987654321"


def test_commit_rejects_missing_or_inactive_account(client, accounts, biz_bank):
    rent = accounts["6100"]
    csv = "Date,Description,Debit,Credit\n2026-05-01,Office rent,1500.00,\n"
    row = _upload(client, biz_bank["id"], csv).json()["rows"][0]

    r = _commit_preview(
        client,
        biz_bank["id"],
        csv,
        preview=_upload(client, biz_bank["id"], csv).json(),
        row_overrides={0: {"include": True, "account_id": 999999}},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "Row 1: account_id is missing or inactive"

    client.patch(
        f"/api/v1/accounts/{rent['id']}",
        headers=HEAD,
        json={"active": False},
    )
    r = _commit_preview(
        client,
        biz_bank["id"],
        csv,
        preview=_upload(client, biz_bank["id"], csv).json(),
        row_overrides={0: {"include": True, "account_id": rent["id"]}},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "Row 1: account_id is missing or inactive"


def test_commit_skips_duplicate_rows_inside_same_payload(client, biz_bank):
    csv = (
        "Date,Description,Credit\n"
        "2026-05-01,Salary,5000.00\n"
        "2026-05-01,Salary,5000.00\n"
    )
    r = _commit_preview(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 2, "skipped_duplicates": 0}


def test_inactive_bank_account_rejects_manual_entry_and_import(client, biz_bank):
    csv = "Date,Description,Credit\n2026-05-01,Salary,5000.00\n"
    prior_preview = _upload(client, biz_bank["id"], csv).json()
    r = client.patch(
        f"/api/v1/bank-accounts/{biz_bank['id']}",
        headers=HEAD,
        json={"is_active": False},
    )
    assert r.status_code == 200, r.text

    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={
            "direction": "in",
            "amount": "100.00",
            "occurred_at": "2026-05-01",
            "memo": "Late entry",
        },
    )
    assert r.status_code == 400
    assert "inactive" in r.json()["detail"]

    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 400
    assert "inactive" in r.json()["detail"]

    r = _commit_preview(
        client,
        biz_bank["id"],
        csv,
        preview=prior_preview,
    )
    assert r.status_code == 400
    assert "inactive" in r.json()["detail"]


# --- Unit: amount parser edge cases (BUG-F6 parenthesised negatives, BUG-B7 sci notation) ---


def test_parse_decimal_edge_cases():
    from app.services.excel_import import _parse_decimal

    # Accounting-style negatives.
    assert _parse_decimal("(250.00)") == "-250.00"
    assert _parse_decimal("(1,234.50)") == "-1234.50"
    # Currency symbols + thousands separators still work.
    assert _parse_decimal("$1,000.00") == "1000.00"
    assert _parse_decimal("-500") == "-500.00"
    # Scientific notation is rejected (never a real bank amount).
    assert _parse_decimal("1e5") is None
    assert _parse_decimal("1E5") is None
    # Garbage / malformed.
    assert _parse_decimal("abc") is None
    assert _parse_decimal("1.2.3") is None
    # Empty / blank.
    assert _parse_decimal("") is None
    assert _parse_decimal(None) is None
