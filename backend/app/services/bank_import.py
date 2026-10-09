"""Bank statement import service (M3).

Two-step pipeline:

  1. Preview parses a statement and returns identity/review status and rule
      suggestions without writing.
  2. Commit reparses the uploaded bytes, verifies preview-bound choices, and
      writes accepted rows atomically.

Supported formats: CSV, XLSX, and PDF. Column mapping remains server-derived
and is bound to the preview decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy.orm import Session

from ..models.company import (
    Account,
    AccountType,
    BankAccount,
    BankRule,
    BankTransaction,
    BankTxnDirection,
    TaxCode,
)
from .excel_import import (
    _cell_to_str,
    _normalise,
    _parse_date,
    _parse_decimal,
    _read_csv,
    _read_xlsx,
)
from .bank_accounts import (
    BankTxnError,
    InvoicePaymentWouldDoubleCount,
    reject_control_category_for_void_invoice,
    reject_capital_tax_code_on_control_account,
    reject_expense_category_for_matching_ap_payment,
    reject_income_category_for_matching_ar_payment,
)
from . import gst_policy, invoice_payments
from ..schemas.bank import check_txn_date
from ..schemas._limits import SQLITE_EXACT_MONEY_MAX


# ---------------------------------------------------------------------------
# Header heuristics
# ---------------------------------------------------------------------------


BANK_FIELDS = [
    "occurred_at",   # required
    "memo",          # narrative / description / details
    "counter_party_name",
    "provider_transaction_id",
    "amount",        # signed; positive = IN
    "debit",         # unsigned OUT
    "credit",        # unsigned IN
]


_HINTS: dict[str, list[str]] = {
    "occurred_at": ["date", "transaction date", "posted", "value date", "日期"],
    "memo": ["description", "narrative", "details", "memo", "particulars", "摘要"],
    "counter_party_name": [
        "payee",
        "payer",
        "merchant",
        "counterparty",
        "counter party",
        "counter-party",
        "counter party name",
        "counter-party name",
        "counter_party",
        "counter_party_name",
        "对方",
    ],
    "provider_transaction_id": [
        "transaction id",
        "transaction identifier",
        "transaction reference",
        "transaction ref",
        "reference id",
        "bank transaction id",
    ],
    "amount": ["amount", "value", "金额"],
    "debit": ["debit", "withdrawal", "out", "支出", "借方"],
    "credit": ["credit", "deposit", "in", "收入", "贷方"],
}


_SUBSTRING_FIELD_ORDER = [
    "occurred_at",
    "memo",
    "counter_party_name",
    "provider_transaction_id",
    "debit",
    "credit",
    "amount",
]

# Bare 2-3 letter ASCII hints ("in", "out") must match a whole word in the
# header, never a substring: with debit/credit resolved before amount, a raw
# substring "in" ⊂ "incl"/"spending" would let credit steal a signed
# "Amount (incl GST)" or unsigned "Spending Amount" column and flip every
# row's direction. Longer hints and CJK hints keep plain substring matching.
_SHORT_ASCII_HINT_LEN = 3


def _hint_matches(hint: str, header: str) -> bool:
    if len(hint) > _SHORT_ASCII_HINT_LEN or not hint.isascii():
        return hint in header
    return hint in re.split(r"[^a-z0-9]+", header)


def propose_mapping(headers: list[str]) -> dict[str, int | None]:
    norm = [_normalise(h) for h in headers]
    mapping: dict[str, int | None] = {f: None for f in BANK_FIELDS}
    used: set[int] = set()
    # Pass 1: exact
    for field, hints in _HINTS.items():
        for i, h in enumerate(norm):
            if i in used or not h:
                continue
            if any(hint == h for hint in hints):
                mapping[field] = i
                used.add(i)
                break
    # Pass 2: substring
    for field in _SUBSTRING_FIELD_ORDER:
        hints = _HINTS[field]
        if mapping[field] is not None:
            continue
        for i, h in enumerate(norm):
            if i in used or not h:
                continue
            if any(_hint_matches(hint, h) for hint in hints):
                mapping[field] = i
                used.add(i)
                break
    return mapping


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _looks_like_transaction_row(cells: list[Any]) -> bool:
    """A row is data (not a header) when its first cell is a date and another
    cell is a money amount — used to detect header-less CSV exports."""
    if not cells:
        return False
    strs = [_cell_to_str(c) for c in cells]
    return _parse_date(strs[0]) is not None and any(
        _parse_decimal(c) is not None for c in strs[1:]
    )


def _synthesize_headers(row: list[Any]) -> list[str]:
    """Name the columns of a header-less CSV by cell shape. CommBank's NetBank
    export is Date, Amount(signed), Description, Balance with no header row."""
    headers: list[str] = []
    money_seen = 0
    for i, cell in enumerate(row):
        s = _cell_to_str(cell)
        if _parse_date(s) is not None and "Date" not in headers:
            headers.append("Date")
        elif _parse_decimal(s) is not None:
            headers.append("Amount" if money_seen == 0 else "Balance")
            money_seen += 1
        elif "Description" not in headers:
            headers.append("Description")
        else:
            headers.append(f"col{i}")
    return headers


def parse_statement(
    *,
    content: bytes,
    filename: str,
    bank_format: str | None = None,
    mapping: dict[str, int | None] | None = None,
) -> dict[str, Any]:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in {"xlsx", "xlsm"}:
        headers, data_rows = _read_xlsx(content)
    elif ext == "csv":
        headers, data_rows = _read_csv(content)
        # Header-less export (e.g. CommBank NetBank CSV): the "header" row read
        # above is actually the first transaction — put it back and name the
        # columns by shape so the mapping/preview works.
        if _looks_like_transaction_row(headers):
            data_rows = [headers, *data_rows]
            headers = _synthesize_headers(headers)
    elif ext == "pdf":
        # Deterministic PDF extraction (pdfplumber/pypdf) → per-bank parser →
        # the same (headers, data_rows) shape as CSV/XLSX. `bank_format` picks a
        # bank; None auto-detects. No AI (the old Ollama pdf_extract stays gone).
        from .bank_pdf import parse_pdf

        headers, data_rows = parse_pdf(content, bank_format)
    else:
        raise ValueError(f"Unsupported format: .{ext} (use .csv, .xlsx or .pdf)")

    mapping = propose_mapping(headers) if mapping is None else _validate_mapping(mapping, headers)
    rows_out: list[dict[str, Any]] = []
    for i, raw in enumerate(data_rows, start=2):
        if all(c is None or (isinstance(c, str) and not c.strip()) for c in raw):
            continue
        rows_out.append({
            "row_no": i,
            "cells": [_cell_to_str(c) for c in raw],
            "raw": list(raw),
        })

    return {
        "headers": headers,
        "mapping": mapping,
        "rows": rows_out,
        "field_options": BANK_FIELDS,
    }


def _validate_mapping(
    mapping: dict[str, int | None], headers: list[str]
) -> dict[str, int | None]:
    if set(mapping) != set(BANK_FIELDS):
        raise ValueError("Column mapping does not match the previewed fields")
    validated: dict[str, int | None] = {}
    for field in BANK_FIELDS:
        index = mapping[field]
        if index is not None and (
            type(index) is not int or index < 0 or index >= len(headers)
        ):
            raise ValueError("Column mapping references an invalid column")
        validated[field] = index
    return validated


def _row_to_txn_shape(
    raw: list[Any], mapping: dict[str, int | None]
) -> dict[str, Any]:
    """Materialise one raw row into a tentative BankTransaction shape.

    Direction logic:
      - If both debit and credit columns are mapped, use whichever is non-zero.
      - Else if a signed amount column is mapped, positive = IN.

    `amount` in the returned dict is always positive (the BankTransaction
    convention); `direction` carries the sign.
    """
    def cell(field: str) -> Any:
        idx = mapping.get(field)
        if idx is None or idx >= len(raw):
            return None
        return raw[idx]

    occurred_at = _parse_date(cell("occurred_at"))
    memo = _cell_to_str(cell("memo")) or None
    counter = _cell_to_str(cell("counter_party_name")) or None
    provider_transaction_id = _cell_to_str(cell("provider_transaction_id")) or None

    debit_raw = cell("debit")
    credit_raw = cell("credit")
    amount_raw = cell("amount")

    direction: str | None = None
    amount: Decimal | None = None

    debit = _parse_decimal(debit_raw) if debit_raw not in (None, "") else None
    credit = _parse_decimal(credit_raw) if credit_raw not in (None, "") else None
    signed = _parse_decimal(amount_raw) if amount_raw not in (None, "") else None

    # A single row with BOTH a debit and a credit is ambiguous — we can't tell
    # the direction, so flag it rather than silently pick one and drop the other.
    ambiguous = bool(debit and Decimal(debit) > 0 and credit and Decimal(credit) > 0)

    if debit and Decimal(debit) > 0:
        direction = "out"
        amount = Decimal(debit)
    elif credit and Decimal(credit) > 0:
        direction = "in"
        amount = Decimal(credit)
    elif signed:
        d = Decimal(signed)
        if d > 0:
            direction = "in"
            amount = d
        elif d < 0:
            direction = "out"
            amount = -d
        # zero → skip; falls through

    return {
        "occurred_at": occurred_at,
        "memo": memo,
        "counter_party_name": counter,
        "provider_transaction_id": provider_transaction_id,
        "direction": direction,
        "amount": str(amount) if amount is not None else None,
        "ambiguous": ambiguous,
    }


def _row_has_zero_amount(raw: list[Any], mapping: dict[str, int | None]) -> bool:
    values: list[Decimal] = []

    for field in ("debit", "credit", "amount"):
        idx = mapping.get(field)
        if idx is None or idx >= len(raw):
            continue
        raw_value = raw[idx]
        if raw_value in (None, ""):
            continue
        parsed = _parse_decimal(raw_value)
        if parsed is not None:
            values.append(Decimal(parsed))

    return bool(values) and all(v == 0 for v in values)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def compute_dedup_key(
    *,
    bank_account_id: int,
    direction: str,
    amount: Decimal | str,
    occurred_at: date | str,
    memo: str | None,
    counter_party_name: str | None,
) -> str:
    """Stable SHA-256 hash. Same logical txn → same hash, regardless of
    whether import is via CSV or XLSX or a re-import of the same statement.

    Memo and counter-party are normalised (trim + lowercase + collapse
    whitespace) so trivial formatting changes don't break dedup.  A structured
    JSON tuple avoids delimiter-collision ambiguity between text fields.
    """
    amt = Decimal(str(amount)).quantize(Decimal("0.01"))
    occ = (
        date.fromisoformat(occurred_at).isoformat()
        if isinstance(occurred_at, str)
        else occurred_at.isoformat()
    )
    direction_value = (
        direction.value if hasattr(direction, "value") else str(direction)
    )
    payload = json.dumps(
        [
            int(bank_account_id),
            direction_value,
            f"{amt:.2f}",
            occ,
            _norm_text(memo),
            _norm_text(counter_party_name),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def existing_dedup_keys(
    db: Session, *, bank_account_id: int, keys: Iterable[str]
) -> set[str]:
    keys = list(keys)
    if not keys:
        return set()
    rows = (
        db.query(BankTransaction.dedup_key)
        .filter(
            BankTransaction.bank_account_id == bank_account_id,
            BankTransaction.dedup_key.in_(keys),
        )
        .all()
    )
    return {r[0] for r in rows if r[0]}


def _norm_text(value: str | None) -> str:
    return " ".join((value or "").lower().split())


def existing_txn_fingerprints(
    db: Session, *, bank_account_id: int
) -> dict[tuple[str, str, str], list[set[str]]]:
    """Index of every EXISTING transaction on the account, keyed by
    (direction, amount, date), each mapping to a list of normalised
    memo/counter-party token sets.

    Used to flag a preview row as a duplicate of a transaction already in the
    account — including manually-entered rows, which carry no dedup_key and so
    are invisible to the dedup_key path. This is what stops re-importing a
    statement that overlaps rows the user typed by hand (or a prior import under
    a different memo) from silently double-counting money.
    """
    rows = (
        db.query(
            BankTransaction.direction,
            BankTransaction.amount,
            BankTransaction.occurred_at,
            BankTransaction.memo,
            BankTransaction.counter_party_name,
        )
        .filter(BankTransaction.bank_account_id == bank_account_id)
        .all()
    )
    index: dict[tuple[str, str, str], list[set[str]]] = {}
    for direction, amount, occurred_at, memo, counter_party in rows:
        dir_v = direction.value if hasattr(direction, "value") else str(direction)
        key = (
            dir_v,
            str(Decimal(amount).quantize(Decimal("0.01"))),
            occurred_at.isoformat(),
        )
        tokens = {t for t in (_norm_text(memo), _norm_text(counter_party)) if t}
        index.setdefault(key, []).append(tokens)
    return index


def fingerprint_is_duplicate(
    index: dict[tuple[str, str, str], list[set[str]]],
    *,
    direction: str,
    amount: Decimal | str,
    occurred_at: date | str,
    memo: str | None,
    counter_party: str | None,
) -> bool:
    """A preview row duplicates an existing txn when it matches on
    (direction, amount, date) AND their memo/counter-party overlap — the
    amount+date+direction match tightened by text so two genuinely different
    same-amount, same-day movements aren't falsely flagged. Rows with no text on
    either side fall back to the amount+date+direction match alone."""
    key = (
        direction,
        str(Decimal(str(amount)).quantize(Decimal("0.01"))),
        occurred_at if isinstance(occurred_at, str) else occurred_at.isoformat(),
    )
    candidates = index.get(key)
    if not candidates:
        return False
    cand_tokens = {t for t in (_norm_text(memo), _norm_text(counter_party)) if t}
    for existing_tokens in candidates:
        if not cand_tokens and not existing_tokens:
            return True
        if cand_tokens & existing_tokens:
            return True
    return False


# ---------------------------------------------------------------------------
# Rule matching + deterministic fallback suggestions
# ---------------------------------------------------------------------------


_GST_BEARING_CODES = {TaxCode.STANDARD, TaxCode.CAPITAL}

# Fast, deterministic fallback hints for common AU SMB bank memos. The value is
# (default CoA account code, tax code). Rules still win when present.
_MEMO_HEURISTICS: dict[str, tuple[str, str]] = {
    "invoice payment": ("4000", "standard"),
    "consulting": ("4000", "standard"),
    "service fee": ("4000", "standard"),
    "stripe payout": ("4000", "standard"),
    "square payout": ("4000", "standard"),
    "card settlement": ("4000", "standard"),
    "retainer": ("4000", "standard"),
    "customer": ("4000", "standard"),
    "savings interest": ("4100", "input_taxed"),
    "interest": ("4100", "none"),
    "owner": ("3000", "none"),
    "quarterly sweep": ("1010", "none"),
    "opening top-up": ("1010", "none"),
    "internal transfer": ("1010", "none"),
    "rent": ("6100", "gst_free"),
    "lease": ("6100", "gst_free"),
    "electricity": ("6110", "standard"),
    "energy": ("6110", "standard"),
    "water": ("6110", "standard"),
    "telco": ("6200", "standard"),
    "telstra": ("6200", "standard"),
    "optus": ("6200", "standard"),
    "vodafone": ("6200", "standard"),
    "internet": ("6200", "standard"),
    "travel to client": ("6310", "gst_free"),
    "uber": ("6310", "standard"),
    "qantas": ("6310", "standard"),
    "virgin": ("6310", "standard"),
    "hotel": ("6310", "standard"),
    "officeworks": ("6400", "standard"),
    "office supplies": ("6400", "standard"),
    "stationery": ("6400", "standard"),
    "fresh": ("6400", "gst_free"),
    "coffee": ("6400", "gst_free"),
    "client": ("4000", "standard"),
    "aws invoice": ("6410", "none"),
    "aws": ("6410", "standard"),
    "amazon web services": ("6410", "standard"),
    "subscription": ("6410", "standard"),
    "adobe": ("6410", "standard"),
    "xero": ("6410", "standard"),
    "microsoft": ("6410", "standard"),
    "google workspace": ("6410", "standard"),
    "account fee": ("6500", "input_taxed"),
    "bank fee": ("6500", "input_taxed"),
    "platform settlement fee": ("6500", "input_taxed"),
    "merchant fee": ("6500", "input_taxed"),
    "accounting": ("6600", "standard"),
    "legal": ("6600", "standard"),
    "insurance": ("6700", "standard"),
    "payroll": ("6000", "none"),
    "wages": ("6000", "none"),
    "salary": ("6000", "none"),
    "salaries": ("6000", "none"),
    "superannuation": ("6010", "none"),
    "ato": ("6900", "none"),
    "coworking": ("6900", "standard"),
    "plumbing repair": ("6110", "standard"),
    "warehouse supplies": ("6400", "standard"),
    "contract": ("5100", "standard"),
    "capital": ("1700", "capital"),
}

def _tax_code_value(value: TaxCode | str | None) -> str | None:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _suggested_gst_amount(
    amount: Decimal,
    tax_code: TaxCode | str | None,
    *,
    gst_registered: bool = True,
) -> str | None:
    if not gst_registered:
        return "0.00"
    if tax_code is None:
        return None
    try:
        tc = TaxCode(_tax_code_value(tax_code))
    except ValueError:
        return None
    if tc in _GST_BEARING_CODES:
        return str((amount / Decimal("11")).quantize(Decimal("0.01")))
    return "0.00"


def _accounts_by_code(db: Session) -> dict[str, Account]:
    return {
        a.code: a
        for a in db.query(Account).filter(Account.active.is_(True)).all()
    }


def heuristic_suggestion(
    accounts_by_code: dict[str, Account],
    *,
    direction: str,
    memo: str | None,
    counter_party: str | None,
) -> tuple[Account, str, str] | None:
    haystack = f"{memo or ''} {counter_party or ''}".lower()
    if not haystack.strip():
        return None
    for keyword, (code, tax_code) in _MEMO_HEURISTICS.items():
        if keyword in haystack:
            account = accounts_by_code.get(code)
            if account is not None and _account_allowed_for_direction(account, direction):
                return account, tax_code, keyword
    return None


def _account_allowed_for_direction(account: Account, direction: str) -> bool:
    account_type = AccountType(account.type)
    if direction == "in":
        return account_type not in {AccountType.EXPENSE, AccountType.COST_OF_SALES}
    if direction == "out":
        return account_type != AccountType.INCOME
    return True



def load_active_rules(db: Session) -> list[BankRule]:
    return (
        db.query(BankRule)
        .filter(BankRule.is_active.is_(True))
        .order_by(BankRule.priority.asc(), BankRule.id.asc())
        .all()
    )


def match_rule(
    rules: list[BankRule],
    *,
    direction: str,
    amount: Decimal,
    memo: str | None,
    counter_party: str | None,
) -> BankRule | None:
    """Return the first rule whose every non-null match clause matches."""
    import re

    for r in rules:
        if r.match_direction and r.match_direction != direction:
            continue
        if r.match_amount_min is not None and amount < r.match_amount_min:
            continue
        if r.match_amount_max is not None and amount > r.match_amount_max:
            continue
        if r.match_memo_regex:
            try:
                if not re.search(r.match_memo_regex, memo or "", re.IGNORECASE):
                    continue
            except re.error:
                # malformed regex → treat as non-matching, don't crash import
                continue
        if r.match_counter_party_regex:
            try:
                if not re.search(
                    r.match_counter_party_regex,
                    counter_party or "",
                    re.IGNORECASE,
                ):
                    continue
            except re.error:
                continue
        return r
    return None


# ---------------------------------------------------------------------------
# Top-level preview + commit
# ---------------------------------------------------------------------------


def _build_preview_rows(
    db: Session,
    *,
    bank_account_id: int,
    content: bytes,
    filename: str,
    bank_format: str | None = None,
    mapping: dict[str, int | None] | None = None,
    gst_registered: bool,
) -> dict[str, Any]:
    bank = db.get(BankAccount, bank_account_id)
    if bank is None:
        raise ValueError(f"Bank account {bank_account_id} not found")
    if not bank.is_active:
        raise ValueError(f"Bank account {bank.name} is inactive")

    parsed = parse_statement(
        content=content,
        filename=filename,
        bank_format=bank_format,
        mapping=mapping,
    )
    rules = load_active_rules(db)
    accounts_by_code = _accounts_by_code(db)

    # First pass: materialise rows + compute dedup keys.
    materialised: list[dict[str, Any]] = []
    for row in parsed["rows"]:
        shape = _row_to_txn_shape(row["raw"], parsed["mapping"])
        if shape.get("ambiguous"):
            materialised.append({
                "row_no": row["row_no"],
                "cells": row["cells"],
                "parsed": shape,
                "ok": False,
                "issue": "Row has both a debit and a credit — can't tell the direction",
            })
            continue
        if not shape["occurred_at"] or not shape["direction"] or not shape["amount"]:
            issue = (
                "Zero-amount rows are skipped; bank transactions must be non-zero"
                if _row_has_zero_amount(row["raw"], parsed["mapping"])
                else "Could not extract date / direction / amount"
            )
            materialised.append({
                "row_no": row["row_no"],
                "cells": row["cells"],
                "parsed": shape,
                "ok": False,
                "issue": issue,
            })
            continue
        dk = compute_dedup_key(
            bank_account_id=bank_account_id,
            direction=shape["direction"],
            amount=shape["amount"],
            occurred_at=shape["occurred_at"],
            memo=shape["memo"],
            counter_party_name=shape["counter_party_name"],
        )
        materialised.append({
            "row_no": row["row_no"],
            "cells": row["cells"],
            "parsed": shape,
            "dedup_key": dk,
            "ok": True,
        })

    # Suggestions are independent of identity classification. The caller below
    # applies the server-derived review rules shared with commit.
    for m in materialised:
        if not m.get("ok"):
            continue
        p = m["parsed"]
        m["is_duplicate"] = False
        rule = match_rule(
            rules,
            direction=p["direction"],
            amount=Decimal(p["amount"]),
            memo=p["memo"],
            counter_party=p["counter_party_name"],
        )
        amount = Decimal(p["amount"])
        if rule is not None:
            rule_account = db.get(Account, rule.set_account_id)
            if (
                rule_account is None
                or not rule_account.active
                or not _account_allowed_for_direction(rule_account, p["direction"])
            ):
                # Bank rules are still automatic classification. Keep them on the
                # safe side of the cash direction; supplier refunds and other
                # contra cases can be categorised manually from reconciliation.
                rule = None

        if rule is not None:
            tax_code = _tax_code_value(rule.set_tax_code)
            m["suggested_account_id"] = rule.set_account_id
            m["suggested_tax_code"] = tax_code
            m["suggested_gst_amount"] = _suggested_gst_amount(
                amount, tax_code, gst_registered=gst_registered
            )
            m["suggestion_source"] = "rule"
            m["matched_rule_id"] = rule.id
            m["matched_rule_description"] = rule.description
        else:
            suggestion = heuristic_suggestion(
                accounts_by_code,
                direction=p["direction"],
                memo=p["memo"],
                counter_party=p["counter_party_name"],
            )
            if suggestion is not None:
                account, tax_code, keyword = suggestion
                m["suggested_account_id"] = account.id
                m["suggested_tax_code"] = tax_code
                m["suggested_gst_amount"] = _suggested_gst_amount(
                    amount, tax_code, gst_registered=gst_registered
                )
                m["suggestion_source"] = "heuristic"
                m["matched_rule_id"] = None
                m["matched_rule_description"] = keyword
            else:
                m["suggested_account_id"] = None
                m["suggested_tax_code"] = "standard"
                m["suggested_gst_amount"] = (
                    None if gst_registered else "0.00"
                )
                m["suggestion_source"] = None
                m["matched_rule_id"] = None
                m["matched_rule_description"] = None

        if not gst_registered:
            # Keep the useful account/category suggestion but persist the row
            # outside BAS for its entire lifetime, including after a future
            # change to gst_registered=True.
            m["suggested_tax_code"] = "none"
            m["suggested_gst_amount"] = "0.00"

    return {
        "bank_account_id": bank_account_id,
        "headers": parsed["headers"],
        "mapping": parsed["mapping"],
        "field_options": parsed["field_options"],
        "rows": materialised,
    }


_COMMIT_MONEY_MAX = SQLITE_EXACT_MONEY_MAX
_COMMIT_MONEY_QUANTUM = Decimal("0.01")


def _commit_row_error(row_index: int, reason: str) -> ValueError:
    """Build a safe error that never serialises the caller's raw row."""
    return ValueError(f"Row {row_index}: {reason}")


