"""Tests for multi-schema reflection and schema-qualified identifiers.

Uses SQLite with ``ATTACH DATABASE`` to emulate the Pusula/MedipolDB layout
(tables spread across several named schemas) without needing a live PostgreSQL
server.
"""

import asyncio

import pytest
from sqlalchemy import create_engine, event, text

from dbgpt.agent.expand.deepeye_sql.multischema import (
    SchemaAwareConnector,
    wrap_multi_schema,
)
from dbgpt.agent.expand.deepeye_sql.schema_profile import SchemaProfile
from dbgpt.agent.expand.deepeye_sql.util import quote_ident, split_qualified


def _make_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _attach(dbapi_conn, _record):
        dbapi_conn.execute('ATTACH DATABASE ":memory:" AS "Hasta"')
        dbapi_conn.execute('ATTACH DATABASE ":memory:" AS "Tedavi"')

    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE "Hasta"."Hasta" ('
                          '"Id" INTEGER PRIMARY KEY, "Adi" TEXT, "CinsiyetId" TEXT)'))
        conn.execute(text('CREATE TABLE "Hasta"."Protokol" ('
                          '"Id" INTEGER PRIMARY KEY, "HastaId" INTEGER, '
                          'FOREIGN KEY ("HastaId") REFERENCES "Hasta"("Id"))'))
        conn.execute(text('CREATE TABLE "Tedavi"."ICD_ToMerkez" ('
                          '"Id" INTEGER PRIMARY KEY, "Kodu" TEXT)'))
        conn.execute(text('INSERT INTO "Hasta"."Hasta" VALUES (1, \'Alice\', \'K\'), '
                          '(2, \'Bob\', \'E\')'))
        conn.execute(text('INSERT INTO "Hasta"."Protokol" VALUES (1, 1), (2, 2)'))
        conn.execute(text('INSERT INTO "Tedavi"."ICD_ToMerkez" VALUES (1, \'A00\')'))
    return engine


class _FakeConnector:
    """Minimal RDBMSConnector stand-in exposing `_engine`, dialect and run()."""

    dialect = "sqlite"

    def __init__(self, engine):
        self._engine = engine

    def run(self, sql, fetch="all"):
        with self._engine.connect() as c:
            res = c.execute(text(sql))
            if res.returns_rows:
                cols = list(res.keys())
                return [tuple(cols)] + [list(r) for r in res.fetchall()]
            return []

    def query_ex(self, sql, fetch="all", timeout=None):
        with self._engine.connect() as c:
            res = c.execute(text(sql))
            if res.returns_rows:
                return list(res.keys()), [list(r) for r in res.fetchall()]
            return [], []

    def get_current_db_name(self):
        return "main"


@pytest.fixture
def connector():
    return SchemaAwareConnector(_FakeConnector(_make_engine()), schemas=["Hasta", "Tedavi"])


class TestQuoteIdent:
    def test_postgres_quotes_parts(self):
        assert quote_ident("Hasta.Hasta", "postgresql") == '"Hasta"."Hasta"'

    def test_sqlite_quotes_parts(self):
        assert quote_ident("Hasta.Hasta", "sqlite") == '"Hasta"."Hasta"'

    def test_mysql_backticks(self):
        assert quote_ident("Hasta.Hasta", "mysql") == "`Hasta`.`Hasta`"

    def test_split_qualified(self):
        assert split_qualified("Hasta.Hasta") == ("Hasta", "Hasta")
        assert split_qualified("plain") == ("", "plain")


class TestSchemaAwareConnector:
    def test_table_names_qualified(self, connector):
        tables = connector.get_table_names()
        assert "Hasta.Hasta" in tables
        assert "Hasta.Protokol" in tables
        assert "Tedavi.ICD_ToMerkez" in tables

    def test_get_columns_qualified(self, connector):
        cols = connector.get_columns("Hasta.Hasta")
        names = {c["name"] for c in cols}
        assert names == {"Id", "Adi", "CinsiyetId"}
        pk = [c for c in cols if c["name"] == "Id"][0]
        assert pk["is_in_primary_key"] is True

    def test_foreign_keys_qualified(self, connector):
        fks = connector.get_foreign_keys()
        assert ("Hasta.Protokol", "HastaId", "Hasta.Hasta", "Id") in fks

    def test_wrap_is_idempotent(self, connector):
        assert wrap_multi_schema(connector) is connector


class TestSchemaProfileMultiSchema:
    def test_profile_reflects_qualified_tables(self, connector):
        profile = SchemaProfile.from_connector(connector, include_value_stats=False)
        assert set(profile.tables) == {"Hasta.Hasta", "Hasta.Protokol", "Tedavi.ICD_ToMerkez"}
        assert ("Hasta.Protokol", "HastaId", "Hasta.Hasta", "Id") in profile.foreign_keys

    def test_render_uses_quoted_names(self, connector):
        profile = SchemaProfile.from_connector(connector, include_value_stats=False)
        rendered = profile.render()
        assert '"Hasta"."Hasta"' in rendered
        assert '"Tedavi"."ICD_ToMerkez"' in rendered

    def test_filter_to_linked_fk_closure(self, connector):
        profile = SchemaProfile.from_connector(connector, include_value_stats=False)
        linked = profile.filter_to_linked(
            {"Hasta.Protokol", "Hasta.Hasta"},
            {"Hasta.Protokol": {"Id"}},
            force_pk_fk=True,
        )
        assert "HastaId" in linked.tables["Hasta.Protokol"].columns
        assert "Id" in linked.tables["Hasta.Hasta"].columns


class TestPipelineMultiSchema:
    class _ScriptedLLM:
        async def complete(self, messages, temperature=0.0, max_new_tokens=2048, **kw):
            content = "\n".join(m.get("content", "") for m in messages)
            if "Extract the most useful search terms" in content:
                return '<result>["Alice"]</result>'
            if "pinpoint the specific tables and columns" in content:
                return ('<result><table table_name="Hasta.Hasta">'
                        '<column column_name="Adi"/></table></result>')
            if "SQL Candidate A" in content:
                return "<result>A</result>"
            if "correcting a SQL query" in content or "external SQL checker" in content:
                return '<result>\n```sql\nSELECT "Adi" FROM "Hasta"."Hasta"\n```\n</result>'
            return ('<result>\n```sql\nSELECT "Adi" FROM "Hasta"."Hasta" '
                    "WHERE \"CinsiyetId\" = 'K'\n```\n</result>")

    def test_end_to_end(self, connector):
        from dbgpt.agent.expand.deepeye_sql.pipeline import DeepEyeSQLPipeline

        pipeline = DeepEyeSQLPipeline(connector, self._ScriptedLLM(), sampling_budget=1)
        result = asyncio.run(pipeline.run("Which female patients are there?"))
        assert result.success, result.error
        assert result.sql and "Hasta" in result.sql
        assert [r[0] for r in result.rows] == ["Alice"]
