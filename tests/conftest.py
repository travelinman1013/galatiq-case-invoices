"""Every test gets a private, freshly seeded database and no model provider."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acme_ap import db
from acme_ap.models import Invoice

ROOT = Path(__file__).resolve().parent.parent
INVOICES = ROOT / "data" / "invoices"
STRESS = ROOT / "data" / "stress"
EXPECTED = Path(__file__).resolve().parent / "fixtures" / "expected"


@pytest.fixture(autouse=True)
def _isolated_db(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "inventory.db")
    monkeypatch.setattr(db, "CHECKPOINT_PATH", tmp_path / "checkpoints.db")
    monkeypatch.setattr(db, "SEED_DIR", ROOT / "data")
    if request.node.get_closest_marker("llm") is None:
        # Everything except the @llm tests runs without a model, whatever the shell has set.
        monkeypatch.setenv("LLM_PROVIDER", "none")
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    db.ensure_db()


def expected_invoice(stem: str) -> Invoice:
    """Hand-written ground truth for the unstructured samples (tests/fixtures/expected/)."""
    data = json.loads((EXPECTED / f"{stem}.json").read_text())
    folder = STRESS if stem.startswith("stress_") else INVOICES
    return Invoice(**data, source_path=str(folder / f"{stem}.txt"), source_kind="llm")
