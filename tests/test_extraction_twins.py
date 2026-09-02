"""The PDFs ship with text/JSON twins, so extraction has free ground truth.

Runs only with a model configured: `uv run pytest -m llm` with LLM_PROVIDER set.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from acme_ap import llm, readers
from acme_ap.agents import Agents
from acme_ap.normalize import aggregate_quantities
from tests.conftest import INVOICES, STRESS, expected_invoice

pytestmark = pytest.mark.llm

TWINS = {
    "invoice_1011.pdf": lambda: expected_invoice("invoice_1011"),
    "invoice_1012.pdf": lambda: expected_invoice("invoice_1012"),  # OCR damage + vendor rebrand
    "invoice_1013.pdf": lambda: readers.load(INVOICES / "invoice_1013.json"),  # structured twin
    "stress_2003.xml": lambda: expected_invoice("stress_2003"),  # attribute XML via the extractor
    "stress_2004.txt": lambda: expected_invoice("stress_2004"),  # German labels, European numbers, EUR
    "stress_2005.txt": lambda: expected_invoice("stress_2005"),  # 30 lines
    "stress_2007.pdf": lambda: expected_invoice("stress_2007"),  # two pages
    "stress_2008.txt": lambda: expected_invoice("stress_2008"),  # upper case, hyphens
}


def _path(name: str):
    return (STRESS if name.startswith("stress_") else INVOICES) / name


@pytest.fixture(scope="module")
def agents() -> Agents:
    model = llm.get_llm()
    if model is None:
        pytest.skip("no model configured (LLM_PROVIDER=none)")
    try:
        llm.preflight()
    except RuntimeError as exc:
        pytest.fail(f"model provider not usable: {exc}")
    return Agents(model)


@pytest.mark.parametrize("pdf", TWINS, ids=list(TWINS))
def test_pdf_extraction_matches_its_twin(agents: Agents, pdf: str):
    truth = TWINS[pdf]()
    path = _path(pdf)
    raw = readers.load(path)
    text = raw.text if isinstance(raw, readers.RawText) else path.read_text()
    got = agents.extract(text, [], str(path))

    diff = []
    if (got.invoice_number or "") != truth.invoice_number:
        diff.append(f"invoice_number: {got.invoice_number!r} != {truth.invoice_number!r}")
    from acme_ap.normalize import canon_vendor

    if canon_vendor(truth.vendor) != canon_vendor(got.vendor):
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
