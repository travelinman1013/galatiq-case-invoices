"""SQLite: the mock inventory, a vendor master, and the payment ledger.

Plain functions; `agents.py` wraps the lookups as tools. Paths are module constants so tests
can point them at a temp dir.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DB_PATH = Path(os.getenv("ACME_DB_PATH", "inventory.db"))
CHECKPOINT_PATH = Path(os.getenv("ACME_CHECKPOINT_PATH", "runs/checkpoints.db"))
# Optional CSV seeds (data/inventory.csv, data/vendors.csv) override the built-in seed below,
# so a different dataset can bring its own catalog without touching Python.
SEED_DIR = Path(os.getenv("ACME_SEED_DIR", "data"))

# The brief's seed, plus catalog prices so pricing can be validated.
INVENTORY_SEED = [
    ("WidgetA", 15, 250.00),
    ("WidgetB", 10, 500.00),
    ("GadgetX", 5, 750.00),
    ("FakeItem", 0, 1000.00),
]

# Every vendor in the sample set is approved except the two that only appear on bad invoices.
# FastShip is seeded so the "formerly FastShip Ltd." rebrand on INV-1012 is a lead, not a match.
VENDOR_SEED = [
    ("Widgets Inc.", 1, None),
    ("Gadgets Co.", 1, None),
    ("Precision Parts Ltd.", 1, None),
    ("Global Supply Chain Partners", 1, None),
    ("Acme Industrial Supplies", 1, None),
    ("MegaWidgets Corp", 1, None),
    ("Consolidated Materials Group", 1, None),
    ("Summit Manufacturing Co.", 1, None),
    ("Atlas Industrial Supply", 1, None),
    ("TechParts International", 1, "Bills in EUR"),
    ("Reliable Components Inc.", 1, None),
    ("FastShip Ltd.", 1, "May have rebranded as QuickShip Distributers — unverified"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    item TEXT PRIMARY KEY COLLATE NOCASE,
    stock INTEGER NOT NULL,
    unit_price REAL
);
CREATE TABLE IF NOT EXISTS vendors (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    approved INTEGER NOT NULL DEFAULT 1,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_number TEXT,
    file_stem TEXT,
    thread_id TEXT UNIQUE,
    vendor TEXT,
    total REAL,
    status TEXT NOT NULL,
    reason TEXT,
    decided_at TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _csv_seed(name: str, columns: int) -> list[tuple] | None:
    import csv

    path = SEED_DIR / name
    if not path.exists():
        return None
    with path.open(newline="", encoding="utf-8") as fh:
        rows = [tuple((cell.strip() or None) for cell in row[:columns]) for row in csv.reader(fh)]
    rows = [r for r in rows[1:] if r and r[0]]  # skip the header
    if name == "inventory.csv":
        return [(item, int(stock or 0), float(price) if price else None) for item, stock, price in rows]
    return [(vendor, int(approved or 1), notes) for vendor, approved, notes in rows]


def ensure_db() -> bool:
    """Create and seed the database if it does not exist. Returns True when it seeded."""
    fresh = not DB_PATH.exists()
    with connect() as conn:
        conn.executescript(SCHEMA)
        if fresh:
            inventory = _csv_seed("inventory.csv", 3) or INVENTORY_SEED
            vendors = _csv_seed("vendors.csv", 3) or VENDOR_SEED
            conn.executemany("INSERT INTO inventory VALUES (?, ?, ?)", inventory)
            conn.executemany("INSERT INTO vendors VALUES (?, ?, ?)", vendors)
    return fresh


def reset_db() -> None:
    """Wipe the inventory DB and the graph checkpoints; reseed."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
    ensure_db()


# ---- lookups (also exposed to the VP agent as tools) -------------------------------------


def lookup_item(item: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM inventory WHERE item = ?", (item,)).fetchone()
    return dict(row) if row else None


def catalog_items() -> list[str]:
    with connect() as conn:
        return [r["item"] for r in conn.execute("SELECT item FROM inventory ORDER BY item")]


def lookup_vendor(name: str) -> dict | None:
    """Exact (case-insensitive) match first, then punctuation/suffix-insensitive: "Widgets Inc" finds
    "Widgets Inc.". Unknown names stay unknown — there is no fuzzy matching."""
    from acme_ap.normalize import canon_vendor

    with connect() as conn:
        row = conn.execute("SELECT * FROM vendors WHERE name = ?", (name,)).fetchone()
        if row:
            return dict(row)
        key = canon_vendor(name)
        if not key:
            return None
        for candidate in conn.execute("SELECT * FROM vendors"):
            if canon_vendor(candidate["name"]) == key:
                return dict(candidate)
    return None


def vendor_names() -> list[str]:
    with connect() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM vendors ORDER BY name")]


def ledger_history(invoice_number: str | None) -> list[dict]:
    if not invoice_number:
        return []
    with connect() as conn:
        rows = conn.execute("SELECT * FROM ledger WHERE invoice_number = ? ORDER BY id", (invoice_number,)).fetchall()
    return [dict(r) for r in rows]


# ---- writes ---------------------------------------------------------------------------------


def ledger_write(
    *,
    invoice_number: str | None,
    file_stem: str,
    thread_id: str,
    vendor: str | None,
    total: float | None,
    status: str,
    reason: str | None,
) -> None:
    """Insert or update the ledger row for this thread (one row per run)."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ledger (invoice_number, file_stem, thread_id, vendor, total, status, reason, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                status = excluded.status, reason = excluded.reason, decided_at = excluded.decided_at,
                vendor = excluded.vendor, total = excluded.total, invoice_number = excluded.invoice_number
            """,
            (invoice_number, file_stem, thread_id, vendor, total, status, reason, now),
        )


def ledger_pending() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM ledger WHERE status = 'pending_review' ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def ledger_find_thread(key: str) -> dict | None:
    """Resolve an invoice number, file stem, or thread id to its most recent ledger row."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM ledger
            WHERE thread_id = ? OR invoice_number = ? OR file_stem = ?
            ORDER BY (status = 'pending_review') DESC, id DESC LIMIT 1
            """,
            (key, key, key),
        ).fetchone()
    return dict(row) if row else None


def ledger_summary() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(total), 0) AS amount "
            "FROM ledger GROUP BY status ORDER BY status"
        ).fetchall()
    return [dict(r) for r in rows]


def ledger_rows() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM ledger ORDER BY id")]
