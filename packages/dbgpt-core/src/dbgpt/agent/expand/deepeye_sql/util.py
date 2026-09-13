"""Shared utilities for the DeepEye-SQL pipeline."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Set

import sqlparse

logger = logging.getLogger(__name__)

# Optional, richer parser. Fall back to sqlparse (a guaranteed dependency) when
# sqlglot is unavailable.
try:
    import sqlglot
    from sqlglot import exp

    _SQLGLOT_AVAILABLE = True
except Exception:  # pragma: no cover - depends on environment
    sqlglot = None  # type: ignore
    exp = None  # type: ignore
    _SQLGLOT_AVAILABLE = False


# SQLAlchemy dialect name -> sqlglot dialect name.
_DIALECT_MAP = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "mssql": "tsql",
    "sqlserver": "tsql",
    "mysql": "mysql",
    "sqlite": "sqlite",
    "duckdb": "duckdb",
    "clickhouse": "clickhouse",
    "oracle": "oracle",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
}


def sqlglot_dialect(dialect: Optional[str]) -> Optional[str]:
    """Map a DB-GPT/SQLAlchemy dialect to a sqlglot dialect name."""
    if not dialect:
        return None
    return _DIALECT_MAP.get(dialect.lower(), dialect.lower())


def sqlglot_available() -> bool:
    """Return whether sqlglot is importable."""
    return _SQLGLOT_AVAILABLE


_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_sql(text: str) -> str:
    """Extract the first SQL statement from an LLM response.

    Handles ```sql ... ``` fences and also strips a leading
    "```sql" marker without a closing fence.
    """
    if not text:
        return ""
    text = text.strip()
    match = _SQL_FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    # Handle an unclosed ```sql fence.
    if text.startswith("```"):
        text = re.sub(r"^```\w*\s*", "", text).strip()
        # Drop a trailing ``` if present.
        text = re.sub(r"```\s*$", "", text).strip()
    return text.strip()


def first_statement(sql: str) -> str:
    """Return only the first SQL statement from a possibly multi-statement text."""
    statements = sqlparse.split(sql)
    if not statements:
        return sql.strip()
    return statements[0].strip()


def get_statement_type(sql: str) -> str:
    """Return the SQL statement type (SELECT/INSERT/...) using sqlparse."""
    try:
        parsed = sqlparse.parse(sql)
        if parsed:
            return parsed[0].get_type() or "UNKNOWN"
    except Exception:  # pragma: no cover - defensive
        logger.debug("sqlparse failed to determine statement type", exc_info=True)
    return "UNKNOWN"


def is_select_statement(sql: str) -> bool:
    """Return True if the first statement is a SELECT (read-only)."""
    return get_statement_type(sql) == "SELECT"


def extract_tables(sql: str, dialect: Optional[str] = None) -> Set[str]:
    """Extract referenced table names from a SQL statement."""
    tables: Set[str] = set()
    if sqlglot_available() and sqlglot is not None:
        try:
            expr = sqlglot.parse_one(sql, read=sqlglot_dialect(dialect))
            for t in expr.find_all(exp.Table):
                if t.name:
                    tables.add(t.name)
            return tables
        except Exception:
            logger.debug("sqlglot table extraction failed; using fallback")
    # Fallback: sqlparse identifier extraction is unreliable for tables, so do
    # a lightweight regex over FROM/JOIN clauses.
    clean = re.sub(r"--.*$", "", sql, flags=re.MULTILINE)
    for match in re.finditer(
        r"\b(?:from|join)\s+([`\"\[]?[A-Za-z0-9_$]+[`\"\]]?)",
        clean,
        flags=re.IGNORECASE,
    ):
        name = match.group(1).strip("`\"[]")
        tables.add(name)
    return tables


def extract_columns(sql: str, dialect: Optional[str] = None) -> Set[str]:
    """Extract referenced column names from a SQL statement."""
    columns: Set[str] = set()
    if sqlglot_available() and sqlglot is not None:
        try:
            expr = sqlglot.parse_one(sql, read=sqlglot_dialect(dialect))
            for c in expr.find_all(exp.Column):
                if c.name:
                    columns.add(c.name)
            return columns
        except Exception:
            logger.debug("sqlglot column extraction failed; using fallback")
    # Fallback: capture identifiers followed by operators/commas/keywords.
    for match in re.finditer(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?=,|=|<=|>=|<|>|\)|\s+(?:from|where|and|or|"
        r"group|order|having|limit|join)\b)",
        sql,
        flags=re.IGNORECASE,
    ):
        columns.add(match.group(1))
    return columns


def extract_json(text: str) -> Any:
    """Extract the first JSON object/array from an LLM response.

    Tries a direct ``json.loads`` first, then falls back to balanced-brace
    scanning so that surrounding prose does not break parsing.
    """
    if not text:
        raise ValueError("Empty response; expected JSON")
    text = text.strip()
    # Strip a code fence if present.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fallback: locate the first { or [ and scan to the matching close.
    for start_ch, end_ch in (("{", "}"), ("[", "]")):
        start = text.find(start_ch)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == start_ch:
                depth += 1
            elif ch == end_ch:
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        break
    raise ValueError(f"Unable to extract JSON from response: {text[:200]!r}")


def extract_json_array(text: str) -> List[str]:
    """Extract a JSON array of strings (used for keyword extraction)."""
    data = extract_json(text)
    if isinstance(data, list):
        return [str(x) for x in data]
    if isinstance(data, dict):
        # Some models wrap the list in an object; accept a "keywords" key.
        for key in ("keywords", "values", "terms", "results"):
            if key in data and isinstance(data[key], list):
                return [str(x) for x in data[key]]
    raise ValueError(f"Expected a JSON array of strings, got: {data!r}")


def render_schema_ddl(connector, tables: Set[str]) -> str:
    """Render DDL-style schema text for the given tables using a connector.

    Prefers ``get_table_info`` for the subset of tables; falls back to a
    hand-built ``CREATE TABLE``-like description when the connector does not
    support subset selection.
    """
    if not tables:
        return ""
    try:
        info = connector.get_table_info(sorted(tables))
        if info and info.strip():
            return info
    except Exception as e:  # pragma: no cover - depends on connector
        logger.debug("get_table_info failed: %s", e)

    blocks: List[str] = []
    for table in sorted(tables):
        cols = connector.get_columns(table)
        col_lines = []
        for c in cols:
            name = c.get("name", "")
            col_type = c.get("type", "")
            comment = c.get("comment") or ""
            pk = " PRIMARY KEY" if c.get("is_in_primary_key") else ""
            col_lines.append(f"  {name} {col_type}{pk}" + (f"  -- {comment}" if comment else ""))
        blocks.append(f"CREATE TABLE {table} (\n" + ",\n".join(col_lines) + "\n);")
    return "\n\n".join(blocks)


def qualify_column(table: str, column: str) -> str:
    """Return a qualified column key ``table.column``."""
    return f"{table}.{column}"


_XML_RESULT_RE = re.compile(
    r"<result\b[^>]*>(.*?)</result>", re.IGNORECASE | re.DOTALL
)


def extract_xml_result(text: str) -> str:
    """Extract the content inside the first ``<result>...</result>`` block."""
    if not text:
        return ""
    match = _XML_RESULT_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def extract_llm_sql(text: str) -> str:
    """Extract SQL from an LLM response: XML ``<result>`` then ```sql fence."""
    return extract_sql(extract_xml_result(text))


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, float):
        return round(value, 4)
    try:
        hash(value)
        return value
    except TypeError:
        return str(value)


def hash_result(rows: List[Any]) -> str:
    """Return an order-insensitive, deterministic signature of a result set.

    Matches the DeepEye-SQL ``hash_result`` behaviour (``frozenset`` of
    hashable rows; row order does not matter, column order is fixed). Sorting
    the canonical set before hashing guarantees a stable string regardless of
    insertion order.
    """
    import hashlib

    canonical = sorted(tuple(_hashable(r) for r in row) for row in rows)
    return hashlib.sha1(repr(canonical).encode("utf-8")).hexdigest()


def result_table_str(
    columns: Optional[List[str]],
    rows: Optional[List[List[Any]]],
    *,
    max_rows: int = 5,
    max_cell_chars: int = 100,
) -> str:
    """Render an execution result as a compact table string for prompts."""
    if columns is None and rows is None:
        return "(no result)"
    columns = columns or []
    rows = rows or []
    if not columns and not rows:
        return "(empty result)"

    def _cell(value: Any) -> str:
        s = "" if value is None else str(value)
        if len(s) > max_cell_chars:
            s = s[: max_cell_chars - 3] + "..."
        return s

    header = " | ".join(_cell(c) for c in columns)
    lines = [header, "-" * len(header)]
    for row in rows[:max_rows]:
        lines.append(" | ".join(_cell(v) for v in row))
    if len(rows) > max_rows:
        lines.append(f"... ({len(rows)} rows total, showing {max_rows})")
    return "\n".join(lines)
