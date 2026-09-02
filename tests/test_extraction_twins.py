"""The PDFs ship with text/JSON twins, so extraction has free ground truth.

Runs only with a model configured: `uv run pytest -m llm` with LLM_PROVIDER set.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from acme_ap import llm, readers
from acme_ap.agents import Agents
from acme_ap.normalize import aggregate_quantities
from tests.conftest import INVOICES, expected_invoice

pytestmark = pytest.mark.llm

TWINS = {
    "invoice_1011.pdf": lambda: expected_invoice("invoice_1011"),
    "invoice_1012.pdf": lambda: expected_invoice("invoice_1012"),  # OCR damage + vendor rebrand
    "invoice_1013.pdf": lambda: readers.load(INVOICES / "invoice_1013.json"),  # structured twin
}


@pytest.fixture(scope="module")
def agents() -> Agents:
    model = llm.get_llm()
    if model is None:
        pytest.skip("no model configured (LLM_PROVIDER=none)")
    return Agents(model)


@pytest.mark.parametrize("pdf", TWINS, ids=list(TWINS))
def test_pdf_extraction_matches_its_twin(agents: Agents, pdf: str):
    truth = TWINS[pdf]()
    raw = readers.load(INVOICES / pdf)
    got = agents.extract(raw.text, [], str(INVOICES / pdf))

    diff = []
    if (got.invoice_number or "") != truth.invoice_number:
        diff.append(f"invoice_number: {got.invoice_number!r} != {truth.invoice_number!r}")
    if truth.vendor.lower() not in (got.vendor or "").lower():
        diff.append(f"vendor: {got.vendor!r} != {truth.vendor!r}")
    if got.total != truth.total:
        diff.append(f"total: {got.total} != {truth.total}")
    if got.due_date != truth.due_date:
        diff.append(f"due_date: {got.due_date} != {truth.due_date}")
    want = aggregate_quantities([(li.item, li.quantity) for li in truth.line_items])
    have = aggregate_quantities([(li.item, li.quantity or Decimal(0)) for li in got.line_items])
    if {k.lower(): v for k, v in want.items()} != {k.lower(): v for k, v in have.items()}:
        diff.append(f"quantities: {dict(have)} != {dict(want)}")
    assert not diff, f"{pdf} extraction drifted from its twin:\n  " + "\n  ".join(diff)
