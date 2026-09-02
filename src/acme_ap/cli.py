"""Command line. `python main.py --invoice_path=...` is the brief's contract; `acme-ap` is the same app."""

from __future__ import annotations

import typer

from acme_ap import db

app = typer.Typer(no_args_is_help=False, add_completion=False, rich_markup_mode="rich")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    invoice_path: str | None = typer.Option(None, "--invoice_path", "--invoice-path", help="Process one invoice"),
) -> None:
    """Acme Corp invoice processing — ingest → validate → approve → pay."""
    if ctx.invoked_subcommand is None:
        if invoice_path is None:
            typer.echo(ctx.get_help())
            raise typer.Exit()
        run(invoice_path)


@app.command("setup-db")
def setup_db(reset: bool = typer.Option(False, "--reset", help="Wipe the ledger and checkpoints too")) -> None:
    """Create and seed inventory.db (runs automatically on first use)."""
    if reset:
        db.reset_db()
        typer.echo(f"Reset and reseeded {db.DB_PATH}")
    else:
        typer.echo(f"{'Seeded' if db.ensure_db() else 'Already present:'} {db.DB_PATH}")


@app.command()
def run(path: str = typer.Argument(..., help="Invoice file")) -> None:
    """Process one invoice end to end."""
    db.ensure_db()
    typer.echo(f"pipeline not wired yet: {path}")
    raise typer.Exit(code=2)