def _commit_money(
    value: Any,
    *,
    row_index: int,
    field: str,
    strictly_positive: bool,
) -> Decimal:
    try:
        amount = Decimal(str(value))
        quantised = amount.quantize(_COMMIT_MONEY_QUANTUM)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise _commit_row_error(row_index, f"{field} is not a valid amount") from exc
    if not amount.is_finite() or amount != quantised:
        raise _commit_row_error(
            row_index,
            f"{field} must have at most two decimal places",
        )
    if strictly_positive and amount <= 0:
        raise _commit_row_error(row_index, f"{field} must be greater than zero")
    if not strictly_positive and amount < 0:
        raise _commit_row_error(row_index, f"{field} must not be negative")
    if amount > _COMMIT_MONEY_MAX:
        raise _commit_row_error(row_index, f"{field} exceeds the supported limit")
    return quantised


def _identity_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _transaction_identity_payload(
    shape: dict[str, Any], *, provider_namespace: str | None = None
) -> list[Any]:
    amount = shape.get("amount")
    return [
        shape.get("occurred_at"),
        shape.get("direction"),
        f"{Decimal(amount).quantize(Decimal('0.01')):.2f}" if amount else None,
        _norm_text(shape.get("memo")),
        _norm_text(shape.get("counter_party_name")),
        (shape.get("provider_transaction_id") or "").strip() or None,
        provider_namespace,
    ]


