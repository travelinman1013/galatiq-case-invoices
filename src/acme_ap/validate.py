"""Deterministic checks. Whether 22 exceeds 15 is arithmetic, not judgement — no model here."""

from __future__ import annotations

import difflib
import re
from decimal import Decimal

from acme_ap import db
from acme_ap.models import Finding, Invoice
from acme_ap.normalize import aggregate_quantities

SCRUTINY_THRESHOLD = Decimal("10000")
TOTAL_TOLERANCE = Decimal("1.00")
PRICE_TOLERANCE = Decimal("0.10")
_FRAUD_WORDS = re.compile(r"urgent|immediately|wire transfer|penalt", re.IGNORECASE)
_NET_TERMS = re.compile(r"net\s*(\d+)", re.IGNORECASE)
TERMS_TOLERANCE_DAYS = 3


def run_checks(invoice: Invoice) -> list[Finding]:
    if invoice.source_kind == "unextracted":
        return [
            Finding(
                code="EXTRACTION_UNAVAILABLE",
                severity="warn",
                message="Unstructured file and no model configured — needs a human to read it",
            )
        ]
    findings: list[Finding] = []
    for check in (
        _extraction_incomplete,
        _vendor,
        _items,
        _amounts,
        _dates,
        _duplicate,
        _currency_and_prices,
        _fraud_language,
        _over_threshold,
    ):
        findings.extend(check(invoice))
    return findings


def _extraction_incomplete(inv: Invoice) -> list[Finding]:
    if inv.source_kind == "llm" and not inv.line_items:
        return [
            Finding(
                code="EXTRACTION_INCOMPLETE",
                severity="block",
                message="Model extraction produced no line items",
            )
        ]
    return []


def _vendor(inv: Invoice) -> list[Finding]:
    if not inv.vendor:
        return [Finding(code="MISSING_VENDOR", severity="block", message="No vendor on invoice")]
    if db.lookup_vendor(inv.vendor) is None:
        hint = ""
        close = difflib.get_close_matches(inv.vendor, db.vendor_names(), n=1, cutoff=0.6)
        if close:
            hint = f" (closest approved vendor: {close[0]} — not substituted)"
        return [
            Finding(
                code="UNKNOWN_VENDOR",
                severity="warn",
                message=f"Vendor '{inv.vendor}' is not in the vendor master{hint}",
            )
        ]
    return []


def _items(inv: Invoice) -> list[Finding]:
    findings: list[Finding] = []
    catalog = db.catalog_items()
    valid: list[tuple[str, Decimal]] = []
    for li in inv.line_items:
        if li.quantity is None or li.quantity <= 0:
            findings.append(
                Finding(
                    code="INVALID_QUANTITY",
                    severity="block",
                    message=(
                        f"{li.item}: quantity could not be read"
                        if li.quantity is None
                        else f"{li.item}: quantity {li.quantity} is not a positive number"
                    ),
                    item=li.item,
                )
            )
            continue
        valid.append((li.item, li.quantity))
    for item, qty in aggregate_quantities(valid).items():
        record = db.lookup_item(item)
        if record is None:
            close = difflib.get_close_matches(item, catalog, n=1, cutoff=0.6)
            hint = f" (did you mean {close[0]}? — not substituted)" if close else ""
            findings.append(
                Finding(
                    code="UNKNOWN_ITEM",
                    severity="block",
                    message=f"{item} is not in the catalog{hint}",
                    item=item,
                )
            )
        elif record["stock"] == 0:
            findings.append(
                Finding(
                    code="ZERO_STOCK_ITEM",
                    severity="block",
                    message=f"{record['item']} has zero stock — suspicious placeholder item",
                    item=record["item"],
                )
            )
        elif qty > record["stock"]:
            findings.append(
                Finding(
                    code="STOCK_EXCEEDED",
                    severity="warn",
                    message=f"{record['item']}: invoice bills {qty} against {record['stock']} in stock",
                    item=record["item"],
                )
            )
    return findings


def _amounts(inv: Invoice) -> list[Finding]:
    findings: list[Finding] = []
    if inv.total is not None and inv.total <= 0:
        findings.append(Finding(code="INVALID_AMOUNT", severity="block", message=f"Total {inv.total} is not positive"))
    recomputed = inv.recomputed_total()
    if recomputed is not None and inv.total is not None:
        delta = inv.total - recomputed
        if abs(delta) > TOTAL_TOLERANCE:
            direction = "over" if delta > 0 else "under"
            findings.append(
                Finding(
                    code="TOTAL_MISMATCH",
                    severity="block",
                    message=(
                        f"Stated total {inv.total} is {abs(delta)} {direction} the recomputed "
                        f"{recomputed} (lines + tax + shipping)"
                    ),
                )
            )
    return findings


