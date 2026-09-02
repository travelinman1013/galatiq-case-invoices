"""Acme AP in a browser: watch one invoice run, decide on what paused, read any run's audit trail.

Same graph and same checkpoint as the CLI. The ledger row is the audit index; the checkpoint is the trail.
"""

from __future__ import annotations

import sqlite3
import uuid

import streamlit as st
import streamlit.components.v1 as components
from langgraph.types import Command

from acme_ap import cli, db, llm, report

st.set_page_config(page_title="Acme AP · invoice control", layout="wide")
st.title("Acme Corp · invoice processing")
st.caption(f"model: {llm.describe()}")

NODE_LABEL = {
    "ingest": "Ingest",
    "critique_extraction": "Extraction critic",
    "validate": "Validate (code)",
    "approve": "VP agent",
    "critique_decision": "Decision critic",
    "escalate": "Escalate",
    "human_review": "Human review",
    "pay": "Pay",
    "log_rejection": "Reject",
}
ICON = {"pay": "✅", "log_rejection": "⛔", "escalate": "⏸️", "human_review": "🧑‍💼"}
STATUS_BADGE = {"paid": "🟢 paid", "rejected": "🔴 rejected", "pending_review": "🟠 waiting on the VP"}


@st.cache_resource
def mermaid_source() -> str:
    return cli.get_graph().get_graph().draw_mermaid()


def render_graph(placeholder, done: list[str], active: str | None, paused: bool = False) -> None:
    """The compiled graph as Mermaid: finished nodes green, the running one amber, a pause orange."""
    src = mermaid_source()
    src += "\nclassDef done fill:#c8e6c9,stroke:#2e7d32,color:#1b5e20;"
    src += "\nclassDef active fill:#ffe082,stroke:#ff8f00,color:#000;"
    src += "\nclassDef paused fill:#ffcc80,stroke:#ef6c00,color:#000;"
    if done:
        src += f"\nclass {','.join(dict.fromkeys(done))} done;"
    if active:
        src += f"\nclass {active} active;"
    if paused:
        src += "\nclass human_review paused;"
    html = f"""
    <script type="module">
      import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
      mermaid.initialize({{ startOnLoad: true, theme: 'neutral', flowchart: {{ curve: 'linear' }} }});
    </script>
    <pre class="mermaid" style="text-align:center">{src}</pre>
    """
    with placeholder:
        components.html(html, height=560, scrolling=True)


def _next_guess(node: str, delta: dict) -> str | None:
    """Which node is about to run, for the amber highlight. Best effort; the stream corrects it."""
    if node == "ingest":
        inv = (delta or {}).get("invoice")
        return "critique_extraction" if inv is not None and inv.source_kind == "llm" else "validate"
    if node == "critique_extraction":
        crit = (delta or {}).get("extraction_critique")
        return "validate" if crit is None or crit.ok else "ingest"
    if node == "validate":
        return "approve"
    if node == "approve":
        return "critique_decision"
    if node == "critique_decision":
        crit = (delta or {}).get("decision_critique")
        return "approve" if crit is not None and not crit.ok else None
    if node == "escalate":
        return "human_review"
    return None


def run_live(path, diagram, timeline) -> dict:
    """Stream the graph node by node into the two placeholders. Returns the CLI's summary row."""
    graph = cli.get_graph()
    thread_id = f"{path.stem}-{uuid.uuid4().hex[:6]}"
    events = report.EventLog()
    calls_before = llm.USAGE["calls"]
    done: list[str] = []
    lines: list[str] = []
    paused = False
    render_graph(diagram, done, "ingest")
    payload = {"source_path": str(path), "thread_id": thread_id}
    for update in graph.stream(payload, config=cli._config(thread_id), stream_mode="updates"):
        for node, delta in update.items():
            if node == "__interrupt__":
                paused = True
                lines.append("🧑‍💼 **Human review** — paused; asleep in the checkpoint until the VP inbox decides")
                render_graph(diagram, done, None, paused=True)
                timeline.markdown("\n\n".join(lines))
                continue
            done.append(node)
            for event in (delta or {}).get("events", []):
                events.record(thread_id, event)
                lines.append(f"{ICON.get(node, '▸')} **{NODE_LABEL.get(node, node)}** — {event.get('summary', '')}")
            render_graph(diagram, done, None if node in ("pay", "log_rejection") else _next_guess(node, delta))
            timeline.markdown("\n\n".join(lines))
    return cli.summarize(graph, path, thread_id, paused, llm.USAGE["calls"] - calls_before)


def trail_view(trail: dict) -> None:
    """One run's audit trail, read back from its checkpoint."""
    a, b, c = st.columns(3)
    a.metric("policy floor", trail["floor"] or "—")
    b.metric("final decision", (trail["decision"] or {}).get("action", "—"))
    c.metric("source", trail["source_kind"] or "—")
    if trail["findings"]:
        st.markdown("**Findings**\n\n" + "\n".join(f"- {f}" for f in trail["findings"]))
    else:
        st.markdown("**Findings** — none")
    if trail["vp_decision"]:
        st.markdown(f"**VP agent** — *{trail['vp_decision']['action']}*: {trail['vp_decision']['rationale']}")
    crit = trail["decision_critique"]
    if crit and not crit["ok"]:
        st.markdown("**Decision critic objected** — " + "; ".join(crit["issues"]))
    if trail["human"]:
        st.markdown(f"**Human reviewer** — {trail['human'].get('action')} {trail['human'].get('note') or ''}")
    if trail["payment"]:
        st.markdown(f"**Outcome** — {trail['payment']}")
    st.dataframe(
        [
            {"when": e.get("ts", ""), "node": e.get("node", ""), "what happened": e.get("summary", "")}
            for e in trail["events"]
        ],
        width="stretch",
        hide_index=True,
    )


