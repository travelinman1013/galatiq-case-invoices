from decimal import Decimal

import pytest

from acme_ap import policy
from acme_ap.models import Finding


def f(code, severity):
    return Finding(code=code, severity=severity, message=code)


@pytest.mark.parametrize(
    ("findings", "total", "action"),
    [
        ([], Decimal(5000), "approve"),
        ([f("OVER_10K", "info")], Decimal(10530), "escalate"),
        ([f("STOCK_EXCEEDED", "warn")], Decimal(15225), "escalate"),
        ([f("PRICE_DEVIATION", "warn")], Decimal(7185), "escalate"),
        ([f("TOTAL_MISMATCH", "block"), f("STOCK_EXCEEDED", "warn")], Decimal(15525), "reject"),
        ([f("UNKNOWN_ITEM", "block")], Decimal(3233), "reject"),
        ([], None, "approve"),
    ],
)
def test_floor(findings, total, action):
    assert policy.floor(findings, total)[0] == action


def test_floor_reasons_explain_the_outcome():
    action, reasons = policy.floor([f("STOCK_EXCEEDED", "warn")], Decimal(15225))
    assert action == "escalate"
    assert any("STOCK_EXCEEDED" in r for r in reasons) and any("OVER_10K" in r for r in reasons)


def test_model_can_only_tighten_the_floor():
    assert policy.max_conservative("reject", "approve") == "reject"
    assert policy.max_conservative("escalate", "approve") == "escalate"
    assert policy.max_conservative("approve", "escalate") == "escalate"
    assert policy.max_conservative("approve", "approve") == "approve"
    assert policy.max_conservative("escalate", "reject", "approve") == "reject"
