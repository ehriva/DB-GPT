"""Confirmed terms stored in the application's metadata database."""

import json
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import Column, DateTime, Index, String, Text

from dbgpt.storage.metadata import Model, db

_WRITE_LOCK = threading.RLock()


def normalize_term(value: str) -> str:
    return value.strip().casefold()


class TermEntity(Model):
    __tablename__ = "shared_term"
    __table_args__ = (Index("ix_shared_term_name", "term_key"),)

    id = Column(String(32), primary_key=True)
    datasource = Column(String(255), nullable=False, default="")
    term = Column(String(255), nullable=False)
    term_key = Column(String(255), nullable=False)
    definition = Column(Text, nullable=False)
    formula = Column(Text, nullable=False, default="")
    formula_confirmed = Column(String(1), nullable=False, default="0")
    aliases_json = Column(Text, nullable=False, default="[]")
    sources_json = Column(Text, nullable=False, default="[]")
    source_type = Column(String(32), nullable=False, default="user")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "datasource": self.datasource,
            "term": self.term,
            "definition": self.definition,
            "formula": self.formula,
            "formula_confirmed": self.formula_confirmed == "1",
            "aliases": json.loads(self.aliases_json),
            "sources": json.loads(self.sources_json),
            "source_type": self.source_type,
            "created_at": self.created_at.isoformat() + "Z",
            "updated_at": self.updated_at.isoformat() + "Z",
            "last_used_at": self.last_used_at.isoformat() + "Z",
        }


class TermStore:
    def __init__(self, manager=None):
        self.db = manager or db

    def find(self, term: str = "", datasource: str = "") -> List[Dict[str, Any]]:
        key = normalize_term(term)
        with self.db.session(commit=False) as session:
            rows = session.query(TermEntity).all()
            results = [
                row.public()
                for row in rows
                if not key
                or row.term_key == key
                or key in [normalize_term(a) for a in json.loads(row.aliases_json)]
            ]
        return sorted(
            results,
            key=lambda r: (r["datasource"] == datasource, r["last_used_at"]),
            reverse=True,
        )

    def save(self, value: Dict[str, Any]) -> Dict[str, Any]:
        term = str(value["term"]).strip()
        definition = str(value["definition"]).strip()
        if not term or not definition:
            raise ValueError("名词和解释不能为空。")
        datasource = value.get("datasource", "") or ""
        formula = (value.get("formula") or "").strip()
        confirmed = "1" if value.get("formula_confirmed") else "0"
        with _WRITE_LOCK, self.db.session() as session:
            matches = (
                session.query(TermEntity)
                .filter_by(datasource=datasource, term_key=normalize_term(term))
                .all()
            )
            row = next(
                (
                    r
                    for r in matches
                    if r.definition == definition
                    and r.formula == formula
                    and r.formula_confirmed == confirmed
                ),
                None,
            )
            if row is None:
                row = TermEntity(
                    id=uuid.uuid4().hex,
                    datasource=datasource,
                    term=term,
                    term_key=normalize_term(term),
                    definition=definition,
                    formula=formula,
                    formula_confirmed=confirmed,
                    aliases_json=json.dumps(
                        value.get("aliases", []), ensure_ascii=False
                    ),
                    sources_json=json.dumps(
                        value.get("sources", []), ensure_ascii=False
                    ),
                    source_type=value.get("source_type", "user"),
                )
                session.add(row)
            else:
                aliases = json.loads(row.aliases_json)
                row.aliases_json = json.dumps(
                    list(dict.fromkeys([*aliases, *value.get("aliases", [])]))[:20],
                    ensure_ascii=False,
                )
                sources = {s["url"]: s for s in json.loads(row.sources_json)}
                sources.update({s["url"]: s for s in value.get("sources", [])})
                row.sources_json = json.dumps(
                    list(sources.values())[:10], ensure_ascii=False
                )
            row.last_used_at = datetime.utcnow()
            session.flush()
            return row.public()

    def update(self, record_id: str, value: Dict[str, Any]):
        with _WRITE_LOCK, self.db.session() as session:
            row = session.query(TermEntity).filter_by(id=record_id).first()
            if row is None:
                raise KeyError("共享术语不存在。")
            for field in ("term", "definition", "formula", "datasource", "source_type"):
                if field in value:
                    setattr(row, field, value[field].strip())
            row.term_key = normalize_term(row.term)
            row.aliases_json = json.dumps(value.get("aliases", []), ensure_ascii=False)
            row.sources_json = json.dumps(value.get("sources", []), ensure_ascii=False)
            row.formula_confirmed = "1" if value.get("formula_confirmed") else "0"
            row.updated_at = datetime.utcnow()
            session.flush()
            return row.public()

    def delete(self, record_id: str) -> None:
        with _WRITE_LOCK, self.db.session() as session:
            row = session.query(TermEntity).filter_by(id=record_id).first()
            if row is None:
                raise KeyError("共享术语不存在。")
            session.delete(row)
