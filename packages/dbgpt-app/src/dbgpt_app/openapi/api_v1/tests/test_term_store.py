"""Persistence and scope regressions independent of LLM output."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from dbgpt.storage.metadata import DatabaseManager, Model
from dbgpt_app.openapi.api_v1.terms.store import TermStore


def manager(path):
    db = DatabaseManager()
    db.init_db(f"sqlite:///{path}", base=Model)
    db.create_all()
    return db


def test_shared_persistence_aliases_multiple_definitions_and_history_snapshot(tmp_path):
    path = tmp_path / "metadata.db"
    db = manager(path)
    store = TermStore(db)
    value = {
        "term": " ARR ",
        "definition": "年度经常性收入",
        "datasource": "sales",
        "aliases": ["年度订阅收入"],
        "formula": "月订阅收入 * 12",
        "formula_confirmed": True,
    }
    first = store.save(value)
    assert TermStore(db).save(value)["id"] == first["id"]
    other = store.save({**value, "definition": "会计收益率"})
    cross = store.save({**value, "datasource": "other"})
    assert len(store.find("年度订阅收入", "sales")) == 3
    assert store.find("arr", "sales")[-1]["id"] == cross["id"]
    store.update(first["id"], {**value, "definition": "修订的口径"})
    assert first["definition"] == "年度经常性收入"
    store.delete(other["id"])
    db.engine.dispose()
    reopened = manager(path)
    assert len(TermStore(reopened).find("arr", "sales")) == 2
    assert TermStore(reopened).find("arr", "sales")[0]["definition"] == "修订的口径"
    reopened.engine.dispose()


def test_unconfirmed_formula_and_missing_record(tmp_path):
    db = manager(tmp_path / "metadata.db")
    store = TermStore(db)
    result = store.save({"term": "毛利", "definition": "收入减成本"})
    assert result["formula_confirmed"] is False
    with pytest.raises(KeyError):
        store.delete("missing")
    db.engine.dispose()


def test_two_simultaneous_clients_do_not_duplicate_definition(tmp_path):
    db = manager(tmp_path / "metadata.db")
    value = {"term": "毛利", "definition": "收入减材料成本"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(lambda _: TermStore(db).save(value), range(2)))
    assert records[0]["id"] == records[1]["id"]
    assert len(TermStore(db).find("毛利")) == 1
    db.engine.dispose()
