"""Drive the Streamlit page headlessly: run an invoice live, then approve it from the inbox."""

from __future__ import annotations

import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from acme_ap import db  # noqa: E402
from tests.conftest import ROOT  # noqa: E402


@pytest.fixture
def page():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()
    assert not at.exception
    return at


def test_page_renders_empty_state(page):
    assert "Acme Corp" in page.title[0].value
    assert any("Nothing waiting" in s.value for s in page.success)


def test_run_live_then_approve_from_the_inbox(page):
    # pick INV-1017 (clean, over $10K) and press Run: the graph streams and pauses for the VP
    options = page.selectbox[0].options
    idx = next(i for i, o in enumerate(options) if "invoice_1017" in o)
    page.selectbox[0].select(options[idx]).run()
    run_button = next(b for b in page.button if b.label == "Run")
    run_button.click().run()
    assert not page.exception
    assert page.session_state["last"]["status"] == "PAUSED"
    assert db.ledger_find_thread("INV-1017")["status"] == "pending_review"
    assert any("Human review" in m.value for m in page.markdown)

    # the inbox lists it; Approve resumes the paused thread and pays
    approve = next(b for b in page.button if b.label == "Approve")
    approve.click().run()
    assert not page.exception
    assert db.ledger_find_thread("INV-1017")["status"] == "paid"
