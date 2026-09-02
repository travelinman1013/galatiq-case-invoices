"""Typed contracts shared by every stage. Validators call `normalize`, so no reader can forget to."""

from __future__ import annotations

import operator
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, model_validator

from acme_ap import normalize

Action = Literal["approve", "escalate", "reject"]
Severity = Literal["info", "warn", "block"]
SourceKind = Literal["structured", "llm", "unextracted"]

_MONEY_FIELDS = ("subtotal", "tax", "shipping", "total")


class LineItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    item: str
    quantity: Decimal
    unit_price: Decimal | None = None
    raw_item: str | None = None
    note: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        raw = data.get("item")
        item, note = normalize.canon_item(raw)
        data["item"] = item
        data.setdefault("raw_item", None if raw is None else str(raw))
        if note and not data.get("note"):
            data["note"] = note
        data["quantity"] = normalize.money(data.get("quantity"))
        data["unit_price"] = normalize.money(data.get("unit_price"))
        return data


class Invoice(BaseModel):
    """The one shape every stage after ingestion sees, whatever the file looked like."""

    model_config = ConfigDict(extra="ignore")

    invoice_number: str | None = None
    vendor: str | None = None
    issue_date: date | None = None
    due_date: date | None = None
    line_items: list[LineItem] = []
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    shipping: Decimal | None = None
    total: Decimal | None = None
    currency: str = "USD"
    payment_terms: str | None = None
    notes: str | None = None
    source_path: str | None = None
    source_kind: SourceKind = "structured"

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        data["invoice_number"] = normalize.canon_invoice_number(data.get("invoice_number"))
        vendor = data.get("vendor")
        data["vendor"] = vendor.strip() if isinstance(vendor, str) else vendor
        issue = normalize.parse_date(data.get("issue_date"))
        data["issue_date"] = issue
        data["due_date"] = normalize.parse_date(data.get("due_date"), issue_date=issue)
        for field in _MONEY_FIELDS:
            data[field] = normalize.money(data.get(field))
        currency = data.get("currency")
        data["currency"] = (currency or "USD").strip().upper()
        data["line_items"] = [li for li in (data.get("line_items") or []) if li is not None]
        return data

    @property
    def file_stem(self) -> str:
        from pathlib import Path

        return Path(self.source_path).stem if self.source_path else (self.invoice_number or "?")

    def recomputed_total(self) -> Decimal | None:
        """Lines × price + stated tax + stated shipping. None when a line has no price."""
        if not self.line_items or any(li.unit_price is None for li in self.line_items):
            return None
        lines = sum((li.quantity * li.unit_price for li in self.line_items), Decimal(0))
        return lines + (self.tax or Decimal(0)) + (self.shipping or Decimal(0))


class LineDraft(BaseModel):
    """LLM-facing line: every field a plain string so strict JSON-schema modes accept it."""

    item: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    note: str | None = None


class InvoiceDraft(BaseModel):
    """What the extractor returns. Converted with `Invoice.model_validate(draft.model_dump())`."""

    invoice_number: str | None = None
    vendor: str | None = None
    issue_date: str | None = None
    due_date: str | None = None
    line_items: list[LineDraft] = []
    subtotal: str | None = None
    tax: str | None = None
    shipping: str | None = None
    total: str | None = None
    currency: str | None = None
    payment_terms: str | None = None
    notes: str | None = None


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str
    item: str | None = None


class Critique(BaseModel):
    """Shared by both self-correction loops: `ok=False` sends the work back with `issues`."""

    ok: bool
    issues: list[str] = []


class Decision(BaseModel):
    action: Action
    rationale: str


class RunState(TypedDict, total=False):
    source_path: str
    thread_id: str
    raw_text: str | None
    invoice: Invoice | None
    extraction_attempts: int
    extraction_critique: Critique | None
    findings: list[Finding]
    floor: Action
    floor_reasons: list[str]
    vp_decision: Decision | None
    decision_critique: Critique | None
    decision_attempts: int
    decision: Decision | None
    human: dict | None
    payment: dict | None
    events: Annotated[list[dict], operator.add]
