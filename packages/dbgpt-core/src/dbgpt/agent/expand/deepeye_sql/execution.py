"""Safe SQL execution helpers for the DeepEye-SQL pipeline.

All execution in the pipeline is read-only (SELECT-only) and bounded by a row
limit so that verification and selection never run unbounded queries. Results
can be cached so identical SQL is not executed twice across pipeline stages.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from typing import Any, List, Optional, Tuple

from .util import first_statement, get_statement_type

logger = logging.getLogger(__name__)

_LIMIT_RE = re.compile(r"\blimit\s+\d+", re.IGNORECASE)


def has_limit(sql: str) -> bool:
    """Return whether the SQL already contains a LIMIT clause."""
    return bool(_LIMIT_RE.search(sql))


def wrap_limit(sql: str, dialect: str, max_rows: int) -> str:
    """Wrap a SELECT in a bounded outer query."""
    if dialect and dialect.lower() in ("mssql", "sqlserver"):
        return f"SELECT TOP {max_rows} * FROM (\n{sql}\n) AS _deepeye_limit"
    return f"SELECT * FROM (\n{sql}\n) AS _deepeye_limit LIMIT {max_rows}"


def normalize_sql(sql: str) -> str:
    """Normalize SQL for caching (collapse whitespace, lower-case)."""
    return " ".join(sql.split()).strip().lower()


def ensure_read_only(sql: str, strict: bool = True) -> None:
    """Raise if the SQL is not a single read-only SELECT statement."""
    stmt_type = get_statement_type(sql)
    if stmt_type != "SELECT":
        raise ValueError(
            f"Only SELECT queries are allowed for execution; got {stmt_type!r}"
        )
    if strict:
        import sqlparse

        statements = [s for s in sqlparse.split(sql) if s and s.strip()]
        if len(statements) > 1:
            raise ValueError(
                "Multiple SQL statements are not allowed for read-only execution."
            )


class ExecutionCache:
    """A small LRU cache of execution results keyed by normalized SQL."""

    def __init__(self, maxsize: int = 512):
        self._data: "OrderedDict[str, Tuple[List[str], List[List[Any]]]]" = OrderedDict()
        self._maxsize = maxsize

    def get(self, key: str) -> Optional[Tuple[List[str], List[List[Any]]]]:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def set(self, key: str, value: Tuple[List[str], List[List[Any]]]) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def __contains__(self, key: str) -> bool:
        return key in self._data


def safe_execute(
    connector: Any,
    sql: str,
    *,
    max_rows: int = 100,
    timeout: Optional[float] = None,
    cache: Optional[ExecutionCache] = None,
    read_only: bool = True,
) -> Tuple[List[str], List[List[Any]]]:
    """Execute a SELECT safely, bounding the returned rows and caching results.

    Returns:
        ``(columns, rows)`` where ``columns`` is a list of field names and
        ``rows`` is a list of row lists.

    Raises:
        ValueError: if the SQL is not read-only.
        Exception: any database error from execution (propagated to the caller).
    """
    ensure_read_only(sql, strict=read_only)
    key = normalize_sql(sql)
    if cache is not None and key in cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    dialect = (getattr(connector, "dialect", "") or "").lower()

    candidates = [sql]
    if not has_limit(sql):
        try:
            candidates.insert(0, wrap_limit(sql, dialect, max_rows))
        except Exception:  # pragma: no cover
            pass

    last_err: Optional[Exception] = None
    for query in candidates:
        try:
            result = _run_query(connector, query, timeout)
            if cache is not None:
                cache.set(key, result)
            return result
        except Exception as e:  # pragma: no cover - depends on DB
            last_err = e
            logger.debug("safe_execute attempt failed: %s", e)
    assert last_err is not None
    raise last_err


def measure_execution_time(
    connector: Any, sql: str, *, max_rows: int = 100, repeat: int = 2
) -> float:
    """Measure the mean execution time of a query (for selection tie-breaks).

    Outlier samples beyond ±3σ are dropped, matching the reference.
    """
    import statistics
    import time

    samples: List[float] = []
    for _ in range(max(1, repeat)):
        start = time.perf_counter()
        try:
            safe_execute(connector, sql, max_rows=max_rows)
        except Exception:  # pragma: no cover - failed queries are filtered earlier
            return float("inf")
        samples.append(time.perf_counter() - start)
    if len(samples) <= 2:
        return statistics.mean(samples) if samples else float("inf")
    mean = statistics.mean(samples)
    std = statistics.stdev(samples)
    kept = [s for s in samples if abs(s - mean) <= 3 * std]
    return statistics.mean(kept) if kept else mean


def _run_query(
    connector: Any, query: str, timeout: Optional[float]
) -> Tuple[List[str], List[List[Any]]]:
    """Run a query via ``query_ex`` (preferred) or ``run``."""
    query_ex = getattr(connector, "query_ex", None)
    if callable(query_ex):
        columns, rows = query_ex(query, fetch="all", timeout=timeout)
        columns = [str(c) for c in columns] if columns else []
        rows = [list(r) for r in rows] if rows else []
        return columns, rows

    result = connector.run(query)
    if not result:
        return [], []
    columns = [str(c) for c in result[0]]
    rows = [list(r) for r in result[1:]]
    return columns, rows
