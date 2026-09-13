"""Unit tests for the DeepEye-SQL pipeline modules.

These tests cover the deterministic core of the pipeline (prompts, SQL/XML
parsing, checker regexes, schema-profile relational closure, value-retrieval
index and selection ranking) without requiring a live database or LLM.
"""

import asyncio

import pytest

from dbgpt.agent.expand.deepeye_sql import prompts, util
from dbgpt.agent.expand.deepeye_sql.checkers import (
    JoinChecker,
    MaxMinChecker,
    OrderByLimitChecker,
    OrderByNullChecker,
    SelectChecker,
    TimeChecker,
)
from dbgpt.agent.expand.deepeye_sql.schema_profile import (
    ColumnInfo,
    SchemaProfile,
    TableInfo,
)
from dbgpt.agent.expand.deepeye_sql.schemas import CandidateSQL
from dbgpt.agent.expand.deepeye_sql.selection import (
    ConfidenceAwareSelector,
    _parse_vote,
)
from dbgpt.agent.expand.deepeye_sql.value_retrieval import InMemoryValueIndex


# ---------------------------------------------------------------------------
# util
# ---------------------------------------------------------------------------
class TestUtil:
    def test_extract_xml_result(self):
        assert util.extract_xml_result("x <result> SELECT 1 </result> y") == "SELECT 1"

    def test_extract_llm_sql(self):
        text = "x <result>\n```sql\nSELECT 1\n```\n</result>"
        assert util.extract_llm_sql(text) == "SELECT 1"

    def test_extract_sql_fence(self):
        assert util.extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"

    def test_is_select(self):
        assert util.is_select_statement("SELECT 1")
        assert not util.is_select_statement("INSERT INTO t VALUES (1)")

    def test_hash_result_order_insensitive(self):
        h1 = util.hash_result([[1, "a"], [2, "b"]])
        h2 = util.hash_result([[2, "b"], [1, "a"]])
        assert h1 == h2

    def test_result_table_str(self):
        out = util.result_table_str(["a", "b"], [[1, 2], [3, 4]])
        assert "a | b" in out
        assert "1 | 2" in out


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
class TestPrompts:
    def test_all_prompts_render(self):
        prompts.render_keyword_extraction("q", "h")
        prompts.render_direct_link("q", "h", "schema")
        for gen in ("skeleton", "icl", "divide_conquer"):
            prompts.render_generation_prompt(
                gen, "q", "h", "schema", "sqlite", few_shot_examples=""
            )
        prompts.render_execution_checker("q", "h", "schema", "sqlite", "SELECT 1", "err")
        prompts.render_common_checker("q", "h", "schema", "sqlite", "SELECT 1", "fix")
        prompts.render_pair_selection("q", "h", "schema", "a", "ra", "b", "rb")

    def test_keyword_prompt_has_result_tag(self):
        out = prompts.render_keyword_extraction("q")
        assert "<result>" in out


# ---------------------------------------------------------------------------
# checkers
# ---------------------------------------------------------------------------
class _NoopConnector:
    dialect = "sqlite"

    def run(self, sql, fetch="all"):
        return []


@pytest.fixture
def noop_connector():
    return _NoopConnector()


class TestCheckers:
    def test_join_checker_or(self, noop_connector):
        report = JoinChecker().check(
            "SELECT * FROM a JOIN b ON a.id = b.id OR a.id = b.other",
            noop_connector,
            100,
        )
        assert not report.passed

    def test_join_checker_ok(self, noop_connector):
        assert JoinChecker().check(
            "SELECT * FROM a JOIN b ON a.id = b.id", noop_connector, 100
        ).passed

    def test_order_by_limit(self, noop_connector):
        assert not OrderByLimitChecker().check(
            "SELECT x FROM t ORDER BY MIN(x) LIMIT 1", noop_connector, 100
        ).passed

    def test_time_checker_quotes_year(self, noop_connector):
        report = TimeChecker().check(
            "SELECT strftime('%Y', d) >= 1988 FROM t", noop_connector, 100
        )
        assert report.rewritten_sql == "SELECT strftime('%Y', d) >= '1988' FROM t"

    def test_time_checker_noop(self, noop_connector):
        assert TimeChecker().check(
            "SELECT d FROM t WHERE d >= '1988'", noop_connector, 100
        ).passed

    def test_select_checker_concat(self, noop_connector):
        report = SelectChecker().check("SELECT a || ' ' || b FROM t", noop_connector, 100)
        assert report.rewritten_sql is not None
        assert "||" not in report.rewritten_sql

    def test_select_checker_star(self, noop_connector):
        assert not SelectChecker().check("SELECT t.* FROM t", noop_connector, 100).passed

    def test_max_min_nested(self, noop_connector):
        assert not MaxMinChecker().check(
            "SELECT x FROM t WHERE x = (SELECT MAX(x) FROM t)", noop_connector, 100
        ).passed

    def test_max_min_redundant(self, noop_connector):
        assert not MaxMinChecker().check(
            "SELECT MAX(x) FROM t LIMIT 1", noop_connector, 100
        ).passed

    def test_order_by_null(self, noop_connector):
        assert not OrderByNullChecker().check(
            "SELECT x FROM t ORDER BY x LIMIT 1", noop_connector, 100
        ).passed

    def test_order_by_null_skips_agg(self, noop_connector):
        assert OrderByNullChecker().check(
            "SELECT x FROM t ORDER BY SUM(x) LIMIT 1", noop_connector, 100
        ).passed


