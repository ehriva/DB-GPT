"""Integration tests for the DeepEye-SQL pipeline against a real SQLite DB."""

import asyncio
import sqlite3

import pytest


class _ScriptedLLM:
    """Deterministic LLM whose responses match the test schema."""

    async def complete(self, messages, temperature=0.0, max_new_tokens=2048, **kw):
        content = "\n".join(m.get("content", "") for m in messages)
        if "Extract the most useful search terms" in content:
            return '<result>["France"]</result>'
        if "pinpoint the specific tables and columns" in content:
            return (
                '<result><table table_name="users">'
                '<column column_name="name"/><column column_name="country"/>'
                "</table></result>"
            )
        if "SQL Candidate A" in content:
            return "<result>A</result>"
        if "correcting a SQL query" in content or "external SQL checker" in content:
            return "<result>\n```sql\nSELECT name FROM users\n```\n</result>"
        return (
            "<result>\n```sql\nSELECT name FROM users WHERE country = 'France'\n"
            "```\n</result>"
        )


@pytest.fixture
def sqlite_db(tmp_path):
    path = tmp_path / "deepeye.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            name TEXT,
            country TEXT
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            amount REAL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        INSERT INTO users VALUES (1, 'Alice', 'France');
        INSERT INTO users VALUES (2, 'Bob', 'Germany');
        INSERT INTO users VALUES (3, 'Carol', 'France');
        INSERT INTO orders VALUES (1, 1, 10.0);
        INSERT INTO orders VALUES (2, 3, 25.5);
        """
    )
    conn.commit()
    conn.close()
    return str(path)


def _make_connector(path):
    from dbgpt_ext.datasource.rdbms.conn_sqlite import SQLiteConnector

    return SQLiteConnector.from_file_path(path)


def test_end_to_end_real_sqlite(sqlite_db):
    from dbgpt.agent.expand.deepeye_sql.pipeline import DeepEyeSQLPipeline

    connector = _make_connector(sqlite_db)
    pipeline = DeepEyeSQLPipeline(connector, _ScriptedLLM(), sampling_budget=1)
    result = asyncio.run(pipeline.run("Which users are from France?"))

    assert result.success, result.error
    assert result.sql and "France" in result.sql
    assert set(result.columns) == {"name"}
    names = {row[0] for row in result.rows}
    assert names == {"Alice", "Carol"}
    assert result.selected_by == "high_confidence"


def test_schema_profile_reflects_fk(sqlite_db):
    from dbgpt.agent.expand.deepeye_sql.schema_profile import SchemaProfile

    connector = _make_connector(sqlite_db)
    profile = SchemaProfile.from_connector(connector, include_value_stats=False)

    assert set(profile.tables) == {"users", "orders"}
    # FK reflection from SQLAlchemy metadata.
    assert ("orders", "user_id", "users", "id") in profile.foreign_keys


def test_value_index_build(sqlite_db):
    from dbgpt.agent.expand.deepeye_sql.value_retrieval import build_value_index

    connector = _make_connector(sqlite_db)
    index = build_value_index(connector, embedder=None, max_indexed_values=100)
    assert "users.country" in index.columns()
    results = index.search("users.country", "France", 2)
    assert results and results[0][0] == "France"
