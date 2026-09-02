"""Two output surfaces: a Rich console for people, runs/<ts>/events.jsonl for machines."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from acme_ap import llm

console = Console()
log = logging.getLogger("acme_ap")

RUNS_DIR = Path("runs")

NODE_STYLE = {
    "ingest": "cyan",
    "critique_extraction": "magenta",
    "validate": "yellow",
    "approve": "blue",
    "critique_decision": "magenta",
    "escalate": "bold yellow",
    "human_review": "bold yellow",
    "pay": "bold green",
    "log_rejection": "bold red",
}


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, show_path=False, markup=True, rich_tracebacks=True)],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


class EventLog:
    """Append-only JSONL for one CLI invocation."""

    def __init__(self, base: Path | None = None) -> None:
        base = base or RUNS_DIR
        self.dir = base / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "events.jsonl"

    def record(self, thread_id: str, event: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"thread_id": thread_id, **event}, default=str) + "\n")


def print_event(thread_id: str, event: dict) -> None:
    node = event.get("node", "?")
    style = NODE_STYLE.get(node, "white")
    log.info(f"[dim]{thread_id}[/] [{style}]{node:<20}[/] {event.get('summary', '')}")


def header() -> None:
    console.rule(f"[bold]Acme AP[/] · model: {llm.describe()}")


def usage_line() -> str:
    u = llm.USAGE
    if not u["calls"]:
        return "model calls: 0"
    return f"model calls: {u['calls']} · tokens in/out: {u['input_tokens']}/{u['output_tokens']}"
