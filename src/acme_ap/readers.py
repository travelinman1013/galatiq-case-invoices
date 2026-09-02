"""File → `Invoice` (structured formats) or `RawText` (anything the model has to read)."""

from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from acme_ap.models import Invoice

STRUCTURED_SUFFIXES = {".json", ".xml", ".csv"}
TEXT_SUFFIXES = {".txt", ".pdf"}


@dataclass
class RawText:
    text: str
    path: str


def looks_parsed(invoice: Invoice) -> bool:
    """Did a structured reader actually find line items? If not, the file's shape is unknown to us
    and the raw contents should go to the extractor instead of being trusted."""
    return any(li.item and li.quantity is not None for li in invoice.line_items)


# Key/header synonyms accepted by the structured readers. Anything else falls back to the model.
_KEYS = {
    "invoice_number": (
        "invoice_number",
        "invoice number",
        "invoice",
        "invoice #",
        "invoice_id",
        "invoiceid",
        "inv",
        "number",
    ),
    "vendor": ("vendor", "supplier", "vendor_name", "seller", "from"),
    "issue_date": ("date", "issue_date", "invoice_date", "issued"),
    "due_date": ("due_date", "due date", "due", "dueon", "due_on", "payment_due"),
    "item": ("item", "name", "product", "description", "sku", "part"),
    "quantity": ("quantity", "qty", "units", "count"),
    "unit_price": ("unit_price", "unit price", "price", "rate", "unitprice"),
    "amount": ("amount", "line_total", "line total", "total", "extended"),
    "subtotal": ("subtotal", "sub_total", "net"),
    "tax": ("tax_amount", "tax", "vat", "sales_tax"),
    "shipping": ("shipping", "freight", "delivery"),
    "total": ("total", "amount_due", "amountdue", "grand_total", "balance_due", "total_amount"),
    "currency": ("currency", "ccy"),
    "payment_terms": ("payment_terms", "terms"),
    "notes": ("notes", "note", "memo", "comments"),
}


def _pick(mapping: dict, field: str):
    """First present synonym for `field` in a dict with arbitrary key spelling."""
    lowered = {str(k).strip().lower().replace("-", "_"): v for k, v in mapping.items()}
    for key in _KEYS[field]:
        if key in lowered and lowered[key] not in (None, ""):
            return lowered[key]
    return None


def _line(mapping: dict) -> dict:
    line = {
        "item": _pick(mapping, "item"),
        "quantity": _pick(mapping, "quantity"),
        "unit_price": _pick(mapping, "unit_price"),
        "note": _pick(mapping, "notes"),
    }
    if line["unit_price"] is None:
        # Some invoices give only the extended amount; derive the unit price so pricing can be checked.
        from acme_ap.normalize import money

        amount, qty = money(_pick(mapping, "amount")), money(line["quantity"])
        if amount is not None and qty:
            line["unit_price"] = amount / qty
    return line


class UnsupportedFormat(ValueError):
    pass


def load(path: str | Path) -> Invoice | RawText:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _from_json(path)
    if suffix == ".xml":
        return _from_xml(path)
    if suffix == ".csv":
        return _from_csv(path)
    if suffix == ".txt":
        return RawText(text=path.read_text(encoding="utf-8", errors="replace"), path=str(path))
    if suffix == ".pdf":
        return RawText(text=_pdf_text(path), path=str(path))
    raise UnsupportedFormat(f"Unsupported invoice format: {path.suffix}")


def _pdf_text(path: Path) -> str:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    return "\n".join(pages)


def _from_json(path: Path) -> Invoice:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return Invoice(source_path=str(path), source_kind="structured")
    vendor = _pick(data, "vendor")
    if isinstance(vendor, dict):
        vendor = _pick(vendor, "vendor") or vendor.get("name")
    lines = None
    for key in ("line_items", "items", "lines", "products"):
        if isinstance(data.get(key), list):
            lines = data[key]
            break
    return Invoice(
        invoice_number=_pick(data, "invoice_number"),
        vendor=vendor,
        issue_date=_pick(data, "issue_date"),
        due_date=_pick(data, "due_date"),
        line_items=[_line(li) for li in (lines or []) if isinstance(li, dict)],
        subtotal=_pick(data, "subtotal"),
        tax=_pick(data, "tax"),
        shipping=_pick(data, "shipping"),
        total=_pick(data, "total"),
        currency=_pick(data, "currency"),
        payment_terms=_pick(data, "payment_terms"),
        notes=_pick(data, "notes"),
        source_path=str(path),
        source_kind="structured",
    )


