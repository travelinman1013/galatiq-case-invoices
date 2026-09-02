"""The VP inbox. A web page that approves what the CLI paused — same graph, same checkpoint."""

from __future__ import annotations

import streamlit as st
from langgraph.types import Command

from acme_ap import cli, db, llm, report

st.set_page_config(page_title="Acme AP · invoice control", layout="wide")
st.title("Acme Corp · invoice processing")
st.caption(f"model: {llm.describe()}")

run_col, result_col = st.columns([1, 2])

with run_col:
    st.subheader("Process an invoice")
    files = cli._batch_files()
    choice = st.selectbox("Sample invoice", files, format_func=lambda p: p.name)
    if st.button("Run", type="primary"):
        with st.spinner(f"Running {choice.name} through the graph…"):
            st.session_state["last"] = cli._process(choice, report.EventLog())
    if st.button("Reset ledger & checkpoints"):
        db.reset_db()
        st.session_state.pop("last", None)
        st.rerun()

with result_col:
    row = st.session_state.get("last")
    if row:
        st.subheader(f"{row['file']} → {row['invoice']}")
        a, b, c, d = st.columns(4)
        a.metric("policy floor", row["floor"])
        b.metric("decision", row["decision"])
        c.metric("status", "paid" if row["status"] == "success" else row["status"])
        d.metric("model calls", row["calls"])
        st.caption(f"findings: {row['findings']}")
        events = row["state"].get("events", [])
        st.dataframe(
            [{"node": e["node"], "what happened": e["summary"]} for e in events],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Pick a sample invoice and press Run, or decide on the paused invoices below.")

st.divider()
st.subheader("VP inbox — invoices waiting for a decision")
pending = db.ledger_pending()
if not pending:
    st.success("Nothing waiting. Run `acme-ap run --all` (or press Run above) to fill the queue.")
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