_GENERIC_PROVIDER_NAMESPACE = "open-accounting:generic-bank-import:v1"


def _same_provider_transaction(existing: tuple[Any, ...], shape: dict[str, Any]) -> bool:
    direction, amount, occurred_at, memo, counter_party = existing
    return (
        (direction.value if hasattr(direction, "value") else str(direction))
        == shape.get("direction")
        and Decimal(amount).quantize(Decimal("0.01"))
        == Decimal(str(shape.get("amount"))).quantize(Decimal("0.01"))
        and occurred_at.isoformat() == shape.get("occurred_at")
        and _norm_text(memo) == _norm_text(shape.get("memo"))
        and _norm_text(counter_party)
        == _norm_text(shape.get("counter_party_name"))
    )


def preview_import(
    db: Session,
    *,
    bank_account_id: int,
    content: bytes,
    filename: str,
    bank_format: str | None = None,
    mapping: dict[str, int | None] | None = None,
    gst_registered: bool,
) -> dict[str, Any]:
    """Parse and classify one statement without suppressing uncertain matches."""
    result = _build_preview_rows(
        db,
        bank_account_id=bank_account_id,
        content=content,
        filename=filename,
        bank_format=bank_format,
        mapping=mapping,
        gst_registered=gst_registered,
    )
    chosen_mapping = result["mapping"]
    namespace = _GENERIC_PROVIDER_NAMESPACE

    occurrences: dict[str, int] = {}
    for row in result["rows"]:
        shape = row["parsed"]
        row_namespace = namespace if shape.get("provider_transaction_id") else None
        row_fingerprint = _identity_hash(
            _transaction_identity_payload(
                shape, provider_namespace=row_namespace
            )
        )
        occurrences[row_fingerprint] = occurrences.get(row_fingerprint, 0) + 1
        row["row_key"] = _identity_hash(
            [row_fingerprint, occurrences[row_fingerprint]]
        )
        row["provider_namespace"] = row_namespace

    statement_key = _identity_hash(sorted(row["row_key"] for row in result["rows"]))
    valid_rows = [row for row in result["rows"] if row.get("ok")]
    has_provider_ids = bool(valid_rows) and all(
        row["parsed"].get("provider_transaction_id") for row in valid_rows
    )
    identified_instance_id = str(
        uuid5(NAMESPACE_URL, f"open-accounting:{bank_account_id}:{statement_key}")
    )

    existing = (
        db.query(
            BankTransaction.provider_namespace,
            BankTransaction.provider_transaction_id,
            BankTransaction.import_statement_key,
            BankTransaction.import_instance_id,
            BankTransaction.import_row_key,
            BankTransaction.direction,
            BankTransaction.amount,
            BankTransaction.occurred_at,
            BankTransaction.memo,
            BankTransaction.counter_party_name,
        )
        .filter(BankTransaction.bank_account_id == bank_account_id)
        .all()
    )
    provider_rows: dict[tuple[str, str], tuple[Any, ...]] = {}
    existing_instances: dict[str, set[str]] = {}
    existing_import_rows: set[tuple[str, str]] = set()
    for record in existing:
        namespace_value, provider_id, existing_statement, instance_id, row_key = record[:5]
        if namespace_value and provider_id:
            provider_rows[(namespace_value, provider_id)] = record[5:]
        if existing_statement and instance_id:
            existing_instances.setdefault(existing_statement, set()).add(instance_id)
        if instance_id and row_key:
            existing_import_rows.add((instance_id, row_key))

    prior_instances = existing_instances.get(statement_key, set())
    statement_review_required = bool(prior_instances) and not has_provider_ids
    row_provider_counts: dict[tuple[str, str], int] = {}
    for row in valid_rows:
        provider_id = (row["parsed"].get("provider_transaction_id") or "").strip()
        if provider_id:
            identity = (namespace, provider_id)
            row_provider_counts[identity] = row_provider_counts.get(identity, 0) + 1

    existing_fingerprints = existing_txn_fingerprints(
        db, bank_account_id=bank_account_id
    )
    for row in result["rows"]:
        row.update(
            {
                "import_statement_key": statement_key,
                "requires_review": False,
                "review_reason": None,
                "review_blocked": False,
                "is_duplicate": False,
            }
        )
        if not row.get("ok"):
            continue

        shape = row["parsed"]
        provider_id = (shape.get("provider_transaction_id") or "").strip()
        provider_identity = (namespace, provider_id)
        if provider_id and row_provider_counts.get(provider_identity, 0) > 1:
            row["requires_review"] = True
            row["review_reason"] = "provider_id_repeated_in_statement"
            row["review_blocked"] = True
        elif provider_id and provider_identity in provider_rows:
            if _same_provider_transaction(provider_rows[provider_identity], shape):
                row["is_duplicate"] = True
            else:
                row["requires_review"] = True
                row["review_reason"] = "provider_id_conflict"
                row["review_blocked"] = True
        elif has_provider_ids and (identified_instance_id, row["row_key"]) in existing_import_rows:
            row["is_duplicate"] = True
        elif fingerprint_is_duplicate(
            existing_fingerprints,
            direction=shape["direction"],
            amount=shape["amount"],
            occurred_at=shape["occurred_at"],
            memo=shape["memo"],
            counter_party=shape["counter_party_name"],
        ):
            row["requires_review"] = True
            row["review_reason"] = "matching_existing_transaction"

        if statement_review_required:
            row["requires_review"] = True
            row["review_reason"] = "identical_statement"

    file_fingerprint = hashlib.sha256(content).hexdigest()
    preview_key = _identity_hash(
        {
            "bank_account_id": bank_account_id,
            "file": file_fingerprint,
            "filename": filename,
            "bank_format": bank_format,
            "mapping": chosen_mapping,
            "provider_namespace": namespace,
        }
    )
    result.update(
        {
            "preview_key": preview_key,
            "import_statement_key": statement_key,
            "has_provider_ids": has_provider_ids,
            "statement_review_required": statement_review_required,
            "existing_import_count": len(prior_instances),
        }
    )
    return result


