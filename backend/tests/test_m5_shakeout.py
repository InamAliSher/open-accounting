"""M5 realistic-data shakeout invariants.

The fixture seeds a brand-new company through the same public HTTP API used by
the standalone seed script, then the tests re-check the cross-report contracts
that tend to drift under realistic volume.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

from backend.scripts import seed_realistic  # noqa: E402


HEAD = {"X-Company-Id": seed_realistic.COMPANY_ID}


def D(value: str | int | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    test_data = tmp_path_factory.mktemp("m5_shakeout")
    os.environ["DATA_DIR"] = str(test_data)
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    from app.main import app

    with TestClient(app) as client:
        api = seed_realistic.Api(client)

        def preview_and_commit(api, bank_id, content, filename, account_by_code, desired):
            preview = api.post(
                f"/api/v1/bank-accounts/{bank_id}/import/preview",
                files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
            ).json()
            desired_by_key = {
                (
                    seed_realistic.iso(row["occurred_at"]),
                    row["direction"],
                    str(row["amount"]),
                    row["memo"],
                ): row
                for row in desired
            }
            decisions = []
            for row in preview["rows"]:
                if not row["ok"]:
                    raise RuntimeError(f"Bank import row failed: {row}")
                parsed = row["parsed"]
                source = desired_by_key[
                    (
                        parsed["occurred_at"],
                        parsed["direction"],
                        parsed["amount"],
                        parsed["memo"],
                    )
                ]
                tax_code = source["tax_code"]
                decisions.append(
                    {
                        "row_key": row["row_key"],
                        "include": True,
                        "account_id": account_by_code[source["account_code"]]["id"],
                        "tax_code": tax_code,
                        "gst_amount": str(
                            seed_realistic.gst_from_gross(
                                Decimal(parsed["amount"]), tax_code
                            )
                        ),
                    }
                )
            mode = (
                "same_import"
                if preview["statement_review_required"]
                else "new_import"
            )
            payload = {
                "preview_key": preview["preview_key"],
                "mapping": preview["mapping"],
                "import_mode": mode,
                "rows": decisions,
            }
            return api.post(
                f"/api/v1/bank-accounts/{bank_id}/import/commit",
                files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
                data={"payload_json": json.dumps(payload)},
            ).json()

        original_preview_and_commit = seed_realistic.preview_and_commit
        seed_realistic.preview_and_commit = preview_and_commit
        try:
            summary = seed_realistic.seed(api, reset=False, exercise=False)
        finally:
            seed_realistic.preview_and_commit = original_preview_and_commit
        HEAD["X-Company-Generation"] = summary["company_generation"]
        yield {"client": client, "summary": summary, "data_dir": test_data}


def _retained_earnings(bs: dict) -> Decimal:
    total = Decimal("0.00")
    for group in bs["equity"]:
        if group["label"] == "Retained earnings":
            total += sum(D(line["balance"]) for line in group["lines"])
    return D(total)


def _company_db_path(seeded, company_id: str = seed_realistic.COMPANY_ID) -> Path:
    return seeded["data_dir"] / "companies" / company_id / "books.db"


def _accounts_by_code(client, headers: dict[str, str]) -> dict[str, dict]:
    r = client.get("/api/v1/accounts", headers=headers)
    assert r.status_code == 200, r.text
    return {a["code"]: a for a in r.json()}


def test_seed_volume_targets_are_met(seeded):
    counts = seeded["summary"]["row_counts"]
    assert counts["bank_transactions"] >= 24 * 30
    assert counts["invoices"] >= 24 * 15
    assert counts["outgoing_documents"] >= 24 * 5
    assert counts["journal_entries"] >= 1 + 24
    assert 8 <= counts["bank_rules"] <= 15


@pytest.mark.parametrize("as_of", ["2024-06-30", "2025-06-30"])
def test_trial_balance_balances_at_fy_ends(seeded, as_of):
    r = seeded["client"].get("/api/v1/reports/trial-balance", headers=HEAD, params={"as_of": as_of})
    assert r.status_code == 200, r.text
    body = r.json()
    assert D(body["total_debit"]) == D(body["total_credit"])
    assert D(body["diff"]) == Decimal("0.00")
    assert body["is_balanced"] is True


@pytest.mark.parametrize("as_of", ["2024-06-30", "2025-06-30"])
def test_balance_sheet_equation_at_fy_ends(seeded, as_of):
    r = seeded["client"].get("/api/v1/reports/balance-sheet", headers=HEAD, params={"as_of": as_of})
    assert r.status_code == 200, r.text
    body = r.json()
    assert D(body["total_assets"]) == D(D(body["total_liabilities"]) + D(body["total_equity"]))
    assert D(body["diff"]) == Decimal("0.00")
    assert body["is_balanced"] is True


def test_profit_and_loss_ties_to_retained_earnings(seeded):
    client = seeded["client"]
    pnl_2024 = client.get(
        "/api/v1/reports/profit-loss",
        headers=HEAD,
        params={"period_start": "2023-07-01", "period_end": "2024-06-30"},
    ).json()
    bs_before = client.get("/api/v1/reports/balance-sheet", headers=HEAD, params={"as_of": "2023-06-30"}).json()
    bs_2024 = client.get("/api/v1/reports/balance-sheet", headers=HEAD, params={"as_of": "2024-06-30"}).json()
    assert D(pnl_2024["net_profit"]) == D(_retained_earnings(bs_2024) - _retained_earnings(bs_before))

    pnl_2025 = client.get(
        "/api/v1/reports/profit-loss",
        headers=HEAD,
        params={"period_start": "2024-07-01", "period_end": "2025-06-30"},
    ).json()
    bs_2025 = client.get("/api/v1/reports/balance-sheet", headers=HEAD, params={"as_of": "2025-06-30"}).json()
    assert D(pnl_2025["net_profit"]) == D(_retained_earnings(bs_2025) - _retained_earnings(bs_2024))


@pytest.mark.parametrize("fy_year,quarter", [(2024, 1), (2024, 2), (2024, 3), (2024, 4), (2025, 1), (2025, 2), (2025, 3), (2025, 4)])
def test_bas_net_gst_matches_gst_exposure(seeded, fy_year, quarter):
    client = seeded["client"]
    bas = client.get("/api/v1/reports/bas", headers=HEAD, params={"fy_year": fy_year, "quarter": quarter}).json()
    gst = client.get(
        "/api/v1/reports/gst-exposure",
        headers=HEAD,
        params={"fy_year": fy_year, "quarter": quarter},
    ).json()
    assert D(D(bas["one_a_gst_on_sales"]) - D(bas["one_b_gst_on_purchases"])) == D(gst["net_gst_payable"])


def test_bank_balances_match_statement_running_totals(seeded):
    client = seeded["client"]
    banks = client.get("/api/v1/bank-accounts", headers=HEAD).json()
    months = [(y, m) for y in [2023, 2024, 2025] for m in range(1, 13) if (y, m) >= (2023, 7) and (y, m) <= (2025, 6)]
    for bank in banks:
        closing = Decimal("0.00")
        saw_month = False
        for y, m in months:
            stmt = client.get(
                "/api/v1/reports/bank-statement",
                headers=HEAD,
                params={"bank_account_id": bank["id"], "year": y, "month": m},
            ).json()
            if not saw_month:
                closing = D(stmt["opening_balance"])
                saw_month = True
            closing += D(stmt["net_change"])
        assert D(bank["current_balance"]) == D(closing)


def test_reimport_duplicate_csv_creates_zero_rows(seeded):
    client = seeded["client"]
    bank = client.get("/api/v1/bank-accounts", headers=HEAD).json()[0]
    csv_bytes = (
        b"Date,Description,Transaction ID,Credit\n"
        b"2025-05-17,Synthetic identity check,m5-provider-001,123.00\n"
    )

    def preview_statement():
        response = client.post(
            f"/api/v1/bank-accounts/{bank['id']}/import/preview",
            headers=HEAD,
            files={"file": ("synthetic.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert response.status_code == 200, response.text
        return response.json()

    def commit_statement(preview):
        payload = {
            "preview_key": preview["preview_key"],
            "mapping": preview["mapping"],
            "import_mode": "new_import",
            "rows": [
                {
                    "row_key": row["row_key"],
                    "include": row["ok"] and not row["is_duplicate"],
                    "account_id": None,
                    "tax_code": "none",
                    "gst_amount": "0.00",
                }
                for row in preview["rows"]
            ],
        }
        return client.post(
            f"/api/v1/bank-accounts/{bank['id']}/import/commit",
            headers=HEAD,
            files={"file": ("synthetic.csv", io.BytesIO(csv_bytes), "text/csv")},
            data={"payload_json": json.dumps(payload)},
        )

    first = commit_statement(preview_statement())
    assert first.status_code == 200, first.text
    assert first.json()["created"] == 1

    repeated = preview_statement()
    assert repeated["has_provider_ids"] is True
    assert repeated["rows"][0]["is_duplicate"] is True
    second = commit_statement(repeated)
    assert second.status_code == 200, second.text
    assert second.json() == {"created": 0, "skipped_duplicates": 1}


def test_tax_code_enforcement_has_no_positive_gst_on_no_gst_codes(seeded):
    db_path = seeded["data_dir"] / "companies" / seed_realistic.COMPANY_ID / "books.db"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, tax_code, gst_amount
            FROM bank_transactions
            WHERE tax_code IN ('gst_free', 'input_taxed', 'none')
              AND CAST(gst_amount AS NUMERIC) > 0
            """
        ).fetchall()
    assert rows == []


