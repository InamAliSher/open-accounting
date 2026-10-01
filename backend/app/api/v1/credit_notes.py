from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...deps import PathId, get_company_db, get_current_company
from ...models.master import Company
from ...schemas._limits import SQLITE_INT_MAX
from ...schemas.credit_note import (
    CreditNoteCreate,
    CreditNoteOut,
    CreditNoteSourceOut,
    CreditNoteUpdate,
)
from ...services import credit_notes as credit_note_service
from ...services import doc_numbering


router = APIRouter(prefix="/credit-notes", tags=["credit-notes"])


def _raise_domain_error(exc: credit_note_service.CreditNoteError) -> None:
    raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc


def _raise_integrity_conflict(db: Session, exc: IntegrityError) -> None:
    db.rollback()
    raise HTTPException(
        status_code=409,
        detail="Credit note conflicts with existing data.",
    ) from exc


@router.get("", response_model=list[CreditNoteOut])
def list_credit_notes(
    direction: Literal["AR", "AP"] | None = None,
    source_invoice_id: int | None = Query(default=None, ge=1, le=SQLITE_INT_MAX),
    status_filter: Literal["draft"] | None = Query(default=None, alias="status"),
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    return [
        credit_note_service.credit_note_output(note)
        for note in credit_note_service.list_credit_notes(
            db,
            direction=direction,
            source_invoice_id=source_invoice_id,
        )
        if status_filter is None or note.status == status_filter
    ]


@router.get("/source-invoices/{invoice_id}", response_model=CreditNoteSourceOut)
def get_source_invoice(
    invoice_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        return credit_note_service.source_snapshot(db, invoice_id)
    except credit_note_service.CreditNoteError as exc:
        _raise_domain_error(exc)


@router.get("/{credit_note_id}", response_model=CreditNoteOut)
def get_credit_note(
    credit_note_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        return credit_note_service.credit_note_output(
            credit_note_service.get_credit_note(db, credit_note_id)
        )
    except credit_note_service.CreditNoteError as exc:
        _raise_domain_error(exc)


@router.post("", response_model=CreditNoteOut, status_code=status.HTTP_201_CREATED)
def create_credit_note(
    payload: CreditNoteCreate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc_numbering._begin_sqlite_immediate(db)
    try:
        note = credit_note_service.create_credit_note(db, payload)
        output = credit_note_service.credit_note_output(note)
        db.commit()
        return output
    except credit_note_service.CreditNoteError as exc:
        db.rollback()
        _raise_domain_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(db, exc)


@router.patch("/{credit_note_id}", response_model=CreditNoteOut)
def update_credit_note(
    credit_note_id: PathId,
    payload: CreditNoteUpdate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc_numbering._begin_sqlite_immediate(db)
    try:
        note = credit_note_service.update_credit_note(db, credit_note_id, payload)
        output = credit_note_service.credit_note_output(note)
        db.commit()
        return output
    except credit_note_service.CreditNoteError as exc:
        db.rollback()
        _raise_domain_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(db, exc)


@router.delete("/{credit_note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_credit_note(
    credit_note_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc_numbering._begin_sqlite_immediate(db)
    try:
        credit_note_service.delete_credit_note(db, credit_note_id)
        db.commit()
    except credit_note_service.CreditNoteError as exc:
        db.rollback()
        _raise_domain_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(db, exc)