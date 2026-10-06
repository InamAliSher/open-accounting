from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from ..models.company import (
    Account,
    AccountType,
    CreditNote,
    CreditNoteApplication,
    CreditNoteApplicationIdempotencyKey,
    CreditNoteApplicationReversal,
    CreditNoteApplicationStatus,
    CreditNoteLine,
    CreditNoteStatus,
    Invoice,
    InvoiceDirection,
    InvoiceLine,
    InvoicePaymentAllocation,
    InvoiceStatus,
    JournalEntry,
    JournalEntrySource,
    JournalLine,
)
from ..schemas.journal import JournalLineCreate
from ..schemas._limits import SQLITE_EXACT_MONEY_MAX
from ..schemas.credit_note import CreditNoteCreate, CreditNoteLineDraftIn, CreditNoteUpdate
from . import invoice_posting
from .invoice_math import GstMathError, check_gst_math, check_invoice_lines
from .journal import _validate_lines


CENT = Decimal("0.01")
QUANTITY_SCALE = Decimal("0.0001")
VALID_TAX_CODES = {"standard", "gst_free", "input_taxed", "capital", "none"}
POSTED_STATUSES = {
    InvoiceStatus.AUTHORISED.value,
    InvoiceStatus.UNPAID.value,
    InvoiceStatus.PARTIAL.value,
    InvoiceStatus.PAID.value,
}


class CreditNoteError(Exception):
    http_status = 422


class CreditNoteNotFound(CreditNoteError):
    http_status = 404


class InvalidSource(CreditNoteError):
    http_status = 409


class SourceHasSettlement(CreditNoteError):
    http_status = 409


class DraftConflict(CreditNoteError):
    http_status = 409


class DuplicateCreditNoteNumber(CreditNoteError):
    http_status = 409


class CreditNoteValidationError(CreditNoteError):
    http_status = 422


class CreditNoteApplicationNotFound(CreditNoteError):
    http_status = 404


class CreditNoteApplicationConflict(CreditNoteError):
    http_status = 409


def _value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _money(value: Decimal) -> Decimal:
    try:
        result = Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CreditNoteValidationError("Credit-note amount could not be calculated.") from exc
    if not result.is_finite() or result < 0 or result > SQLITE_EXACT_MONEY_MAX:
        raise CreditNoteValidationError("Credit-note amount is outside supported limits.")
    return result