def test_bank_rule_gst_free_match_commits_zero_gst(seeded):
    db_path = seeded["data_dir"] / "companies" / seed_realistic.COMPANY_ID / "books.db"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT gst_amount
            FROM bank_transactions
            WHERE counter_party_name = 'Café Niño Supplies'
              AND tax_code = 'gst_free'
            """
        ).fetchall()
    assert rows
    assert all(D(row[0]) == Decimal("0.00") for row in rows)

def test_m5_finding_2_bas_reports_company_gst_registered_flag(seeded):
    bas = seeded["client"].get(
        "/api/v1/reports/bas",
        headers=HEAD,
        params={"fy_year": 2025, "quarter": 4},
    ).json()
    assert bas["gst_registered"] is True, (
        "M5 finding #2: BAS must report gst_registered=True for M5 Shakeout Co Pty Ltd; "
        f"endpoint returned {bas['gst_registered']!r}"
    )


def test_m5_finding_3_invoice_list_returns_every_invoice(seeded):
    client = seeded["client"]
    api_rows = client.get("/api/v1/invoices", headers=HEAD).json()
    with sqlite3.connect(_company_db_path(seeded)) as conn:
        db_count = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    assert len(api_rows) == db_count, (
        "M5 finding #3: GET /api/v1/invoices silently caps at 500 rows; "
        f"API returned {len(api_rows)} rows but invoices table has {db_count}"
    )


def test_m5_finding_3_bank_transaction_list_returns_every_transaction_for_account(seeded):
    client = seeded["client"]
    bank = client.get("/api/v1/bank-accounts", headers=HEAD).json()[0]
    api_rows = client.get(f"/api/v1/bank-accounts/{bank['id']}/transactions", headers=HEAD).json()
    with sqlite3.connect(_company_db_path(seeded)) as conn:
        db_count = conn.execute("SELECT COUNT(*) FROM bank_transactions WHERE bank_account_id = ?", (bank["id"],)).fetchone()[0]
    assert len(api_rows) == db_count, (
        "M5 finding #3: GET /api/v1/bank-accounts/{id}/transactions silently caps at 500 rows; "
        f"API returned {len(api_rows)} rows for bank account {bank['id']} but table has {db_count}"
    )



def test_m5_finding_5_bas_pdf_is_byte_identical_across_renders(seeded):
    """Two back-to-back BAS PDF renders for the same quarter must produce
    identical bytes (no UUID-seeded /ID, no time-varying internal metadata).

    Note: this only covers the within-same-minute case. The on-page footer
    text includes a HH:MM stamp from _footer_text(), so renders that
    straddle a minute boundary will legitimately differ. That's accepted
    as by-design: callers who need cross-minute determinism should compare
    parsed content, not raw bytes.
    """
    client = seeded["client"]
    p1 = client.get("/api/v1/reports/bas/pdf", headers=HEAD, params={"fy_year": 2025, "quarter": 4}).content
    p2 = client.get("/api/v1/reports/bas/pdf", headers=HEAD, params={"fy_year": 2025, "quarter": 4}).content
    assert p1 == p2, (
        "M5 finding #5: BAS PDF renders are not byte-deterministic — "
        f"len p1={len(p1)} len p2={len(p2)}; first diff at byte "
        f"{next((i for i, (a, b) in enumerate(zip(p1, p2)) if a != b), 'no-diff')}"
    )