def commit_import(
    db: Session,
    *,
    bank_account_id: int,
    content: bytes,
    filename: str,
    bank_format: str | None,
    preview_key: str,
    mapping: dict[str, int | None],
    import_mode: str,
    rows: list[dict[str, Any]],
    gst_registered: bool,
) -> dict[str, int]:
    """Reparse the uploaded bytes, verify keyed decisions, and commit atomically."""
    preview = preview_import(
        db,
        bank_account_id=bank_account_id,
        content=content,
        filename=filename,
        bank_format=bank_format,
        mapping=mapping,
        gst_registered=gst_registered,
    )
    if preview["preview_key"] != preview_key:
        raise ValueError("The uploaded file or column mapping differs from the preview")

    decisions: dict[str, dict[str, Any]] = {}
    for row_index, decision in enumerate(rows, start=1):
        row_key = decision.get("row_key")
        if not isinstance(row_key, str) or row_key in decisions:
            raise _commit_row_error(row_index, "row identity is invalid or repeated")
        decisions[row_key] = decision
    preview_by_key = {row["row_key"]: row for row in preview["rows"]}
    if decisions.keys() != preview_by_key.keys():
        raise ValueError("Commit decisions do not match the previewed row identities")

    if preview["statement_review_required"]:
        if import_mode not in {"same_import", "independent_import"}:
            raise ValueError("Choose whether this is the same or an independent import")
    elif import_mode == "same_import":
        raise ValueError("There is no prior import instance to reuse")

    statement_key = preview["import_statement_key"]
    bank = db.get(BankAccount, bank_account_id)
    if bank is None:
        raise ValueError(f"Bank account {bank_account_id} not found")
    if not bank.is_active:
        raise ValueError(f"Bank account {bank.name} is inactive")

    prior_instances = sorted(
        {
            row[0]
            for row in db.query(BankTransaction.import_instance_id)
            .filter(
                BankTransaction.bank_account_id == bank_account_id,
                BankTransaction.import_statement_key == statement_key,
                BankTransaction.import_instance_id.is_not(None),
            )
            .all()
            if row[0]
        }
    )
    if preview["has_provider_ids"]:
        instance_id = str(
            uuid5(
                NAMESPACE_URL,
                f"open-accounting:{bank_account_id}:{statement_key}",
            )
        )
    elif import_mode == "same_import":
        if not prior_instances:
            raise ValueError("The prior import instance no longer exists")
        instance_id = prior_instances[0]
    else:
        instance_id = str(uuid4())

    existing_identity_pairs = {
        (row[0], row[1])
        for row in db.query(
            BankTransaction.import_instance_id, BankTransaction.import_row_key
        )
        .filter(
            BankTransaction.bank_account_id == bank_account_id,
            BankTransaction.import_instance_id == instance_id,
        )
        .all()
    }
    existing_provider_ids = {
        (row[0], row[1]): row[2:]
        for row in db.query(
            BankTransaction.provider_namespace,
            BankTransaction.provider_transaction_id,
            BankTransaction.direction,
            BankTransaction.amount,
            BankTransaction.occurred_at,
            BankTransaction.memo,
            BankTransaction.counter_party_name,
        )
        .filter(
            BankTransaction.bank_account_id == bank_account_id,
            BankTransaction.provider_transaction_id.is_not(None),
        )
        .all()
        if row[0] and row[1]
    }

    created = 0
    skipped = 0
    for row_index, preview_row in enumerate(preview["rows"], start=1):
        decision = decisions[preview_row["row_key"]]
        if not preview_row.get("ok"):
            continue
        shape = preview_row["parsed"]
        provider_id = (shape.get("provider_transaction_id") or "").strip() or None
        provider_namespace_value = preview_row["provider_namespace"] if provider_id else None
        identity_pair = (instance_id, preview_row["row_key"])

        if preview_row.get("review_blocked") and decision.get("include"):
            raise _commit_row_error(row_index, "provider transaction ID conflicts; row cannot be imported")
        if preview_row.get("is_duplicate") or identity_pair in existing_identity_pairs:
            skipped += 1
            continue
        if not decision.get("include"):
            continue

        if provider_id:
            existing_provider = existing_provider_ids.get(
                (provider_namespace_value, provider_id)
            )
            if existing_provider is not None:
                stored = (
                    existing_provider[0],
                    existing_provider[1],
                    existing_provider[2],
                    existing_provider[3],
                    existing_provider[4],
                )
                if _same_provider_transaction(stored, shape):
                    skipped += 1
                    continue
                raise _commit_row_error(row_index, "provider transaction ID conflicts with an existing row")

        try:
            occurred_at = date.fromisoformat(shape["occurred_at"])
            check_txn_date(occurred_at)
            direction = BankTxnDirection(shape["direction"])
            amount = _commit_money(
                shape["amount"],
                row_index=row_index,
                field="amount",
                strictly_positive=True,
            )
            tax_code = TaxCode(decision.get("tax_code") or TaxCode.STANDARD.value)
        except (TypeError, ValueError) as exc:
            raise _commit_row_error(row_index, "parsed transaction is invalid") from exc
        gst_amount = _commit_money(
            decision.get("gst_amount", Decimal("0")),
            row_index=row_index,
            field="gst_amount",
            strictly_positive=False,
        )
        try:
            gst_policy.require_gst_registered_for_amount(
                gst_registered=gst_registered,
                gst_amount=gst_amount,
                context="Bank import row",
            )
        except gst_policy.GstRegistrationError as exc:
            raise _commit_row_error(
                row_index,
                "gst_amount is not allowed while the company is not GST-registered",
            ) from exc
        if not gst_registered:
            tax_code = TaxCode.NONE
            gst_amount = Decimal("0.00")

        account_id = decision.get("account_id")
        account = None
        if account_id is not None:
            account = db.get(Account, account_id)
            if account is None or not account.active:
                raise _commit_row_error(row_index, "account_id is missing or inactive")
            try:
                reject_capital_tax_code_on_control_account(account, tax_code)
                reject_income_category_for_matching_ar_payment(
                    db,
                    direction=direction,
                    amount=amount,
                    account=account,
                    memo=shape["memo"],
                    counter_party_name=shape["counter_party_name"],
                    occurred_at=occurred_at,
                )
                reject_expense_category_for_matching_ap_payment(
                    db,
                    direction=direction,
                    amount=amount,
                    account=account,
                    memo=shape["memo"],
                    counter_party_name=shape["counter_party_name"],
                    occurred_at=occurred_at,
                )
                if not decision.get("invoice_allocations"):
                    reject_control_category_for_void_invoice(
                        db,
                        direction=direction,
                        amount=amount,
                        account=account,
                        memo=shape["memo"],
                        counter_party_name=shape["counter_party_name"],
                        occurred_at=occurred_at,
                    )
            except InvoicePaymentWouldDoubleCount as exc:
                raise _commit_row_error(
                    row_index,
                    "classification conflicts with an existing invoice settlement",
                ) from exc
            except BankTxnError as exc:
                raise _commit_row_error(
                    row_index,
                    "account and tax_code are incompatible",
                ) from exc

        if tax_code not in (TaxCode.STANDARD, TaxCode.CAPITAL) and gst_amount > 0:
            raise _commit_row_error(row_index, "gst_amount must be zero for the selected tax_code")
        if gst_amount > amount:
            raise _commit_row_error(row_index, "gst_amount must not exceed amount")

        txn = BankTransaction(
            bank_account_id=bank_account_id,
            direction=direction,
            amount=amount,
            occurred_at=occurred_at,
            memo=shape["memo"],
            counter_party_name=shape["counter_party_name"],
            account_id=account_id,
            gst_amount=gst_amount,
            tax_code=tax_code,
            dedup_key=None,
            provider_namespace=provider_namespace_value,
            provider_transaction_id=provider_id,
            import_statement_key=statement_key,
            import_instance_id=instance_id,
            import_row_key=preview_row["row_key"],
        )
        db.add(txn)
        db.flush()
        try:
            invoice_payments.replace_transaction_allocations(
                db,
                txn,
                decision.get("invoice_allocations") or [],
                unapplied_account_id=decision.get("unapplied_account_id"),
            )
        except invoice_payments.PaymentAllocationError as exc:
            raise _commit_row_error(
                row_index,
                "invoice allocation is invalid or incomplete",
            ) from exc
        created += 1
        existing_identity_pairs.add(identity_pair)
        if provider_id:
            existing_provider_ids[(provider_namespace_value, provider_id)] = (
                direction,
                amount,
                occurred_at,
                shape["memo"],
                shape["counter_party_name"],
            )

    db.commit()
    return {"created": created, "skipped_duplicates": skipped}
