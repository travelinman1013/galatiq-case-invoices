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
    vendor = data.get("vendor")
    if isinstance(vendor, dict):
        vendor = vendor.get("name")
    return Invoice(
        invoice_number=data.get("invoice_number"),
        vendor=vendor,
        issue_date=data.get("date") or data.get("issue_date"),
        due_date=data.get("due_date"),
        line_items=[
            {
                "item": li.get("item") or li.get("name"),
                "quantity": li.get("quantity"),
                "unit_price": li.get("unit_price"),
                "note": li.get("note"),
            }
            for li in data.get("line_items", [])
        ],
        subtotal=data.get("subtotal"),
        tax=data.get("tax_amount", data.get("tax")),
        shipping=data.get("shipping"),
        total=data.get("total"),
        currency=data.get("currency"),
        payment_terms=data.get("payment_terms"),
        notes=data.get("notes"),
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
        key, value = row[0].strip().lower(), row[1].strip()
        if key == "item":
            lines.append({"item": value})
        elif key in {"quantity", "qty", "unit_price", "note"} and lines:
            lines[-1]["quantity" if key == "qty" else key] = value
        else:
            fields[key] = value
    return Invoice(
        invoice_number=fields.get("invoice_number"),
        vendor=fields.get("vendor"),
        issue_date=fields.get("date"),
        due_date=fields.get("due_date"),
        line_items=lines,
        subtotal=fields.get("subtotal"),
        tax=fields.get("tax"),
        shipping=fields.get("shipping"),
        total=fields.get("total"),
        currency=fields.get("currency"),
        payment_terms=fields.get("payment_terms"),
        source_path=str(path),
        source_kind="structured",
    )


def _from_row_csv(header: list[str], rows: list[list[str]], path: Path) -> Invoice:
    """One row per line item; rows with an empty invoice number are summary rows
    (`Subtotal:` / `Tax (6%):` / `Total:` sit in the unit-price column, the value beside it)."""
    idx = {name: i for i, name in enumerate(header)}

    def col(row: list[str], *names: str) -> str | None:
        for name in names:
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
        if col(row, "invoice number", "invoice_number"):
            fields.setdefault("invoice_number", col(row, "invoice number", "invoice_number"))
            fields.setdefault("vendor", col(row, "vendor"))
            fields.setdefault("date", col(row, "date"))
            fields.setdefault("due_date", col(row, "due date", "due_date"))
            lines.append(
                {
                    "item": col(row, "item"),
                    "quantity": col(row, "qty", "quantity"),
                    "unit_price": col(row, "unit price", "unit_price"),
                }
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
