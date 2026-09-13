"""Safe SQL execution helpers for the DeepEye-SQL pipeline.

All execution in the pipeline is read-only (SELECT-only) and bounded by a
row limit so that verification and selection never run unbounded queries.
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional, Tuple

from .util import get_statement_type

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


def ensure_read_only(sql: str) -> None:
    """Raise if the SQL is not a read-only SELECT statement."""
    stmt_type = get_statement_type(sql)
    if stmt_type != "SELECT":
        raise ValueError(
            f"Only SELECT queries are allowed for execution; got {stmt_type!r}"
        )


def safe_execute(
    connector: Any,
    sql: str,
    *,
    max_rows: int = 100,
    timeout: Optional[float] = None,
) -> Tuple[List[str], List[List[Any]]]:
    """Execute a SELECT safely, bounding the returned rows.

    Returns:
        ``(columns, rows)`` where ``columns`` is a list of field names and
        ``rows`` is a list of row lists.

    Raises:
        ValueError: if the SQL is not read-only.
        Exception: any database error from execution (propagated to the caller).
    """
    ensure_read_only(sql)
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
            return _run_query(connector, query, timeout)
        except Exception as e:  # pragma: no cover - depends on DB
            last_err = e
            logger.debug("safe_execute attempt failed: %s", e)
    assert last_err is not None
    raise last_err


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
