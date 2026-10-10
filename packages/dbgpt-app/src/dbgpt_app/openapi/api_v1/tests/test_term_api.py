"""Two clients share records without personal identity or cookies."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dbgpt.storage.metadata import DatabaseManager, Model
from dbgpt_app.openapi.api_v1.terms import api
from dbgpt_app.openapi.api_v1.terms.store import TermStore


def test_two_clients_share_crud_and_validation(tmp_path, monkeypatch):
    db = DatabaseManager()
    db.init_db(f"sqlite:///{tmp_path / 'metadata.db'}", base=Model)
    db.create_all()
    monkeypatch.setattr(api, "TermStore", lambda: TermStore(db))
    app = FastAPI()
    app.include_router(api.router)
    first, second = TestClient(app), TestClient(app)
    payload = {"term": "毛利", "definition": "收入减成本", "datasource": "sales"}
    created = first.post("/v1/chat/terms", json=payload)
    assert created.status_code == 200
    record = created.json()
    assert "owner_id" not in record
    assert second.get("/v1/chat/terms?term=毛利").json()[0]["id"] == record["id"]
    assert (
        second.put(
            "/v1/chat/terms/" + record["id"],
            json={**payload, "formula": "收入-材料成本", "formula_confirmed": True},
        ).status_code
        == 200
    )
    assert first.get("/v1/chat/terms").json()[0]["formula_confirmed"]
    assert (
        first.post(
            "/v1/chat/terms",
            json={**payload, "sources": [{"url": "javascript:alert(1)"}]},
        ).status_code
        == 422
    )
    assert (
        first.post("/v1/chat/terms", json={**payload, "definition": "  "}).status_code
        == 422
    )
    assert second.delete("/v1/chat/terms/" + record["id"]).status_code == 200
    assert first.get("/v1/chat/terms").json() == []
    assert first.delete("/v1/chat/terms/missing").status_code == 404
    db.engine.dispose()
