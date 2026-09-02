"""The workflow as a graph: two self-correction cycles and one real pause.

ingest ─► critique_extraction ─(issues)─► ingest        structured files skip both
                 │ ok
              validate ─► approve ─► critique_decision ─(objection)─► approve
                                            │ ok
                            approve / escalate / reject
                               │        │          │
                              pay   escalate   log_rejection
                                      │
                                 human_review   ◄── interrupt(): sleeps in the checkpoint
                                   │      │         until someone resumes it
                                  pay  log_rejection
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, interrupt

from acme_ap import payment, policy, readers, validate
from acme_ap.agents import Agents
from acme_ap.models import Decision, RunState

MAX_EXTRACTION_ATTEMPTS = 2
MAX_DECISION_ATTEMPTS = 2

# The state carries these Pydantic models; the checkpointer must be told they are trusted.
CHECKPOINT_TYPES = [("acme_ap.models", name) for name in ("Invoice", "LineItem", "Finding", "Critique", "Decision")]


def checkpoint_serde():
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)


def _event(node: str, summary: str, **extra) -> dict:
    return {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "node": node, "summary": summary, **extra}


def build_graph(checkpointer, agents: Agents):
    """Compile the workflow. `agents` supplies the model-backed roles (or their offline stand-ins)."""

    # -- nodes ------------------------------------------------------------------------------

    def ingest(state: RunState) -> dict:
        path = state["source_path"]
        loaded = readers.load(path)
        if not isinstance(loaded, readers.RawText):
            return {
                "invoice": loaded,
                "raw_text": None,
                "extraction_attempts": 0,
                "events": [_event("ingest", f"parsed {loaded.file_stem} directly ({loaded.source_kind}, no model)")],
            }
        attempts = state.get("extraction_attempts", 0) + 1
        critique = state.get("extraction_critique")
        issues = critique.issues if critique and not critique.ok else []
        invoice = agents.extract(loaded.text, issues, path)
        what = (
            "sent to the human queue unread (no model)"
            if invoice.source_kind == "unextracted"
            else (f"extracted {len(invoice.line_items)} lines, total {invoice.total}")
        )
        return {
            "invoice": invoice,
            "raw_text": loaded.text,
            "extraction_attempts": attempts,
            "events": [_event("ingest", f"attempt {attempts}: {what}", attempt=attempts)],
        }

    def critique_extraction(state: RunState) -> dict:
        critique = agents.critique_extraction(state["raw_text"] or "", state["invoice"])
        summary = "extraction accepted" if critique.ok else f"extraction sent back: {'; '.join(critique.issues)}"
        return {"extraction_critique": critique, "events": [_event("critique_extraction", summary, ok=critique.ok)]}

    def validate_node(state: RunState) -> dict:
        invoice = state["invoice"]
        findings = validate.run_checks(invoice)
        floor, reasons = policy.floor(findings, invoice.total)
        codes = ", ".join(f.code for f in findings) or "clean"
        return {
            "findings": findings,
            "floor": floor,
            "floor_reasons": reasons,
            "decision_attempts": 0,
            "events": [
                _event("validate", f"{codes} → policy floor: {floor}", findings=[f.model_dump() for f in findings])
            ],
        }

    def approve(state: RunState) -> dict:
        attempts = state.get("decision_attempts", 0) + 1
        critique = state.get("decision_critique")
        objections = critique.issues if critique and not critique.ok else []
        vp = agents.vp_decide(state["invoice"], state["findings"], state["floor"], objections)
        final = policy.max_conservative(state["floor"], vp.action)
        rationale = vp.rationale
        if final != vp.action:
            rationale += f" [Policy floor is {state['floor']}; the model's '{vp.action}' cannot loosen it.]"
        decision = Decision(action=final, rationale=rationale)
        return {
            "vp_decision": vp,
            "decision": decision,
            "decision_attempts": attempts,
            "events": [
                _event("approve", f"attempt {attempts}: VP says {vp.action}; after floor: {final}", attempt=attempts)
            ],
        }

    def critique_decision(state: RunState) -> dict:
        critique = agents.critique_decision(state["invoice"], state["findings"], state["decision"])
        summary = "decision stands" if critique.ok else f"controller objects: {'; '.join(critique.issues)}"
        return {"decision_critique": critique, "events": [_event("critique_decision", summary, ok=critique.ok)]}

    def escalate(state: RunState) -> dict:
        payment.park(state["invoice"], state["thread_id"], state["decision"].rationale)
        return {"events": [_event("escalate", "parked in the VP queue (ledger: pending_review)")]}

    def human_review(state: RunState) -> dict:
        invoice = state["invoice"]
        answer = interrupt(
            {
                "invoice_number": invoice.invoice_number,
                "file_stem": invoice.file_stem,
                "vendor": invoice.vendor,
                "total": str(invoice.total),
                "findings": [f"{f.code}: {f.message}" for f in state["findings"]],
                "floor": state["floor"],
                "vp_rationale": state["decision"].rationale,
                "raw_text": state.get("raw_text") if invoice.source_kind == "unextracted" else None,
                "resume_with": {"action": "approve|reject", "note": "optional"},
            }
        )
        action = str(answer.get("action", "")).lower()
        if action not in {"approve", "reject"}:
            raise ValueError(f"resume payload needs action=approve|reject, got {answer!r}")
        note = answer.get("note") or ""
        decision = Decision(action=action, rationale=f"Human reviewer: {action}. {note}".strip())
        return {"human": answer, "decision": decision, "events": [_event("human_review", f"reviewer chose {action}")]}

    def pay(state: RunState) -> dict:
        result = payment.pay(state["invoice"], state["thread_id"], state["decision"])
        return {"payment": result, "events": [_event("pay", f"paid {result['amount']} to {result['vendor']}")]}

    def log_rejection(state: RunState) -> dict:
        result = payment.reject(state["invoice"], state["thread_id"], state["decision"])
        return {"payment": result, "events": [_event("log_rejection", f"rejected: {state['decision'].rationale}")]}

    # -- routing ----------------------------------------------------------------------------

    def after_ingest(state: RunState) -> Literal["critique_extraction", "validate"]:
        return "critique_extraction" if state["invoice"].source_kind == "llm" else "validate"

    def after_extraction_critique(state: RunState) -> Literal["ingest", "validate"]:
        critique = state["extraction_critique"]
        if critique and not critique.ok and state["extraction_attempts"] < MAX_EXTRACTION_ATTEMPTS:
            return "ingest"
        return "validate"

    def after_decision_critique(state: RunState) -> Literal["approve", "pay", "escalate", "log_rejection"]:
        critique = state["decision_critique"]
        if critique and not critique.ok and state["decision_attempts"] < MAX_DECISION_ATTEMPTS:
            return "approve"
        return {"approve": "pay", "escalate": "escalate", "reject": "log_rejection"}[state["decision"].action]

    def after_human(state: RunState) -> Literal["pay", "log_rejection"]:
        return "pay" if state["decision"].action == "approve" else "log_rejection"

    # -- wiring -----------------------------------------------------------------------------

    retry = RetryPolicy(max_attempts=3)
    builder = StateGraph(RunState)
    builder.add_node("ingest", ingest, retry_policy=retry)
    builder.add_node("critique_extraction", critique_extraction, retry_policy=retry)
    builder.add_node("validate", validate_node)
    builder.add_node("approve", approve, retry_policy=retry)
    builder.add_node("critique_decision", critique_decision, retry_policy=retry)
    builder.add_node("escalate", escalate)
    builder.add_node("human_review", human_review)
    builder.add_node("pay", pay)
    builder.add_node("log_rejection", log_rejection)

    builder.add_edge(START, "ingest")
    builder.add_conditional_edges("ingest", after_ingest)
    builder.add_conditional_edges("critique_extraction", after_extraction_critique)
    builder.add_edge("validate", "approve")
    builder.add_edge("approve", "critique_decision")
    builder.add_conditional_edges("critique_decision", after_decision_critique)
    builder.add_edge("escalate", "human_review")
    builder.add_conditional_edges("human_review", after_human)
    builder.add_edge("pay", END)
    builder.add_edge("log_rejection", END)
    return builder.compile(checkpointer=checkpointer)
