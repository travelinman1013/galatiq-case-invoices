"""The brief's mock payment, and the ledger writes that go with each outcome."""

from __future__ import annotations

from acme_ap import db
from acme_ap.models import Decision, Invoice


def mock_payment(vendor, amount):
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}


def pay(invoice: Invoice, thread_id: str, decision: Decision) -> dict:
    amount = float(invoice.total or 0)
    result = mock_payment(invoice.vendor, amount)
    db.ledger_write(
        invoice_number=invoice.invoice_number,
        file_stem=invoice.file_stem,
        thread_id=thread_id,
        vendor=invoice.vendor,
        total=amount,
        status="paid",
        reason=decision.rationale,
    )
    return {**result, "amount": amount, "vendor": invoice.vendor}


def reject(invoice: Invoice, thread_id: str, decision: Decision) -> dict:
    db.ledger_write(
        invoice_number=invoice.invoice_number,
        file_stem=invoice.file_stem,
        thread_id=thread_id,
        vendor=invoice.vendor,
        total=float(invoice.total) if invoice.total is not None else None,
        status="rejected",
        reason=decision.rationale,
    )
    return {"status": "rejected", "reason": decision.rationale}


def park(invoice: Invoice, thread_id: str, reason: str) -> None:
    db.ledger_write(
        invoice_number=invoice.invoice_number,
        file_stem=invoice.file_stem,
        thread_id=thread_id,
        vendor=invoice.vendor,
        total=float(invoice.total) if invoice.total is not None else None,
        status="pending_review",
        reason=reason,
    )
