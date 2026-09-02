"""End-to-end through the compiled graph: outcomes, the pause, and both self-correction loops."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from acme_ap import db
from acme_ap.agents import OfflineAgents
from acme_ap.graph import build_graph
from acme_ap.models import Critique, Decision
from tests.conftest import INVOICES, expected_invoice
from tests.fakes import ScriptedAgents

EXPECTED_OFFLINE = {
    "invoice_1001.txt": ("escalate", "PAUSED"),  # unstructured + no model → a human reads it
    "invoice_1004.json": ("approve", "success"),
    "invoice_1005.json": ("escalate", "PAUSED"),
    "invoice_1006.csv": ("approve", "success"),
    "invoice_1007.csv": ("reject", "rejected"),
    "invoice_1009.json": ("reject", "rejected"),
    "invoice_1013.json": ("reject", "rejected"),
    "invoice_1014.xml": ("escalate", "PAUSED"),
    "invoice_1015.csv": ("approve", "success"),
    "invoice_1016.json": ("reject", "rejected"),
    "invoice_1017.json": ("escalate", "PAUSED"),
}


def _run(graph, name: str, thread_id: str | None = None):
    thread_id = thread_id or f"t-{name}"
    config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke({"source_path": str(INVOICES / name), "thread_id": thread_id}, config=config)
    state = graph.get_state(config).values
    return state, "__interrupt__" in result, config


@pytest.mark.parametrize("name", EXPECTED_OFFLINE, ids=list(EXPECTED_OFFLINE))
def test_offline_outcomes(name):
    graph = build_graph(InMemorySaver(), OfflineAgents())
    state, paused, _ = _run(graph, name)
    action, status = EXPECTED_OFFLINE[name]
    assert state["decision"].action == action
    if status == "PAUSED":
        assert paused and state.get("payment") is None
        assert db.ledger_find_thread(state["thread_id"])["status"] == "pending_review"
    else:
        assert not paused and state["payment"]["status"] == status


def test_unstructured_offline_goes_to_a_human_with_the_raw_text():
    graph = build_graph(InMemorySaver(), OfflineAgents())
    state, paused, config = _run(graph, "invoice_1003.txt")
    assert paused
    assert [f.code for f in state["findings"]] == ["EXTRACTION_UNAVAILABLE"]
    payload = graph.get_state(config).interrupts[0].value
    assert "Fraudster LLC" in payload["raw_text"]


def test_escalation_pauses_then_resumes_to_payment(capsys):
    graph = build_graph(InMemorySaver(), OfflineAgents())
    state, paused, config = _run(graph, "invoice_1005.json")
    assert paused
    assert {f.code for f in state["findings"]} == {"STOCK_EXCEEDED", "OVER_10K"}

    graph.invoke(Command(resume={"action": "approve", "note": "Receiving confirmed the extra GadgetX"}), config=config)
    state = graph.get_state(config).values
    assert state["payment"]["status"] == "success"
    assert state["decision"].rationale.startswith("Human reviewer: approve")
    assert db.ledger_find_thread(state["thread_id"])["status"] == "paid"
    assert "Paid 15225.0 to Global Supply Chain Partners" in capsys.readouterr().out


def test_escalation_can_be_rejected_by_the_reviewer():
    graph = build_graph(InMemorySaver(), OfflineAgents())
    _, _, config = _run(graph, "invoice_1017.json")
    graph.invoke(Command(resume={"action": "reject", "note": "No PO"}), config=config)
    state = graph.get_state(config).values
    assert state["payment"]["status"] == "rejected"
    assert db.ledger_find_thread(state["thread_id"])["status"] == "rejected"


def test_model_cannot_loosen_the_floor():
    agents = ScriptedAgents(decisions=[Decision(action="approve", rationale="Looks fine to me")])
    graph = build_graph(InMemorySaver(), agents)
    state, paused, _ = _run(graph, "invoice_1016.json")  # WidgetC is unknown → floor is reject
    assert state["vp_decision"].action == "approve"
    assert state["decision"].action == "reject"
    assert "cannot loosen" in state["decision"].rationale
    assert state["payment"]["status"] == "rejected"


def test_model_can_tighten_the_floor():
    agents = ScriptedAgents(decisions=[Decision(action="escalate", rationale="New vendor, want a second look")])
    graph = build_graph(InMemorySaver(), agents)
    state, paused, _ = _run(graph, "invoice_1015.csv")  # clean → floor approve
    assert state["floor"] == "approve" and state["decision"].action == "escalate" and paused


def test_extraction_critique_loop_reruns_ingest_once():
    truth = expected_invoice("invoice_1011")
    agents = ScriptedAgents(
        extractions=[truth.model_copy(update={"total": None}), truth],
        extraction_critiques=[Critique(ok=False, issues=["total missing: source says $3,000.00"]), Critique(ok=True)],
    )
    graph = build_graph(InMemorySaver(), agents)
    state, paused, _ = _run(graph, "invoice_1011.txt")
    assert agents.calls == {"extract": 2, "critique_extraction": 2, "vp_decide": 1, "critique_decision": 1}
    assert state["extraction_attempts"] == 2
    assert state["decision"].action == "approve" and state["payment"]["status"] == "success"


def test_extraction_loop_is_bounded():
    truth = expected_invoice("invoice_1011")
    agents = ScriptedAgents(
        extractions=[truth, truth, truth],
        extraction_critiques=[Critique(ok=False, issues=["still wrong"])] * 3,
    )
    graph = build_graph(InMemorySaver(), agents)
    state, _, _ = _run(graph, "invoice_1011.txt")
    assert agents.calls["extract"] == 2  # MAX_EXTRACTION_ATTEMPTS
    assert state["decision"] is not None  # it moved on rather than spinning


def test_decision_critique_loop_reconsiders_once_with_the_objection():
    agents = ScriptedAgents(
        decisions=[
            Decision(action="approve", rationale="fine"),
            Decision(action="escalate", rationale="on reflection, check with receiving"),
        ],
        decision_critiques=[Critique(ok=False, issues=["Vendor has never billed us before"]), Critique(ok=True)],
    )
    graph = build_graph(InMemorySaver(), agents)
    state, paused, _ = _run(graph, "invoice_1015.csv")
    assert agents.calls["vp_decide"] == 2
    assert agents.objections_seen == [[], ["Vendor has never billed us before"]]
    assert state["decision"].action == "escalate" and paused


def test_duplicate_is_caught_across_runs_in_the_same_ledger():
    graph = build_graph(InMemorySaver(), OfflineAgents())
    first, _, _ = _run(graph, "invoice_1004.json", thread_id="a")
    assert first["payment"]["status"] == "success"
    revised, paused, _ = _run(graph, "invoice_1004_revised.json", thread_id="b")
    assert paused and [f.code for f in revised["findings"]] == ["DUPLICATE_INVOICE"]
    again, _, _ = _run(graph, "invoice_1004.json", thread_id="c")
    assert again["payment"]["status"] == "rejected"  # same number, same total, already paid