# ---- top: run one invoice live -------------------------------------------------------------

run_col, live_col = st.columns([1, 2])
with run_col:
    st.subheader("Process an invoice")
    files = cli._batch_files(cli.INVOICE_DIR, cli.STRESS_DIR)
    choice = st.selectbox("Invoice", files, format_func=lambda p: f"{p.parent.name}/{p.name}")
    go = st.button("Run", type="primary")
    if st.button("Reset ledger & checkpoints"):
        cli.reset_graph()  # never keep a connection to a file that is about to be deleted
        db.reset_db()
        st.session_state.pop("last", None)
        st.rerun()

with live_col:
    diagram = st.empty()
    timeline = st.empty()
    summary = st.empty()
    if go:
        st.session_state.pop("last", None)  # the previous run's result never overlaps the new one
        try:
            st.session_state["last"] = run_live(choice, diagram, timeline)
        except sqlite3.OperationalError:
            # the checkpoint file was replaced underneath us (a CLI --reset); reconnect once and retry
            cli.reset_graph()
            st.session_state["last"] = run_live(choice, diagram, timeline)
    row = st.session_state.get("last")
    if row and row["file"] == choice.name:
        with summary.container():
            a, b, c, d = st.columns(4)
            a.metric("policy floor", row["floor"])
            b.metric("decision", row["decision"])
            c.metric("status", "paid" if row["status"] == "success" else row["status"])
            d.metric("model calls", row["calls"])
            st.caption(f"findings: {row['findings']} · full trail in the Audit tab")
    elif not go:
        diagram.info(
            "Pick an invoice and press Run to watch it move through the graph. "
            "Earlier runs live in the Audit trail tab."
        )

st.divider()
inbox_tab, audit_tab, ledger_tab = st.tabs(["VP inbox", "Audit trail", "Ledger"])

# ---- VP inbox ----------------------------------------------------------------------------------

with inbox_tab:
    pending = db.ledger_pending()
    if not pending:
        st.success("Nothing waiting. Run an invoice above, or `acme-ap run --all` from the CLI, to fill the queue.")
    for item in pending:
        label = item["invoice_number"] or item["file_stem"]
        total = f"${item['total']:,.2f}" if item["total"] is not None else "—"
        payload = cli._interrupt_payload(cli.get_graph().get_state(cli._config(item["thread_id"])))
        with st.container(border=True):
            left, right = st.columns([3, 1])
            left.markdown(f"**{label}** · {item['vendor'] or 'vendor unknown'} · {total}")
            for finding in payload.get("findings", []):
                left.markdown(f"- {finding}")
            if payload.get("raw_text"):
                left.text_area(
                    "Source document (no model configured — read it yourself)", payload["raw_text"], height=200
                )
            left.caption(payload.get("vp_rationale", ""))
            note = right.text_input("Note", key=f"note-{item['thread_id']}", placeholder="optional")
            if right.button("Approve", key=f"ok-{item['thread_id']}", type="primary"):
                cli._drive(
                    cli.get_graph(),
                    Command(resume={"action": "approve", "note": note}),
                    item["thread_id"],
                    report.EventLog(),
                )
                st.rerun()
            if right.button("Reject", key=f"no-{item['thread_id']}"):
                cli._drive(
                    cli.get_graph(),
                    Command(resume={"action": "reject", "note": note}),
                    item["thread_id"],
                    report.EventLog(),
                )
                st.rerun()

# ---- Audit trail -------------------------------------------------------------------------------

with audit_tab:
    rows = db.ledger_rows(newest_first=True)
    if not rows:
        st.info("No runs yet.")
    st.caption("One entry per run, newest first. Each expands into the full trail read back from the graph checkpoint.")
    for r in rows:
        label = r["invoice_number"] or r["file_stem"]
        total = f"${r['total']:,.2f}" if r["total"] is not None else "—"
        badge = STATUS_BADGE.get(r["status"], r["status"])
        header = f"{label} · {r['vendor'] or '—'} · {total} · {badge} · {r['decided_at']}"
        with st.expander(header):
            trail_view(cli.audit_trail(r["thread_id"]))

# ---- Ledger ------------------------------------------------------------------------------------

with ledger_tab:
    summary_rows = {r["status"]: r for r in db.ledger_summary()}
    p, r, w = st.columns(3)
    p.metric(
        "Paid straight through",
        f"${summary_rows.get('paid', {}).get('amount', 0):,.2f}",
        f"{summary_rows.get('paid', {}).get('n', 0)} invoices",
    )
    r.metric(
        "Blocked",
        f"${summary_rows.get('rejected', {}).get('amount', 0):,.2f}",
        f"{summary_rows.get('rejected', {}).get('n', 0)} invoices",
    )
    w.metric(
        "Waiting on the VP",
        f"${summary_rows.get('pending_review', {}).get('amount', 0):,.2f}",
        f"{summary_rows.get('pending_review', {}).get('n', 0)} invoices",
    )
    st.dataframe(
        [
            {k: v for k, v in row.items() if k not in ("id", "thread_id", "file_stem")}
            for row in db.ledger_rows(newest_first=True)
        ],
        width="stretch",
        hide_index=True,
    )
