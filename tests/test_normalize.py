from datetime import date
from decimal import Decimal

import pytest

from acme_ap import normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$3,500.O0", Decimal("3500.00")),  # OCR letter-O inside a number
        ("$250", Decimal("250")),
        ("$750 ea", Decimal("750")),
        ("x12", Decimal("12")),
        ("qty 5", Decimal("5")),
        ("15,000.00", Decimal("15000.00")),
        ("(250.00)", Decimal("-250.00")),
        (-5, Decimal("-5")),
        (250.0, Decimal("250.0")),
        ("", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_money(raw, expected):
    assert normalize.money(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("26-Jan-2O26", date(2026, 1, 26)),  # OCR letter-O inside the year
        ("Jan 30 2026", date(2026, 1, 30)),
        ("January 27, 2026", date(2026, 1, 27)),
        ("01/28/2026", date(2026, 1, 28)),
        ("2026-02-22", date(2026, 2, 22)),
        (None, None),
        ("", None),
    ],
)
def test_parse_date(raw, expected):
    assert normalize.parse_date(raw) == expected


def test_relative_due_date_resolves_against_issue_date():
    assert normalize.parse_date("yesterday", issue_date=date(2026, 1, 20)) == date(2026, 1, 19)
    assert normalize.parse_date("yesterday") is None


@pytest.mark.parametrize(
    ("raw", "item", "note"),
    [
        ("WidgetA", "WidgetA", None),
        ("Widget A", "WidgetA", None),
        ("Gadget X ", "GadgetX", None),
        ("WidgetA (rush order)", "WidgetA", "rush order"),
        ("Mega-Sprocket", "MegaSprocket", None),
    ],
)
def test_canon_item(raw, item, note):
    assert normalize.canon_item(raw) == (item, note)


def test_canon_item_never_fuzzy_matches():
    assert normalize.canon_item("WidgetC")[0] == "WidgetC"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("INV-1004", "INV-1004"),
        ("INV 1012", "INV-1012"),
        ("1002", "INV-1002"),
        ("Inv #: 1002", "INV-1002"),
        ("#INV-1010", "INV-1010"),
        ("", None),
        (None, None),
    ],
)
def test_canon_invoice_number(raw, expected):
    assert normalize.canon_invoice_number(raw) == expected


def test_aggregate_quantities_merges_case_insensitively_in_order():
    totals = normalize.aggregate_quantities([("WidgetA", Decimal(8)), ("WidgetB", Decimal(4)), ("widgeta", Decimal(4))])
    assert list(totals.items()) == [("WidgetA", Decimal(12)), ("WidgetB", Decimal(4))]


def test_repair_leaves_words_alone():
    assert normalize.repair_numeric_tokens("FROM: QuickShip Distributers") == "FROM: QuickShip Distributers"
