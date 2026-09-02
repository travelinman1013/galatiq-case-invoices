"""The scenario table: which finding codes each sample invoice must raise."""

from decimal import Decimal

import pytest

from acme_ap import db, readers, validate
from acme_ap.models import Invoice
from tests.conftest import INVOICES, expected_invoice

SCENARIOS = {
    # structured files — read straight from data/
    "invoice_1004.json": set(),
    "invoice_1005.json": {"STOCK_EXCEEDED", "OVER_10K"},
    "invoice_1006.csv": set(),
    "invoice_1007.csv": {"STOCK_EXCEEDED", "TOTAL_MISMATCH", "OVER_10K"},
    "invoice_1009.json": {"MISSING_VENDOR", "INVALID_QUANTITY", "INVALID_AMOUNT", "DUE_DATE_SUSPECT"},
    "invoice_1013.json": {"STOCK_EXCEEDED", "TOTAL_MISMATCH", "OVER_10K"},
    "invoice_1014.xml": {"CURRENCY_NOT_USD"},
    "invoice_1015.csv": set(),
    "invoice_1016.json": {"UNKNOWN_ITEM"},
    "invoice_1017.json": {"OVER_10K"},
    # unstructured files — hand-written expected extractions in tests/fixtures/expected/
    "invoice_1001": set(),
    "invoice_1002": {"STOCK_EXCEEDED", "DUE_DATE_SUSPECT", "OVER_10K"},
    "invoice_1003": {"UNKNOWN_VENDOR", "ZERO_STOCK_ITEM", "DUE_DATE_SUSPECT", "FRAUD_LANGUAGE", "OVER_10K"},
    "invoice_1008": {"UNKNOWN_VENDOR", "UNKNOWN_ITEM"},
    "invoice_1010": {"PRICE_DEVIATION"},
    "invoice_1011": set(),
    "invoice_1012": {"UNKNOWN_VENDOR"},
}


def _invoice(key: str) -> Invoice:
    return readers.load(INVOICES / key) if "." in key else expected_invoice(key)


@pytest.mark.parametrize("key", SCENARIOS, ids=list(SCENARIOS))
def test_scenario_codes(key):
    findings = validate.run_checks(_invoice(key))
    assert {f.code for f in findings} == SCENARIOS[key]


def test_stock_check_aggregates_duplicate_skus():
    inv = _invoice("invoice_1013.json")
    stock = [f for f in validate.run_checks(inv) if f.code == "STOCK_EXCEEDED"]
    assert {f.item for f in stock} == {"WidgetA", "WidgetB", "GadgetX"}
    assert any("22" in f.message for f in stock)


def test_unknown_item_suggests_but_never_substitutes():
    inv = _invoice("invoice_1016.json")
    [f] = [f for f in validate.run_checks(inv) if f.code == "UNKNOWN_ITEM"]
    assert f.item == "WidgetC"
    assert "did you mean Widget" in f.message
    assert "not substituted" in f.message


def test_rush_order_qualifier_becomes_a_note_not_an_unknown_item():
    inv = _invoice("invoice_1010")
    rush = inv.line_items[-1]
    assert (rush.item, rush.note) == ("WidgetA", "rush order")
    [f] = validate.run_checks(inv)
    assert f.code == "PRICE_DEVIATION" and "20%" in f.message


def test_shipping_is_part_of_the_arithmetic():
    inv = _invoice("invoice_1010")
    assert inv.recomputed_total() == Decimal("7185.00")


def test_rebranded_vendor_points_at_the_approved_name():
    [f] = validate.run_checks(_invoice("invoice_1012"))
    assert f.code == "UNKNOWN_VENDOR"


def test_duplicate_invoice_revision_vs_double_pay():
    first = readers.load(INVOICES / "invoice_1004.json")
    revised = readers.load(INVOICES / "invoice_1004_revised.json")
    assert validate.run_checks(revised) == []  # nothing in the ledger yet

    db.ledger_write(
        invoice_number=first.invoice_number,
        file_stem="invoice_1004",
        thread_id="t1",
        vendor=first.vendor,
        total=float(first.total),
        status="paid",
        reason=None,
    )
    [rev] = validate.run_checks(revised)
    assert (rev.code, rev.severity) == ("DUPLICATE_INVOICE", "warn")  # different total → revision

    [dup] = validate.run_checks(first)
    assert (dup.code, dup.severity) == ("DUPLICATE_INVOICE", "block")  # same total, already paid


def test_price_check_skipped_for_foreign_currency():
    codes = {f.code for f in validate.run_checks(_invoice("invoice_1014.xml"))}
    assert "PRICE_DEVIATION" not in codes  # 225 EUR vs 250 USD is not a price finding


def test_terms_mismatch_has_a_tolerance():
    base = dict(
        invoice_number="INV-9",
        vendor="Widgets Inc.",
        issue_date="2026-01-15",
        payment_terms="Net 15",
        line_items=[{"item": "WidgetA", "quantity": 1, "unit_price": 250}],
        total=250,
    )
    ok = Invoice(**base, due_date="2026-02-01")  # 17 days on Net 15: calendar rounding, no finding
    assert validate.run_checks(ok) == []
    off = Invoice(**base, due_date="2026-03-15")  # 59 days on Net 15
    [f] = validate.run_checks(off)
    assert f.code == "TERMS_MISMATCH" and f.severity == "warn"


def test_unreadable_quantity_is_a_finding_not_a_crash():
    inv = Invoice(
        invoice_number="INV-8",
        vendor="Widgets Inc.",
        issue_date="2026-01-15",
        due_date="2026-02-01",
        line_items=[{"item": "WidgetA", "quantity": "twelve", "unit_price": 250}],
        total=3000,
    )
    assert inv.line_items[0].quantity is None
    codes = [f.code for f in validate.run_checks(inv)]
    assert codes == ["INVALID_QUANTITY"]
