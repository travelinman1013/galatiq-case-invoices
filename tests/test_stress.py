"""The stress set: shapes the sample data never showed. Offline where possible; the rest via -m llm."""

from __future__ import annotations

import csv

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from acme_ap import policy, readers, validate
from acme_ap.agents import OfflineAgents
from acme_ap.graph import build_graph
from tests.conftest import ROOT, STRESS, expected_invoice
from tests.fakes import ScriptedAgents

with (ROOT / "data" / "expected_outcomes.csv").open(newline="") as fh:
    EXPECTED = {row["file"]: row["floor"] for row in csv.DictReader(fh)}

STRUCTURED = ["stress_2001.json", "stress_2002.csv", "stress_2006.json", "stress_2009.json"]
FIXTURED = ["stress_2003", "stress_2004", "stress_2005", "stress_2007", "stress_2008"]


@pytest.mark.parametrize("name", STRUCTURED)
def test_structured_stress_files_hit_the_expected_floor(name):
    inv = readers.load(STRESS / name)
    assert readers.looks_parsed(inv)
    floor, _ = policy.floor(validate.run_checks(inv), inv.total)
    assert floor == EXPECTED[name]


@pytest.mark.parametrize("stem", FIXTURED)
def test_expected_extractions_hit_the_expected_floor(stem):
    inv = expected_invoice(stem)
    floor, _ = policy.floor(validate.run_checks(inv), inv.total)
    assert floor == EXPECTED[next(k for k in EXPECTED if k.startswith(stem))]


def test_thirty_lines_aggregate_before_the_stock_check():
    findings = validate.run_checks(expected_invoice("stress_2005"))
    stock = [f for f in findings if f.code == "STOCK_EXCEEDED"]
    assert [f.item for f in stock] == ["GadgetX"] and "10 against 5" in stock[0].message


def test_european_amounts_and_eur_currency():
    inv = expected_invoice("stress_2004")
    assert inv.total == 1600 and inv.currency == "EUR"
    assert {f.code for f in validate.run_checks(inv)} == {"CURRENCY_NOT_USD"}


def test_uppercase_hyphenated_invoice_is_clean():
    inv = expected_invoice("stress_2008")
    assert [li.item for li in inv.line_items] == ["WIDGETA", "WIDGETB"]
    assert validate.run_checks(inv) == []


def test_unknown_structured_shape_falls_back_to_the_extractor():
    agents = ScriptedAgents(extractions=[expected_invoice("stress_2003")])
    graph = build_graph(InMemorySaver(), agents)
    config = {"configurable": {"thread_id": "t-2003"}}
    graph.invoke({"source_path": str(STRESS / "stress_2003.xml"), "thread_id": "t-2003"}, config=config)
    state = graph.get_state(config).values
    assert agents.calls["extract"] == 1
    assert "handing the raw file to the extractor" in state["events"][0]["summary"]
    assert state["decision"].action == "approve" and state["payment"]["status"] == "success"


def test_unknown_structured_shape_offline_goes_to_a_human():
    graph = build_graph(InMemorySaver(), OfflineAgents())
    config = {"configurable": {"thread_id": "t-2003-offline"}}
    result = graph.invoke(
        {"source_path": str(STRESS / "stress_2003.xml"), "thread_id": "t-2003-offline"}, config=config
    )
    assert "__interrupt__" in result
    assert "INV-2003" in result["__interrupt__"][0].value["raw_text"]
