from decimal import Decimal

import pytest

from acme_ap import readers
from tests.conftest import INVOICES

STRUCTURED = sorted(p for p in INVOICES.iterdir() if p.suffix in readers.STRUCTURED_SUFFIXES)


@pytest.mark.parametrize("path", STRUCTURED, ids=[p.name for p in STRUCTURED])
def test_structured_files_parse_with_totals_and_lines(path):
    inv = readers.load(path)
    assert inv.source_kind == "structured"
    assert inv.invoice_number and inv.invoice_number.startswith("INV-")
    assert inv.line_items, "every sample has at least one line"
    assert inv.total is not None


def test_kv_csv_keeps_every_repeated_item_key():
    inv = readers.load(INVOICES / "invoice_1006.csv")
    assert [(li.item, li.quantity) for li in inv.line_items] == [("WidgetA", Decimal(5)), ("WidgetB", Decimal(3))]
    assert inv.total == Decimal("2750.00")


def test_row_csv_reads_summary_rows_from_the_price_columns():
    inv = readers.load(INVOICES / "invoice_1007.csv")
    assert inv.vendor == "MegaWidgets Corp"
    assert len(inv.line_items) == 3
    assert (inv.subtotal, inv.tax, inv.total) == (Decimal("14750.00"), Decimal("885.00"), Decimal("15525.00"))
    assert inv.recomputed_total() == Decimal("15635.00")  # $110 more than the vendor claims


def test_xml_reads_currency():
    inv = readers.load(INVOICES / "invoice_1014.xml")
    assert inv.currency == "EUR"
    assert inv.vendor == "TechParts International"
    assert inv.total == Decimal("4125.00")


def test_json_nested_vendor_and_empty_strings_survive():
    inv = readers.load(INVOICES / "invoice_1009.json")
    assert inv.vendor == ""
    assert inv.due_date is None
    assert inv.line_items[0].quantity == Decimal(-5)


def test_text_and_pdf_are_raw():
    txt = readers.load(INVOICES / "invoice_1012.txt")
    pdf = readers.load(INVOICES / "invoice_1012.pdf")
    assert isinstance(txt, readers.RawText) and isinstance(pdf, readers.RawText)
    assert "QuickShip" in txt.text and "QuickShip" in pdf.text


def test_unknown_suffix_raises(tmp_path):
    bad = tmp_path / "invoice.docx"
    bad.write_text("x")
    with pytest.raises(readers.UnsupportedFormat):
        readers.load(bad)
