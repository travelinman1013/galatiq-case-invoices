"""Pure normalization helpers. No I/O, no models — every ingestion path funnels through here."""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from dateutil import parser as dateparser

# A token is "numeric-ish" when it is made only of digits, OCR look-alikes and money punctuation.
_NUMISH = re.compile(r"^[$€£]?[0-9Oo][0-9Oo,.]*$")
_TOKEN = re.compile(r"[^\s\-/():]+")
_PAREN = re.compile(r"\(([^)]*)\)")
_DIGITS = re.compile(r"\d+")


def repair_numeric_tokens(text: str) -> str:
    """Turn the letter O into a zero, but only inside tokens that are otherwise numeric.

    `$3,500.O0` -> `$3,500.00`, `26-Jan-2O26` -> `26-Jan-2026`; words are left alone.
    """

    def fix(match: re.Match[str]) -> str:
        tok = match.group(0)
        if _NUMISH.match(tok) and any(c.isdigit() for c in tok):
            return tok.replace("O", "0").replace("o", "0")
        return tok

    return _TOKEN.sub(fix, text)


def money(value: object) -> Decimal | None:
    """Parse anything money-shaped into a Decimal. Returns None when it isn't one."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return Decimal(str(value))
    text = repair_numeric_tokens(str(value)).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace("$", "").replace("€", "").replace("£", "")
    text = text.replace(",", "").replace(" ", "")
    if text.lower().endswith("ea") or text.lower().endswith("each"):
        text = re.sub(r"(?i)(each|ea)$", "", text)
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def parse_date(value: object, issue_date: date | None = None) -> date | None:
    """Parse a date in any of the sample formats. Relative words resolve against the issue date."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered == "yesterday":
        return issue_date - timedelta(days=1) if issue_date else None
    if lowered == "today":
        return issue_date
    text = repair_numeric_tokens(text)
    try:
        return dateparser.parse(text, dayfirst=False).date()
    except (ValueError, OverflowError, TypeError):
        return None


def canon_item(raw: object) -> tuple[str, str | None]:
    """`"WidgetA (rush order)"` -> `("WidgetA", "rush order")`; `"Widget A"` -> `("WidgetA", None)`.

    The catalog is matched case-insensitively downstream, so only whitespace and qualifiers are
    removed here. Unknown names stay unknown — nothing is ever fuzzy-matched into the catalog.
    """
    text = "" if raw is None else str(raw).strip()
    note = None
    paren = _PAREN.search(text)
    if paren:
        note = paren.group(1).strip() or None
        text = _PAREN.sub("", text)
    text = re.sub(r"[\s\-_]+", "", text)
    return text, note


def canon_invoice_number(raw: object) -> str | None:
    """`INV 1012`, `Inv #: 1002`, `1002`, `INV-1004` -> `INV-1012` / `INV-1002` / `INV-1004`."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    digits = _DIGITS.search(repair_numeric_tokens(text))
    if not digits:
        return text
    return f"INV-{digits.group(0)}"


def aggregate_quantities(items: list[tuple[str, Decimal]]) -> OrderedDict[str, Decimal]:
    """Sum quantities per item, case-insensitively, keeping first-seen spelling and order."""
    totals: OrderedDict[str, Decimal] = OrderedDict()
    seen: dict[str, str] = {}
    for item, qty in items:
        key = seen.setdefault(item.lower(), item)
        totals[key] = totals.get(key, Decimal(0)) + qty
    return totals
