"""The VP inbox, and a live view of the graph while it runs. Same graph, same checkpoint as the CLI."""

from __future__ import annotations

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


@st.cache_resource
def mermaid_source() -> str:
    return cli.get_graph().get_graph().draw_mermaid()


def render_graph(placeholder, done: list[str], active: str | None, paused: bool = False) -> None:
    """The compiled graph as Mermaid, with finished nodes green and the running one amber."""
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


def run_live(path) -> dict:
    """Stream the graph node by node, lighting the diagram up as it goes. Returns the summary row."""
    graph = cli.get_graph()
    thread_id = f"{path.stem}-{uuid.uuid4().hex[:6]}"
    events = report.EventLog()
    calls_before = llm.USAGE["calls"]
    diagram = st.empty()
    timeline = st.empty()
    done: list[str] = []
    lines: list[str] = []
    paused = False
    render_graph(diagram, done, "ingest")
    for update in graph.stream(
        {"source_path": str(path), "thread_id": thread_id}, config=cli._config(thread_id), stream_mode="updates"
    ):
        for node, delta in update.items():
            if node == "__interrupt__":
                paused = True
                lines.append(
                    "🧑‍💼 **Human review** — paused; the graph is asleep in the checkpoint until someone decides below"
                )
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


run_col, result_col = st.columns([1, 2])

with run_col:
    st.subheader("Process an invoice")
    files = cli._batch_files(cli.INVOICE_DIR, cli.STRESS_DIR)
    choice = st.selectbox("Invoice", files, format_func=lambda p: f"{p.parent.name}/{p.name}")
    go = st.button("Run", type="primary")
    if st.button("Reset ledger & checkpoints"):
        db.reset_db()
        st.session_state.pop("last", None)
        st.rerun()

with result_col:
    if go:
        st.session_state["last"] = run_live(choice)
    row = st.session_state.get("last")
    if row:
        st.subheader(f"{row['file']} → {row['invoice']}")
        a, b, c, d = st.columns(4)
        a.metric("policy floor", row["floor"])
        b.metric("decision", row["decision"])
        c.metric("status", "paid" if row["status"] == "success" else row["status"])
        d.metric("model calls", row["calls"])
        st.caption(f"findings: {row['findings']}")
        if not go:
            st.dataframe(
                [{"node": e["node"], "what happened": e["summary"]} for e in row["state"].get("events", [])],
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.info(
            "Pick an invoice and press Run to watch it move through the graph, or decide on the paused invoices below."
        )

st.divider()
st.subheader("VP inbox — invoices waiting for a decision")
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
            left.text_area("Source document (no model configured — read it yourself)", payload["raw_text"], height=200)
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

st.divider()
st.subheader("Ledger")
summary = {r["status"]: r for r in db.ledger_summary()}
p, r, w = st.columns(3)
p.metric(
    "Paid straight through",
    f"${summary.get('paid', {}).get('amount', 0):,.2f}",
    f"{summary.get('paid', {}).get('n', 0)} invoices",
)
r.metric(
    "Blocked",
    f"${summary.get('rejected', {}).get('amount', 0):,.2f}",
    f"{summary.get('rejected', {}).get('n', 0)} invoices",
)
w.metric(
    "Waiting on the VP",
    f"${summary.get('pending_review', {}).get('amount', 0):,.2f}",
    f"{summary.get('pending_review', {}).get('n', 0)} invoices",
)
st.dataframe(db.ledger_rows(), use_container_width=True, hide_index=True)