# ---------------------------------------------------------------------------
# schema profile closure
# ---------------------------------------------------------------------------
class TestSchemaProfile:
    def _profile(self):
        profile = SchemaProfile(db_id="db")
        users = TableInfo(name="users")
        users.columns["id"] = ColumnInfo(name="id", type="INTEGER", primary_key=True)
        users.columns["name"] = ColumnInfo(name="name", type="TEXT")
        users.columns["country"] = ColumnInfo(name="country", type="TEXT")
        orders = TableInfo(name="orders")
        orders.columns["id"] = ColumnInfo(name="id", type="INTEGER", primary_key=True)
        orders.columns["user_id"] = ColumnInfo(
            name="user_id", type="INTEGER", foreign_keys=[("users", "id")]
        )
        orders.columns["amount"] = ColumnInfo(name="amount", type="REAL")
        profile.tables["users"] = users
        profile.tables["orders"] = orders
        profile.foreign_keys = [("orders", "user_id", "users", "id")]
        return profile

    def test_relational_closure(self):
        linked = self._profile().filter_to_linked(
            {"orders", "users"}, {"orders": {"amount"}}, force_pk_fk=True
        )
        assert "amount" in linked.tables["orders"].columns
        assert "id" in linked.tables["orders"].columns  # PK force-included
        assert "user_id" in linked.tables["orders"].columns  # FK force-included
        assert "users" in linked.tables  # FK target table
        assert "id" in linked.tables["users"].columns  # FK target column

    def test_render_includes_fk(self):
        linked = self._profile().filter_to_linked(
            {"orders", "users"}, {"orders": {"amount"}}, force_pk_fk=True
        )
        assert "`orders`.`user_id` = `users`.`id`" in linked.render()


# ---------------------------------------------------------------------------
# value retrieval index
# ---------------------------------------------------------------------------
class TestValueIndex:
    def test_exact_match(self):
        idx = InMemoryValueIndex()
        idx.add("t.country", ["France", "Germany", "United States"])
        results = idx.search("t.country", "France", 2)
        assert results and results[0][0] == "France"
        assert results[0][1] == 0.0


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------
class TestSelection:
    def test_parse_vote(self):
        assert _parse_vote("<result>A</result>") == "A"
        assert _parse_vote("<result>TIE</result>") == "TIE"

    def test_rank(self):
        c1 = CandidateSQL(
            sql="SELECT 1",
            generator="g",
            index=0,
            result_signature="s1",
            result_rows=[[1]],
            result_columns=["c"],
            confidence=0.5,
        )
        c2 = CandidateSQL(
            sql="SELECT 2",
            generator="g",
            index=1,
            result_signature="s2",
            result_rows=[[2]],
            result_columns=["c"],
            confidence=0.5,
        )
        sel = ConfidenceAwareSelector(
            _NoopConnector(), None, "q", "", "schema", confidence_threshold=0.6
        )
        assert len(sel._rank([c1, c2])) == 2


# ---------------------------------------------------------------------------
# end-to-end pipeline (fakes)
# ---------------------------------------------------------------------------
class _FakeConnector:
    dialect = "sqlite"

    def get_table_names(self):
        return ["users", "orders"]

    def get_columns(self, table):
        if table == "users":
            return [
                {"name": "id", "type": "INTEGER", "is_in_primary_key": True, "comment": ""},
                {"name": "name", "type": "TEXT", "is_in_primary_key": False, "comment": ""},
                {"name": "country", "type": "TEXT", "is_in_primary_key": False, "comment": ""},
            ]
        return [
            {"name": "id", "type": "INTEGER", "is_in_primary_key": True, "comment": ""},
            {"name": "user_id", "type": "INTEGER", "is_in_primary_key": False, "comment": ""},
            {"name": "amount", "type": "REAL", "is_in_primary_key": False, "comment": ""},
        ]

    def get_table_comment(self, table):
        return {"text": ""}

    def get_current_db_name(self):
        return "testdb"

    def run(self, sql, fetch="all"):
        up = sql.upper()
        if "DISTINCT" in up:
            return [("v",), ("France",), ("Germany",)]
        if "COUNT(DISTINCT" in up:
            return [("total", "distinct", "null"), (100, 50, 10)]
        return [("n",), (3,)]


class _FakeLLM:
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
        return "<result>\n```sql\nSELECT name FROM users WHERE country = 'France'\n```\n</result>"


class TestPipeline:
    def test_end_to_end(self):
        from dbgpt.agent.expand.deepeye_sql.pipeline import DeepEyeSQLPipeline

        pipeline = DeepEyeSQLPipeline(_FakeConnector(), _FakeLLM(), sampling_budget=1)
        result = asyncio.run(
            pipeline.run("How many users are from France?")
        )
        assert result.success, result.error
        assert result.sql and "SELECT" in result.sql.upper()
        assert result.rows
        assert result.selected_by == "high_confidence"
