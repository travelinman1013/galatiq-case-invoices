"""Command line. `python main.py --invoice_path=...` is the brief's contract; `acme-ap` is the same app."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from acme_ap import agents as agents_mod
from acme_ap import db, llm, readers, report
from acme_ap import graph as graph_mod

app = typer.Typer(no_args_is_help=False, add_completion=False, rich_markup_mode="rich")
INVOICE_DIR = Path("data/invoices")

_graph: Any = None


def get_graph():
    """The compiled workflow over the on-disk checkpointer. Built once per process."""
    global _graph
    if _graph is None:
        from langgraph.checkpoint.sqlite import SqliteSaver

        db.ensure_db()
        db.CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db.CHECKPOINT_PATH, check_same_thread=False)
        _graph = graph_mod.build_graph(SqliteSaver(conn, serde=graph_mod.checkpoint_serde()), agents_mod.build_agents())
    return _graph


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _drive(graph, payload, thread_id: str, events: report.EventLog) -> tuple[dict, bool]:
    """Stream node updates, narrating each one. Returns (final state, paused?)."""
    paused = False
    for update in graph.stream(payload, config=_config(thread_id), stream_mode="updates"):
        for node, delta in update.items():
            if node == "__interrupt__":
                paused = True
                continue
            for event in (delta or {}).get("events", []):
                report.print_event(thread_id, event)
                events.record(thread_id, event)
    return graph.get_state(_config(thread_id)).values, paused


def _jsonable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    return obj


def _process(path: Path, events: report.EventLog) -> dict:
    """Run one invoice through the graph; return a summary row."""
    graph = get_graph()
    thread_id = f"{path.stem}-{uuid.uuid4().hex[:6]}"
    calls_before = llm.USAGE["calls"]
    state, paused = _drive(graph, {"source_path": str(path), "thread_id": thread_id}, thread_id, events)
    invoice = state.get("invoice")
    decision = state.get("decision")
    payment = state.get("payment") or {}
    if paused:
        status = "PAUSED"
    else:
        status = payment.get("status", "?")
    return {
        "file": path.name,
        "invoice": (invoice.invoice_number if invoice else None) or path.stem,
        "source": invoice.source_kind if invoice else "?",
        "findings": ", ".join(f.code for f in state.get("findings", [])) or "clean",
        "floor": state.get("floor", "?"),
        "decision": decision.action if decision else "?",
        "status": status,
        "calls": llm.USAGE["calls"] - calls_before,
        "thread_id": thread_id,
        "state": state,
    }


STATUS_STYLE = {"success": "green", "paid": "green", "rejected": "red", "PAUSED": "yellow"}


def _summary_table(rows: list[dict]) -> Table:
    table = Table(title="Invoice run", show_lines=False)
    for col in ("file", "invoice", "source", "findings", "floor", "decision", "status", "calls"):
        table.add_column(col, overflow="fold")
    for r in rows:
        status = "paid" if r["status"] == "success" else r["status"]
        table.add_row(
            r["file"],
            r["invoice"],
            r["source"],
            r["findings"],
            r["floor"],
            r["decision"],
            f"[{STATUS_STYLE.get(status, 'white')}]{status}[/]",
            str(r["calls"]),
        )
    return table


def _batch_files() -> list[Path]:
    """Every sample except PDFs that have a text/JSON twin (those are for the extraction test)."""
    files = sorted(
        p for p in INVOICE_DIR.iterdir() if p.suffix.lower() in readers.STRUCTURED_SUFFIXES | readers.TEXT_SUFFIXES
    )
    stems = {p.stem for p in files if p.suffix.lower() != ".pdf"}
    return [p for p in files if not (p.suffix.lower() == ".pdf" and p.stem in stems)]


# -- commands -------------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    invoice_path: str | None = typer.Option(None, "--invoice_path", "--invoice-path", help="Process one invoice"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Acme Corp invoice processing — ingest → validate → approve → pay."""
    report.setup_logging(verbose)
    if ctx.invoked_subcommand is None:
        if invoice_path is None:
            typer.echo(ctx.get_help())
            raise typer.Exit()
        run(path=invoice_path, all_=False, reset=False, as_json=False)


@app.command()
def run(
    path: str | None = typer.Argument(None, help="Invoice file (or use --all)"),
    all_: bool = typer.Option(False, "--all", help="Run every sample in data/invoices"),
    reset: bool = typer.Option(False, "--reset", help="Wipe the ledger and checkpoints first"),
    as_json: bool = typer.Option(False, "--json", help="Print the final state as JSON"),
) -> None:
    """Process one invoice end to end, or all of them."""
    if reset:
        db.reset_db()
    if not path and not all_:
        raise typer.BadParameter("give an invoice path or --all")
    llm.reset_usage()
    try:
        llm.preflight()
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    report.header()
    events = report.EventLog()
    files = _batch_files() if all_ else [Path(path)]
    rows = []
    for file in files:
        if not file.exists():
            raise typer.BadParameter(f"no such file: {file}")
        rows.append(_process(file, events))
    if as_json:
        typer.echo(json.dumps([{k: _jsonable(v) for k, v in r.items()} for r in rows], indent=2))
        return
    report.console.print()
    report.console.print(_summary_table(rows))
    paused = [r for r in rows if r["status"] == "PAUSED"]
    if paused:
        report.console.print(
            f"[yellow]{len(paused)} invoice(s) are waiting for a human.[/] "
            f"See them with [bold]acme-ap queue[/]; "
            f"decide with [bold]acme-ap resume {paused[0]['invoice']} --approve[/]"
        )
    report.console.print(f"[dim]{report.usage_line()} · events: {events.path}[/]")


