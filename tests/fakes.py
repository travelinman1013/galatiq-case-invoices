"""Scripted stand-ins for the model-backed roles, so the loops can be tested without a model."""

from __future__ import annotations

from collections.abc import Iterable

from acme_ap.agents import OfflineAgents
from acme_ap.models import Action, Critique, Decision, Finding, Invoice


class ScriptedAgents(OfflineAgents):
    def __init__(
        self,
        *,
        extractions: Iterable[Invoice] = (),
        extraction_critiques: Iterable[Critique] = (),
        decisions: Iterable[Decision] = (),
        decision_critiques: Iterable[Critique] = (),
    ) -> None:
        super().__init__()
        self._extractions = list(extractions)
        self._extraction_critiques = list(extraction_critiques)
        self._decisions = list(decisions)
        self._decision_critiques = list(decision_critiques)
        self.calls: dict[str, int] = {"extract": 0, "critique_extraction": 0, "vp_decide": 0, "critique_decision": 0}
        self.objections_seen: list[list[str]] = []

    @staticmethod
    def _next(items: list, default):
        return items.pop(0) if items else default

    def extract(self, raw_text: str, issues: list[str], source_path: str) -> Invoice:
        self.calls["extract"] += 1
        inv = self._next(self._extractions, None)
        if inv is None:
            return super().extract(raw_text, issues, source_path)
        return inv.model_copy(update={"source_path": source_path, "source_kind": "llm"})

    def critique_extraction(self, raw_text: str, invoice: Invoice) -> Critique:
        self.calls["critique_extraction"] += 1
        return self._next(self._extraction_critiques, Critique(ok=True))

    def vp_decide(self, invoice: Invoice, findings: list[Finding], floor: Action, objections: list[str]) -> Decision:
        self.calls["vp_decide"] += 1
        self.objections_seen.append(objections)
        return self._next(self._decisions, Decision(action=floor, rationale="scripted: floor"))

    def critique_decision(self, invoice: Invoice, findings: list[Finding], decision: Decision) -> Critique:
        self.calls["critique_decision"] += 1
        return self._next(self._decision_critiques, Critique(ok=True))
