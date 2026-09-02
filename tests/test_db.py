from pathlib import Path

from acme_ap import db


def test_seed_comes_from_csv_when_present():
    assert db.lookup_item("GadgetX") == {"item": "GadgetX", "stock": 5, "unit_price": 750.0}
    assert db.lookup_vendor("TechParts International")["notes"] == "Bills in EUR"


def test_python_seed_when_no_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "SEED_DIR", tmp_path)  # no CSVs here
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "inv.db")
    db.ensure_db()
    assert db.lookup_item("WidgetA")["stock"] == 15


def test_vendor_lookup_is_tolerant_but_not_fuzzy():
    assert db.lookup_vendor("Widgets Inc")["name"] == "Widgets Inc."
    assert db.lookup_vendor("WIDGETS INC")["name"] == "Widgets Inc."
    assert db.lookup_vendor("Widgets, Incorporated")["name"] == "Widgets Inc."
    assert db.lookup_vendor("Widget Inc.") is None  # one letter off is still unknown
    assert db.lookup_vendor("QuickShip Distributers") is None


def test_item_lookup_is_case_insensitive_only():
    assert db.lookup_item("widgeta")["item"] == "WidgetA"
    assert db.lookup_item("WidgetC") is None


def test_seed_dir_default_is_data():
    assert Path(db.SEED_DIR).name == "data"