@app.command()
def queue() -> None:
    """Invoices parked for VP review (paused graph threads)."""
    graph = get_graph()
    pending = db.ledger_pending()
    if not pending:
        report.console.print("The VP queue is empty.")
        return
    table = Table(title="VP queue — paused invoices")
    for col in ("invoice", "vendor", "total", "floor", "why", "resume with"):
        table.add_column(col, overflow="fold")
    for row in pending:
        snapshot = graph.get_state(_config(row["thread_id"]))
        payload = _interrupt_payload(snapshot)
        table.add_row(
            row["invoice_number"] or row["file_stem"],
            row["vendor"] or "—",
            f"{row['total']:,.2f}" if row["total"] is not None else "—",
            payload.get("floor", "?"),
            "\n".join(payload.get("findings", [])) or payload.get("vp_rationale", ""),
            f"acme-ap resume {row['invoice_number'] or row['file_stem']} --approve|--reject",
        )
    report.console.print(table)


def _interrupt_payload(snapshot) -> dict:
    interrupts = getattr(snapshot, "interrupts", None) or ()
    if not interrupts:
        for task in getattr(snapshot, "tasks", ()):
            interrupts = getattr(task, "interrupts", ()) or ()
            if interrupts:
                break
    return dict(interrupts[0].value) if interrupts else {}


@app.command()
def resume(
    key: str = typer.Argument(..., help="Invoice number, file stem, or thread id"),
    approve: bool = typer.Option(False, "--approve"),
    reject: bool = typer.Option(False, "--reject"),
    note: str = typer.Option("", "--note", help="Reviewer note recorded with the decision"),
) -> None:
    """Answer a paused invoice as the VP: the graph wakes up and finishes."""
    if approve == reject:
        raise typer.BadParameter("choose exactly one of --approve / --reject")
    row = db.ledger_find_thread(key)
    if not row:
        raise typer.BadParameter(f"nothing in the ledger matches {key!r}")
    if row["status"] != "pending_review":
        raise typer.BadParameter(f"{key} is not waiting for review (status: {row['status']})")
    from langgraph.types import Command

    report.header()
    events = report.EventLog()
    action = "approve" if approve else "reject"
    state, paused = _drive(get_graph(), Command(resume={"action": action, "note": note}), row["thread_id"], events)
    payment = state.get("payment") or {}
    status = "paid" if payment.get("status") == "success" else payment.get("status", "?")
    report.console.print(
        f"{row['invoice_number'] or row['file_stem']}: [bold]{status}[/] — {state['decision'].rationale}"
    )


@app.command()
def ledger() -> None:
    """Business view: what was paid straight through, what was blocked, what is waiting."""
    db.ensure_db()
    summary = {r["status"]: r for r in db.ledger_summary()}

    def line(status: str, label: str) -> str:
        r = summary.get(status)
        return f"{label}: {r['n']} invoice(s), ${r['amount']:,.2f}" if r else f"{label}: 0"

    report.console.print(f"[green]{line('paid', 'Paid straight through')}[/]")
    report.console.print(f"[red]{line('rejected', 'Blocked')}[/]")
    report.console.print(f"[yellow]{line('pending_review', 'Waiting on the VP')}[/]")
    table = Table(title="Ledger")
    for col in ("when", "invoice", "vendor", "total", "status", "reason"):
        table.add_column(col, overflow="fold")
    for r in db.ledger_rows():
        table.add_row(
            r["decided_at"],
            r["invoice_number"] or r["file_stem"],
            r["vendor"] or "—",
            f"{r['total']:,.2f}" if r["total"] is not None else "—",
            f"[{STATUS_STYLE.get(r['status'], 'yellow')}]{r['status']}[/]",
            (r["reason"] or "")[:160],
        )
    report.console.print(table)


@app.command("graph")
def graph_cmd() -> None:
    """Print the workflow as Mermaid — the README diagram is generated from the code."""
    compiled = graph_mod.build_graph(None, agents_mod.OfflineAgents())
    typer.echo(compiled.get_graph().draw_mermaid())


@app.command()
def doctor() -> None:
    """Check the configured model provider accepts the key before you demo."""
    try:
        report.console.print(f"[green]ok[/] {llm.preflight()}")
    except RuntimeError as exc:
        report.console.print(f"[red]FAIL[/] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("setup-db")
def setup_db(reset: bool = typer.Option(False, "--reset", help="Wipe the ledger and checkpoints too")) -> None:
    """Create and seed inventory.db (runs automatically on first use)."""
    if reset:
        db.reset_db()
        typer.echo(f"Reset and reseeded {db.DB_PATH}")
    else:
        typer.echo(f"{'Seeded' if db.ensure_db() else 'Already present:'} {db.DB_PATH}")