def _quantity(value: Decimal) -> Decimal:
    try:
        result = Decimal(value).quantize(QUANTITY_SCALE, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CreditNoteValidationError("Credit quantity must be a positive decimal.") from exc
    if not result.is_finite() or result <= 0 or result > Decimal("999999.9999"):
        raise CreditNoteValidationError("Credit quantity must be positive and within supported limits.")
    return result


def _load_invoice(session: Session, invoice_id: int) -> Invoice:
    invoice = (
        session.query(Invoice)
        .options(joinedload(Invoice.contact), selectinload(Invoice.lines))
        .filter(Invoice.id == invoice_id)
        .one_or_none()
    )
    if invoice is None:
        raise CreditNoteNotFound("Source invoice not found.")
    return invoice


def _verify_posting(session: Session, invoice: Invoice) -> None:
    expected_type = (
        JournalEntrySource.INVOICE_AR
        if _value(invoice.direction) == InvoiceDirection.AR.value
        else JournalEntrySource.INVOICE_AP
    )
    entries = (
        session.query(JournalEntry)
        .options(selectinload(JournalEntry.lines).joinedload(JournalLine.account))
        .filter(
            JournalEntry.source_id == invoice.id,
            JournalEntry.source_type.in_(
                [JournalEntrySource.INVOICE_AR, JournalEntrySource.INVOICE_AP]
            ),
        )
        .all()
    )
    if len(entries) != 1 or _value(entries[0].source_type) != expected_type.value:
        raise InvalidSource("Source invoice does not have its verified invoice journal posting.")

    entry = entries[0]
    lines = entry.lines
    debits = sum((Decimal(line.debit_amount or 0) for line in lines), Decimal("0"))
    credits = sum((Decimal(line.credit_amount or 0) for line in lines), Decimal("0"))
    control_code = "1100" if _value(invoice.direction) == "AR" else "2000"
    control_side = "debit_amount" if control_code == "1100" else "credit_amount"
    control_amount = sum(
        (
            Decimal(getattr(line, control_side) or 0)
            for line in lines
            if line.account is not None and line.account.code == control_code
        ),
        Decimal("0"),
    )
    if not lines or debits <= 0 or debits != credits or control_amount != Decimal(invoice.total):
        raise InvalidSource("Source invoice journal posting is incomplete or unbalanced.")
    try:
        check_gst_math(invoice.subtotal, invoice.gst_amount, invoice.total)
        check_invoice_lines(invoice.subtotal, invoice.gst_amount, invoice.total, invoice.lines)
    except GstMathError as exc:
        raise InvalidSource(f"Source invoice totals are invalid: {exc}") from exc


def _validate_source_line(
    session: Session,
    invoice: Invoice,
    source_line: InvoiceLine,
) -> Account:
    if Decimal(source_line.quantity) <= 0:
        raise CreditNoteValidationError(
            f"Source invoice line {source_line.id} has no positive original quantity."
        )
    if Decimal(source_line.unit_price) < 0:
        raise CreditNoteValidationError(
            f"Source invoice line {source_line.id} has an invalid unit price."
        )
    try:
        gst_rate = Decimal(source_line.gst_rate)
        tax_code = str(source_line.tax_code)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CreditNoteValidationError("Source invoice line tax data is invalid.") from exc
    if not gst_rate.is_finite() or gst_rate < 0 or gst_rate > 1 or tax_code not in VALID_TAX_CODES:
        raise CreditNoteValidationError(
            f"Source invoice line {source_line.id} has invalid persisted tax data."
        )
    if source_line.account_id is None:
        raise CreditNoteValidationError(
            f"Source invoice line {source_line.id} has no account."
        )
    account = session.get(Account, source_line.account_id)
    if account is None:
        raise CreditNoteValidationError(
            f"Source invoice line {source_line.id} references a missing account."
        )
    try:
        account_type = AccountType(_value(account.type))
    except ValueError as exc:
        raise CreditNoteValidationError(
            f"Source invoice line {source_line.id} has an invalid account type."
        ) from exc
    allowed_types = (
        {AccountType.INCOME}
        if _value(invoice.direction) == "AR"
        else {AccountType.ASSET, AccountType.EXPENSE, AccountType.COST_OF_SALES}
    )
    if not account.active or account_type not in allowed_types:
        raise CreditNoteValidationError(
            f"Source invoice line {source_line.id} account is inactive or invalid for its direction."
        )
    if tax_code == "capital" and (
        _value(invoice.direction) != "AP"
        or account_type != AccountType.ASSET
        or not account.active
    ):
        raise CreditNoteValidationError(
            f"Source invoice line {source_line.id} has invalid Capital account data."
        )
    return account


def _load_eligible_source(session: Session, invoice_id: int) -> Invoice:
    invoice = _load_invoice(session, invoice_id)
    direction = _value(invoice.direction)
    if direction not in {"AR", "AP"} or _value(invoice.status) not in POSTED_STATUSES:
        raise InvalidSource("Source invoice must be authorised or otherwise posted and not void.")
    if invoice.currency != "AUD":
        raise InvalidSource("Source invoice currency must be AUD.")
    if invoice.contact is None:
        raise InvalidSource("Source invoice contact is missing.")
    if Decimal(invoice.paid_amount or 0) > 0:
        raise SourceHasSettlement("Source invoice has recorded cash settlement.")
    if (
        session.query(InvoicePaymentAllocation.id)
        .filter(InvoicePaymentAllocation.invoice_id == invoice.id)
        .first()
        is not None
    ):
        raise SourceHasSettlement("Source invoice has an explicit cash allocation.")
    # The historical control-account settlement matcher is private to invoice posting;
    # C1A1 fails closed on paid_amount and explicit allocations without importing it.
    if not invoice.lines:
        raise InvalidSource("Source invoice must have at least one persisted line.")
    _verify_posting(session, invoice)
    for source_line in invoice.lines:
        _validate_source_line(session, invoice, source_line)
    return invoice


def _reserved_quantity(
    session: Session,
    invoice_id: int,
    source_line_id: int,
    *,
    exclude_credit_note_id: int | None = None,
) -> Decimal:
    query = (
        session.query(func.coalesce(func.sum(CreditNoteLine.quantity), 0))
        .join(CreditNote, CreditNote.id == CreditNoteLine.credit_note_id)
        .filter(
            CreditNote.source_invoice_id == invoice_id,
            CreditNote.status.in_(
                [CreditNoteStatus.DRAFT.value, CreditNoteStatus.AUTHORISED.value]
            ),
            CreditNoteLine.source_invoice_line_id == source_line_id,
        )
    )
    if exclude_credit_note_id is not None:
        query = query.filter(CreditNote.id != exclude_credit_note_id)
    return Decimal(query.scalar() or 0)


def _prepare_lines(
    session: Session,
    invoice: Invoice,
    lines: list[CreditNoteLineDraftIn],
    *,
    exclude_credit_note_id: int | None = None,
) -> list[CreditNoteLine]:
    source_by_id = {line.id: line for line in invoice.lines}
    seen: set[int] = set()
    result: list[CreditNoteLine] = []
    for request_line in lines:
        source_line_id = request_line.source_invoice_line_id
        if source_line_id in seen:
            raise DraftConflict("A source invoice line may appear only once per credit note.")
        seen.add(source_line_id)
        source_line = source_by_id.get(source_line_id)
        if source_line is None:
            raise CreditNoteValidationError(
                f"Source invoice line {source_line_id} does not belong to this invoice."
            )
        _validate_source_line(session, invoice, source_line)
        quantity = _quantity(request_line.quantity)
        original_quantity = Decimal(source_line.quantity).quantize(QUANTITY_SCALE)
        reserved = _reserved_quantity(
            session,
            invoice.id,
            source_line_id,
            exclude_credit_note_id=exclude_credit_note_id,
        )
        if reserved + quantity > original_quantity:
            raise DraftConflict(
                f"Requested quantity for source invoice line {source_line_id} exceeds its remaining creditable quantity."
            )

        try:
            extended = _money(quantity * Decimal(source_line.unit_price))
            tax_code = str(source_line.tax_code)
            if tax_code in {"gst_free", "input_taxed", "none"}:
                subtotal = extended
                gst = Decimal("0.00")
                total = extended
            elif invoice.gst_inclusive:
                total = extended
                gst = _money(total / Decimal("11"))
                subtotal = _money(total - gst)
            else:
                subtotal = extended
                gst = _money(subtotal * Decimal("0.10"))
                total = _money(subtotal + gst)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise CreditNoteValidationError("Credit-note line calculation failed.") from exc
        if subtotal <= 0 or total <= 0 or gst > total:
            raise CreditNoteValidationError(
                f"Source invoice line {source_line_id} does not produce a positive credit amount."
            )
        result.append(
            CreditNoteLine(
                source_invoice_line_id=source_line.id,
                description=source_line.description,
                account_id=source_line.account_id,
                quantity=quantity,
                unit_price=source_line.unit_price,
                gst_rate=source_line.gst_rate,
                line_subtotal=subtotal,
                line_gst=gst,
                line_total=total,
                tax_code=tax_code,
            )
        )
    return result


def _set_totals(note: CreditNote) -> None:
    note.subtotal = _money(sum((line.line_subtotal for line in note.lines), Decimal("0")))
    note.gst_amount = _money(sum((line.line_gst for line in note.lines), Decimal("0")))
    note.total = _money(sum((line.line_total for line in note.lines), Decimal("0")))
    if note.total <= 0:
        raise CreditNoteValidationError("Credit-note total must be greater than zero.")


def _number_available(
    session: Session,
    invoice: Invoice,
    number: str,
    *,
    exclude_credit_note_id: int | None = None,
) -> None:
    query = session.query(CreditNote.id).filter(
        CreditNote.direction == _value(invoice.direction),
        CreditNote.contact_id == invoice.contact_id,
        CreditNote.credit_note_number == number,
    )
    if exclude_credit_note_id is not None:
        query = query.filter(CreditNote.id != exclude_credit_note_id)
    if query.first() is not None:
        raise DuplicateCreditNoteNumber("Credit-note number already exists for this contact and direction.")


def create_credit_note(session: Session, payload: CreditNoteCreate) -> CreditNote:
    invoice = _load_eligible_source(session, payload.source_invoice_id)
    _number_available(session, invoice, payload.credit_note_number)
    note = CreditNote(
        source_invoice_id=invoice.id,
        direction=_value(invoice.direction),
        contact_id=invoice.contact_id,
        credit_note_number=payload.credit_note_number,
        issue_date=payload.issue_date,
        currency="AUD",
        gst_inclusive=invoice.gst_inclusive,
        status=CreditNoteStatus.DRAFT,
        notes=payload.notes,
    )
    note.lines = _prepare_lines(session, invoice, payload.lines)
    _set_totals(note)
    session.add(note)
    session.flush()
    return note


def get_credit_note(session: Session, credit_note_id: int) -> CreditNote:
    note = (
        session.query(CreditNote)
        .options(
            joinedload(CreditNote.source_invoice).joinedload(Invoice.contact),
            joinedload(CreditNote.contact),
            selectinload(CreditNote.lines),
            selectinload(CreditNote.applications).selectinload(
                CreditNoteApplication.reversal
            ),
        )
        .filter(CreditNote.id == credit_note_id)
        .one_or_none()
    )
    if note is None:
        raise CreditNoteNotFound("Credit note not found.")
    return note


def list_credit_notes(
    session: Session,
    *,
    direction: str | None = None,
    source_invoice_id: int | None = None,
) -> list[CreditNote]:
    query = session.query(CreditNote).options(
        joinedload(CreditNote.source_invoice).joinedload(Invoice.contact),
        joinedload(CreditNote.contact),
        selectinload(CreditNote.lines),
        selectinload(CreditNote.applications).selectinload(
            CreditNoteApplication.reversal
        ),
    )
    if direction is not None:
        query = query.filter(CreditNote.direction == direction)
    if source_invoice_id is not None:
        query = query.filter(CreditNote.source_invoice_id == source_invoice_id)
    return query.order_by(CreditNote.id.desc()).all()


def credit_note_output(note: CreditNote) -> dict:
    applications = [
        _credit_note_application_output(application)
        for application in note.applications
    ]
    applied_amount = sum(
        (
            application.amount
            for application in note.applications
            if application.status == CreditNoteApplicationStatus.ACTIVE
        ),
        Decimal("0"),
    )
    return {
        "id": note.id,
        "source_invoice_id": note.source_invoice_id,
        "source_invoice_number": note.source_invoice.invoice_number,
        "direction": _value(note.direction),
        "contact_id": note.contact_id,
        "contact_name": note.contact.name,
        "credit_note_number": note.credit_note_number,
        "issue_date": note.issue_date,
        "currency": note.currency,
        "subtotal": note.subtotal,
        "gst_amount": note.gst_amount,
        "total": note.total,
        "gst_inclusive": note.gst_inclusive,
        "status": _value(note.status),
        "notes": note.notes,
        "created_at": note.created_at,
        "updated_at": note.updated_at,
        "applied_amount": applied_amount,
        "remaining_amount": Decimal(note.total) - applied_amount,
        "applications": applications,
        "lines": [
            {
                "id": line.id,
                "source_invoice_line_id": line.source_invoice_line_id,
                "description": line.description,
                "account_id": line.account_id,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "gst_rate": line.gst_rate,
                "line_subtotal": line.line_subtotal,
                "line_gst": line.line_gst,
                "line_total": line.line_total,
                "tax_code": line.tax_code,
            }
            for line in note.lines
        ],
    }


def source_snapshot(session: Session, invoice_id: int) -> dict:
    invoice = _load_eligible_source(session, invoice_id)
    lines = []
    for source_line in invoice.lines:
        reserved = _reserved_quantity(session, invoice.id, source_line.id)
        original_quantity = Decimal(source_line.quantity).quantize(QUANTITY_SCALE)
        if reserved > original_quantity:
            raise DraftConflict("Existing draft reservations exceed a source line quantity.")
        lines.append(
            {
                "source_invoice_line_id": source_line.id,
                "description": source_line.description,
                "account_id": source_line.account_id,
                "quantity": source_line.quantity,
                "unit_price": source_line.unit_price,
                "gst_rate": source_line.gst_rate,
                "tax_code": source_line.tax_code,
                "line_subtotal": source_line.line_subtotal,
                "line_gst": source_line.line_gst,
                "line_total": source_line.line_total,
                "quantity_reserved": reserved,
                "remaining_creditable_quantity": original_quantity - reserved,
            }
        )
    return {
        "source_invoice_id": invoice.id,
        "source_invoice_number": invoice.invoice_number,
        "direction": _value(invoice.direction),
        "contact_id": invoice.contact_id,
        "contact_name": invoice.contact.name,
        "issue_date": invoice.issue_date,
        "currency": invoice.currency,
        "gst_inclusive": invoice.gst_inclusive,
        "status": _value(invoice.status),
        "subtotal": invoice.subtotal,
        "gst_amount": invoice.gst_amount,
        "total": invoice.total,
        "paid_amount": invoice.paid_amount,
        "lines": lines,
    }


def update_credit_note(
    session: Session,
    credit_note_id: int,
    payload: CreditNoteUpdate,
) -> CreditNote:
    note = get_credit_note(session, credit_note_id)
    if _value(note.status) != CreditNoteStatus.DRAFT.value:
        raise DraftConflict("Only draft credit notes can be updated.")
    invoice = _load_eligible_source(session, note.source_invoice_id)
    changes = payload.model_fields_set
    if "credit_note_number" in changes:
        _number_available(
            session,
            invoice,
            payload.credit_note_number,
            exclude_credit_note_id=note.id,
        )
        note.credit_note_number = payload.credit_note_number
    if "issue_date" in changes:
        note.issue_date = payload.issue_date
    if "notes" in changes:
        note.notes = payload.notes
    if "lines" in changes:
        replacement = payload.lines
    else:
        replacement = [
            CreditNoteLineDraftIn(
                source_invoice_line_id=line.source_invoice_line_id,
                quantity=line.quantity,
            )
            for line in note.lines
        ]
    replacement_lines = _prepare_lines(
        session,
        invoice,
        replacement,
        exclude_credit_note_id=note.id,
    )
    note.lines.clear()
    session.flush()
    note.lines = replacement_lines
    _set_totals(note)
    session.flush()
    return note


def delete_credit_note(session: Session, credit_note_id: int) -> None:
    note = get_credit_note(session, credit_note_id)
    if _value(note.status) != CreditNoteStatus.DRAFT.value:
        raise DraftConflict("Only draft credit notes can be deleted.")
    session.delete(note)
    session.flush()


def _control_account(session: Session, code: str, expected_type: AccountType) -> Account:
    account = session.query(Account).filter(Account.code == code).one_or_none()
    if account is None or not account.active:
        raise CreditNoteValidationError(f"Required active control account {code} is missing.")
    try:
        actual_type = AccountType(_value(account.type))
    except ValueError as exc:
        raise CreditNoteValidationError(
            f"Control account {code} has an invalid account type."
        ) from exc
    if actual_type != expected_type:
        raise CreditNoteValidationError(
            f"Control account {code} must have type {expected_type.value}."
        )
    return account


def post_credit_note(
    session: Session,
    credit_note_id: int,
    *,
    gst_registered: bool,
) -> JournalEntry:
    note = get_credit_note(session, credit_note_id)
    if _value(note.status) != CreditNoteStatus.DRAFT.value:
        raise DraftConflict("Only draft credit notes can be posted.")
    source_type = (
        JournalEntrySource.CREDIT_NOTE_AR
        if _value(note.direction) == "AR"
        else JournalEntrySource.CREDIT_NOTE_AP
    )
    existing = (
        session.query(JournalEntry)
        .filter(
            JournalEntry.source_type.in_(
                [
                    JournalEntrySource.CREDIT_NOTE_AR.value,
                    JournalEntrySource.CREDIT_NOTE_AP.value,
                ]
            ),
            JournalEntry.source_id == note.id,
        )
        .one_or_none()
    )
    if existing is not None:
        raise DraftConflict(f"Credit note {note.id} is already posted.")

    invoice = _load_eligible_source(session, note.source_invoice_id)
    expected_contact_kind = "customer" if note.direction == "AR" else "supplier"
    if (
        _value(note.direction) != _value(invoice.direction)
        or note.contact_id != invoice.contact_id
        or note.currency != "AUD"
        or note.gst_inclusive != invoice.gst_inclusive
        or not invoice.contact.active
        or invoice.contact.kind not in {expected_contact_kind, "both"}
    ):
        raise InvalidSource("Credit-note identity no longer matches its source invoice.")
    if invoice_posting._matching_control_settlement(session, invoice) is not None:
        raise SourceHasSettlement(
            "Source invoice has a bank settlement on its AR/AP control account."
        )

    requested_lines = [
        CreditNoteLineDraftIn(
            source_invoice_line_id=line.source_invoice_line_id,
            quantity=line.quantity,
        )
        for line in note.lines
    ]
    recalculated = _prepare_lines(
        session, invoice, requested_lines, exclude_credit_note_id=note.id
    )
    for stored, current in zip(note.lines, recalculated, strict=True):
        if any(
            getattr(stored, field) != getattr(current, field)
            for field in (
                "source_invoice_line_id",
                "description",
                "account_id",
                "quantity",
                "unit_price",
                "gst_rate",
                "line_subtotal",
                "line_gst",
                "line_total",
                "tax_code",
            )
        ):
            raise InvalidSource("Credit-note line no longer matches its source invoice.")
    subtotal = _money(sum((line.line_subtotal for line in recalculated), Decimal("0")))
    gst_amount = _money(sum((line.line_gst for line in recalculated), Decimal("0")))
    total = _money(sum((line.line_total for line in recalculated), Decimal("0")))
    if (note.subtotal, note.gst_amount, note.total) != (subtotal, gst_amount, total):
        raise CreditNoteValidationError("Credit-note totals no longer match its lines.")
    if not gst_registered and gst_amount != 0:
        raise CreditNoteValidationError(
            "This company is not GST-registered; a credit note with GST cannot be posted."
        )

    lines: list[JournalLineCreate] = []
    if note.direction == "AR":
        receivables = _control_account(session, "1100", AccountType.ASSET)
        lines.extend(
            JournalLineCreate(
                account_id=line.account_id,
                debit_amount=line.line_subtotal,
                description=line.description,
            )
            for line in recalculated
        )
        if gst_amount:
            gst_control = _control_account(session, "2100", AccountType.LIABILITY)
            lines.append(
                JournalLineCreate(
                    account_id=gst_control.id,
                    debit_amount=gst_amount,
                    description="GST collected reversal",
                )
            )
        lines.append(
            JournalLineCreate(
                account_id=receivables.id,
                credit_amount=total,
                description=f"Credit note {note.credit_note_number} receivable",
            )
        )
    else:
        payables = _control_account(session, "2000", AccountType.LIABILITY)
        lines.append(
            JournalLineCreate(
                account_id=payables.id,
                debit_amount=total,
                description=f"Credit note {note.credit_note_number} payable",
            )
        )
        lines.extend(
            JournalLineCreate(
                account_id=line.account_id,
                credit_amount=line.line_subtotal,
                description=line.description,
            )
            for line in recalculated
        )
        if gst_amount:
            gst_control = _control_account(session, "1200", AccountType.ASSET)
            lines.append(
                JournalLineCreate(
                    account_id=gst_control.id,
                    credit_amount=gst_amount,
                    description="GST paid reversal",
                )
            )

    _validate_lines(session, lines)
    entry = JournalEntry(
        entry_date=note.issue_date,
        memo=f"Credit note {note.credit_note_number}",
        reference=note.credit_note_number,
        source_type=source_type,
        source_id=note.id,
    )
    for line in lines:
        entry.lines.append(
            JournalLine(
                account_id=line.account_id,
                debit_amount=line.debit_amount or Decimal("0"),
                credit_amount=line.credit_amount or Decimal("0"),
                description=line.description,
            )
        )
    note.status = CreditNoteStatus.AUTHORISED
    session.add(entry)
    session.flush()
    return entry


def _application_payload_hash(
    *, credit_note_id: int, invoice_id: int, amount: Decimal, application_date: date
) -> str:
    payload = {
        "credit_note_id": credit_note_id,
        "invoice_id": invoice_id,
        "amount": str(amount),
        "application_date": application_date.isoformat(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _credit_note_application_output(application: CreditNoteApplication) -> dict:
    reversal = application.reversal
    return {
        "id": application.id,
        "invoice_id": application.invoice_id,
        "amount": application.amount,
        "application_date": application.application_date,
        "status": _value(application.status),
        "created_at": application.created_at,
        "updated_at": application.updated_at,
        "reversed_at": application.reversed_at,
        "reversal_date": reversal.reversal_date if reversal is not None else None,
    }


def _application_energy(
    session: Session, credit_note: CreditNote, invoice: Invoice
) -> tuple[Decimal, Decimal]:
    credit_applied = Decimal(
        session.query(func.coalesce(func.sum(CreditNoteApplication.amount), 0))
        .filter(
            CreditNoteApplication.credit_note_id == credit_note.id,
            CreditNoteApplication.invoice_id == invoice.id,
            CreditNoteApplication.status == CreditNoteApplicationStatus.ACTIVE,
        )
        .scalar()
        or 0
    )
    invoice_applied = Decimal(
        session.query(func.coalesce(func.sum(CreditNoteApplication.amount), 0))
        .filter(
            CreditNoteApplication.invoice_id == invoice.id,
            CreditNoteApplication.status == CreditNoteApplicationStatus.ACTIVE,
        )
        .scalar()
        or 0
    )
    return credit_applied, invoice_applied


def apply_credit_note(
    session: Session,
    credit_note_id: int,
    payload,
    *,
    company,
    idempotency_key: str,
) -> CreditNoteApplication:
    credit_note = get_credit_note(session, credit_note_id)
    if _value(credit_note.status) != CreditNoteStatus.AUTHORISED.value:
        raise CreditNoteApplicationConflict(
            "Only an authorised credit note may be applied."
        )
    from .period_lock import require_open_date

    require_open_date(
        company,
        payload.application_date,
        operation="apply a credit note",
    )
    invoice = session.get(Invoice, payload.invoice_id)
    if invoice is None:
        raise CreditNoteApplicationNotFound("Target invoice not found.")
    if _value(invoice.status) != InvoiceStatus.AUTHORISED.value:
        raise CreditNoteApplicationConflict(
            "Target invoice must be authorised and not void."
        )
    if (
        credit_note.direction != invoice.direction
        or credit_note.contact_id != invoice.contact_id
        or credit_note.currency != invoice.currency
    ):
        raise CreditNoteApplicationConflict(
            "Credit note and invoice must match by Contact, direction, and currency."
        )

    credit_applied, invoice_applied = _application_energy(
        session, credit_note, invoice
    )
    remaining_credit = Decimal(credit_note.total) - credit_applied
    outstanding = Decimal(invoice.total) - Decimal(invoice.paid_amount or 0) - invoice_applied
    if payload.amount > remaining_credit:
        raise CreditNoteApplicationConflict(
            "Application exceeds the credit note's remaining amount."
        )
    if payload.amount > outstanding:
        raise CreditNoteApplicationConflict(
            "Application exceeds the invoice's outstanding amount."
        )

    payload_hash = _application_payload_hash(
        credit_note_id=credit_note_id,
        invoice_id=payload.invoice_id,
        amount=payload.amount,
        application_date=payload.application_date,
    )
    existing_key = session.get(CreditNoteApplicationIdempotencyKey, idempotency_key)
    if existing_key is not None:
        application = session.get(CreditNoteApplication, existing_key.application_id)
        if application is None:
            raise CreditNoteApplicationConflict(
                "Idempotency-Key points to a missing credit application."
            )
        if existing_key.payload_hash != payload_hash:
            raise CreditNoteApplicationConflict(
                "Idempotency-Key has already been used with a different payload."
            )
        return application

    application = CreditNoteApplication(
        credit_note_id=credit_note_id,
        invoice_id=payload.invoice_id,
        amount=payload.amount,
        application_date=payload.application_date,
        status=CreditNoteApplicationStatus.ACTIVE,
    )
    session.add(application)
    session.flush()
    session.add(
        CreditNoteApplicationIdempotencyKey(
            key=idempotency_key,
            payload_hash=payload_hash,
            application_id=application.id,
        )
    )
    session.flush()
    return application


def reverse_credit_note_application(
    session: Session,
    application_id: int,
    payload,
    *,
    company,
) -> CreditNoteApplication:
    application = session.get(CreditNoteApplication, application_id)
    if application is None:
        raise CreditNoteApplicationNotFound("Credit application not found.")
    if application.status != CreditNoteApplicationStatus.ACTIVE:
        if application.reversal is not None:
            return application
        raise CreditNoteApplicationConflict(
            "Only an active credit application can be reversed."
        )
    from .period_lock import require_open_date

    require_open_date(
        company,
        payload.reversal_date,
        operation="reverse a credit application",
    )
    reversal = CreditNoteApplicationReversal(
        application_id=application.id,
        reversal_date=payload.reversal_date,
    )
    application.status = CreditNoteApplicationStatus.REVERSED
    application.reversed_at = datetime.now(timezone.utc)
    application.reversal = reversal
    session.add(reversal)
    session.flush()
    return application