def _from_xml(path: Path) -> Invoice:
    root = ET.parse(path).getroot()

    def text(xpath: str) -> str | None:
        node = root.find(xpath)
        return node.text.strip() if node is not None and node.text else None

    return Invoice(
        invoice_number=text("header/invoice_number"),
        vendor=text("header/vendor"),
        issue_date=text("header/date"),
        due_date=text("header/due_date"),
        currency=text("header/currency"),
        line_items=[
            {
                "item": (li.findtext("name") or li.findtext("item")),
                "quantity": li.findtext("quantity"),
                "unit_price": li.findtext("unit_price"),
            }
            for li in root.findall("line_items/item")
        ],
        subtotal=text("totals/subtotal"),
        tax=text("totals/tax_amount"),
        total=text("totals/total"),
        payment_terms=text("payment_terms"),
        source_path=str(path),
        source_kind="structured",
    )


def _from_csv(path: Path) -> Invoice:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        raise UnsupportedFormat(f"Empty CSV: {path}")
    header = [h.strip().lower() for h in rows[0]]
    if header[:2] == ["field", "value"]:
        return _from_kv_csv(rows[1:], path)
    return _from_row_csv(header, rows[1:], path)


def _from_kv_csv(rows: list[list[str]], path: Path) -> Invoice:
    """`field,value` with repeated `item` keys: each `item` starts a new line."""
    fields: dict[str, str] = {}
    lines: list[dict] = []
    for row in rows:
        if len(row) < 2:
            continue
        key, value = row[0].strip().lower().replace("-", "_"), row[1].strip()
        if key in _KEYS["item"]:
            lines.append({"item": value})
        elif lines and key in _KEYS["quantity"] + _KEYS["unit_price"] + ("note",):
            lines[-1][
                "quantity" if key in _KEYS["quantity"] else "unit_price" if key in _KEYS["unit_price"] else "note"
            ] = value
        else:
            fields[key] = value
    return Invoice(
        invoice_number=_pick(fields, "invoice_number"),
        vendor=_pick(fields, "vendor"),
        issue_date=_pick(fields, "issue_date"),
        due_date=_pick(fields, "due_date"),
        line_items=lines,
        subtotal=_pick(fields, "subtotal"),
        tax=_pick(fields, "tax"),
        shipping=_pick(fields, "shipping"),
        total=_pick(fields, "total"),
        currency=_pick(fields, "currency"),
        payment_terms=_pick(fields, "payment_terms"),
        source_path=str(path),
        source_kind="structured",
    )


def _from_row_csv(header: list[str], rows: list[list[str]], path: Path) -> Invoice:
    """One row per line item; rows with an empty invoice number are summary rows
    (`Subtotal:` / `Tax (6%):` / `Total:` sit in the unit-price column, the value beside it)."""
    idx = {name.replace("-", "_"): i for i, name in enumerate(header)}

    def col(row: list[str], field: str) -> str | None:
        for name in _KEYS[field]:
            i = idx.get(name)
            if i is not None and i < len(row) and row[i].strip():
                return row[i].strip()
        return None

    fields: dict[str, str | None] = {}
    lines: list[dict] = []
    summary: dict[str, str] = {}
    for row in rows:
        if not any(cell.strip() for cell in row):
            continue
        if col(row, "invoice_number") and col(row, "item"):
            fields.setdefault("invoice_number", col(row, "invoice_number"))
            fields.setdefault("vendor", col(row, "vendor"))
            fields.setdefault("date", col(row, "issue_date"))
            fields.setdefault("due_date", col(row, "due_date"))
            lines.append(
                _line(
                    {
                        "item": col(row, "item"),
                        "quantity": col(row, "quantity"),
                        "unit_price": col(row, "unit_price"),
                        "amount": col(row, "amount"),
                    }
                )
            )
        else:
            # Summary row: the label is the last non-empty cell before the value.
            cells = [c.strip() for c in row if c.strip()]
            if len(cells) >= 2:
                label, value = cells[-2].lower().rstrip(":"), cells[-1]
                if label.startswith("subtotal"):
                    summary["subtotal"] = value
                elif label.startswith("tax"):
                    summary["tax"] = value
                elif label.startswith("shipping"):
                    summary["shipping"] = value
                elif label.startswith("total"):
                    summary["total"] = value
    return Invoice(
        invoice_number=fields.get("invoice_number"),
        vendor=fields.get("vendor"),
        issue_date=fields.get("date"),
        due_date=fields.get("due_date"),
        line_items=lines,
        subtotal=summary.get("subtotal"),
        tax=summary.get("tax"),
        shipping=summary.get("shipping"),
        total=summary.get("total"),
        source_path=str(path),
        source_kind="structured",
    )