def _dates(inv: Invoice) -> list[Finding]:
    if inv.due_date is None:
        return [Finding(code="DUE_DATE_SUSPECT", severity="warn", message="No usable due date")]
    if inv.issue_date is not None:
        if inv.due_date < inv.issue_date:
            return [
                Finding(
                    code="DUE_DATE_SUSPECT",
                    severity="warn",
                    message=f"Due {inv.due_date} is before issue date {inv.issue_date}",
                )
            ]
        if inv.due_date == inv.issue_date and inv.payment_terms and _NET_TERMS.search(inv.payment_terms):
            return [
                Finding(
                    code="DUE_DATE_SUSPECT",
                    severity="warn",
                    message=f"Due date equals issue date despite terms '{inv.payment_terms}'",
                )
            ]
        terms = _NET_TERMS.search(inv.payment_terms or "")
        if terms:
            net_days = int(terms.group(1))
            actual = (inv.due_date - inv.issue_date).days
            if abs(actual - net_days) > TERMS_TOLERANCE_DAYS:
                return [
                    Finding(
                        code="TERMS_MISMATCH",
                        severity="warn",
                        message=f"Terms say Net {net_days} but the due date is {actual} days after issue",
                    )
                ]
    return []


def _duplicate(inv: Invoice) -> list[Finding]:
    history = [row for row in db.ledger_history(inv.invoice_number) if row["status"] != "pending_run"]
    if not history:
        return []
    paid_same = [
        r
        for r in history
        if r["status"] == "paid"
        and r["total"] is not None
        and inv.total is not None
        and abs(Decimal(str(r["total"])) - inv.total) <= TOTAL_TOLERANCE
    ]
    if paid_same:
        return [
            Finding(
                code="DUPLICATE_INVOICE",
                severity="block",
                message=(
                    f"{inv.invoice_number} already paid for the same amount on "
                    f"{paid_same[-1]['decided_at']} — possible double payment"
                ),
            )
        ]
    last = history[-1]
    return [
        Finding(
            code="DUPLICATE_INVOICE",
            severity="warn",
            message=(
                f"{inv.invoice_number} seen before (status {last['status']}, total {last['total']}) — "
                f"looks like a revision; confirm which version stands"
            ),
        )
    ]


def _currency_and_prices(inv: Invoice) -> list[Finding]:
    if inv.currency != "USD":
        return [
            Finding(
                code="CURRENCY_NOT_USD",
                severity="warn",
                message=f"Invoice is in {inv.currency}; catalog prices are USD, pricing not compared",
            )
        ]
    findings: list[Finding] = []
    for li in inv.line_items:
        if li.unit_price is None:
            continue
        record = db.lookup_item(li.item)
        if not record or not record.get("unit_price"):
            continue
        catalog_price = Decimal(str(record["unit_price"]))
        deviation = abs(li.unit_price - catalog_price) / catalog_price
        if deviation > PRICE_TOLERANCE:
            label = f"{li.item} ({li.note})" if li.note else li.item
            findings.append(
                Finding(
                    code="PRICE_DEVIATION",
                    severity="warn",
                    message=f"{label} billed at {li.unit_price} vs catalog {catalog_price} ({deviation:.0%})",
                    item=li.item,
                )
            )
    return findings


def _fraud_language(inv: Invoice) -> list[Finding]:
    haystack = " ".join(filter(None, [inv.notes, inv.payment_terms]))
    hits = sorted({m.group(0).lower() for m in _FRAUD_WORDS.finditer(haystack)})
    if hits:
        return [
            Finding(
                code="FRAUD_LANGUAGE",
                severity="warn",
                message=f"Pressure language on invoice: {', '.join(hits)}",
            )
        ]
    return []


def _over_threshold(inv: Invoice) -> list[Finding]:
    if inv.total is not None and inv.total > SCRUTINY_THRESHOLD:
        return [
            Finding(
                code="OVER_10K",
                severity="info",
                message=f"Total {inv.total} exceeds the {SCRUTINY_THRESHOLD} VP scrutiny threshold",
            )
        ]
    return []
