"""Deterministic SQL unit-testing tool-chain (Stage 3 of DeepEye-SQL).

Faithful reimplementation of the reference tool-chain, in the exact order:

``SyntaxChecker → JoinChecker → OrderByLimitChecker → TimeChecker →
SelectChecker → MaxMinChecker → OrderByNullChecker → ResultChecker``

Each checker either fixes the SQL deterministically or emits a targeted
suggestion that drives an LLM revision (``COMMON_CHECKER_PROMPT`` for
logic/style, ``EXECUTION_CHECKER_PROMPT`` for execution failures).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

from .execution import safe_execute
from .llm import LLMComplete
from .prompts import render_common_checker, render_execution_checker
from .schemas import CandidateSQL, CheckerReport
from .util import extract_llm_sql, hash_result, result_table_str

logger = logging.getLogger(__name__)

# Identifier used by the checker regexes (matches `x`, [x], "x", a.b).
_IDENT = r"(?:`[^`]+`|\[[^\]]+\]|\"[^\"]+\"|[\w\.]+)"


def classify_result(rows: Optional[List[List[Any]]]) -> str:
    """Classify an execution result into one of the DeepEye-SQL result types."""
    if rows is None:
        return "execution_error"
    if not rows:
        return "empty_result"
    if all(all(v is None for v in row) for row in rows):
        return "all_null_result"
    return "success"


def _execute(connector: Any, sql: str, max_rows: int):
    """Execute and return ``(result_type, columns, rows, error)``."""
    try:
        columns, rows = safe_execute(connector, sql, max_rows=max_rows)
        return classify_result(rows), columns, rows, ""
    except Exception as e:
        return "execution_error", [], None, str(e)


class BaseChecker:
    """Base deterministic checker."""

    name: str = "checker"

    def check(
        self, sql: str, connector: Any, max_rows: int
    ) -> CheckerReport:
        raise NotImplementedError


class SyntaxChecker(BaseChecker):
    """Execute; pass unless the query raises an execution error."""

    name = "syntax"

    def check(self, sql, connector, max_rows):
        result_type, _, _, error = _execute(connector, sql, max_rows)
        if result_type in ("success", "empty_result", "all_null_result"):
            return CheckerReport(self.name, True)
        return CheckerReport(
            self.name,
            False,
            message=result_table_str([], [[error or result_type]]),
            details={"execution": True, "result_type": result_type},
        )


class JoinChecker(BaseChecker):
    """Flag ``JOIN ... ON a.x = b.y OR a.x = b.z`` / ``ON a.x IN (...)``."""

    name = "join"

    _RE = re.compile(
        rf"JOIN\s+{_IDENT}(?:\s+AS\s+{_IDENT})?\s+ON\s+"
        rf"({_IDENT}\.{_IDENT}\s*=\s*{_IDENT}\.{_IDENT}"
        rf"(?:\s+OR\s+{_IDENT}\.{_IDENT}\s*=\s*{_IDENT}\.{_IDENT})+"
        rf"|{_IDENT}\.{_IDENT}\s+IN\s*\(.*?\))",
        re.IGNORECASE | re.DOTALL,
    )

    def check(self, sql, connector, max_rows):
        if self._RE.search(sql):
            return CheckerReport(
                self.name,
                False,
                message=(
                    "The SQL uses the JOIN function incorrectly, due to using "
                    "`JOIN table AS T ON Ta.column1 = Tb.column2 OR Ta.column1 "
                    "= Tb.column3` or `JOIN table AS T ON Ta.column1 IN`, please "
                    "only keep the highest priority group of `Ta.column = "
                    "Tb.column` in `OR`."
                ),
            )
        return CheckerReport(self.name, True)


class OrderByLimitChecker(BaseChecker):
    """Flag ``ORDER BY MIN/MAX(col) ... LIMIT n``."""

    name = "order_by_limit"

    _RE = re.compile(
        rf"ORDER BY ((MIN|MAX)\(\s*({_IDENT})\s*\)).*? LIMIT \d+",
        re.IGNORECASE | re.DOTALL,
    )

    def check(self, sql, connector, max_rows):
        match = self._RE.search(sql)
        if match:
            matched = match.group(1)
            col = match.group(3)
            return CheckerReport(
                self.name,
                False,
                message=(
                    "The SQL uses the ORDER BY function incorrectly, using "
                    "MIN/MAX in ORDER BY caluse is incrorrect "
                    f"(`{matched}`), please correct the SQL. If the SQL contains "
                    f"GROUP BY, please judge whether the content of `{col}` needs "
                    f"to use `SUM({col})`."
                ),
            )
        return CheckerReport(self.name, True)


class TimeChecker(BaseChecker):
    """Deterministically quote a bare 4+ digit year after strftime(...)."""

    name = "time"

    def check(self, sql, connector, max_rows):
        res = re.sub(
            r"(strftime *\([^\(]*?\) *[>=<]+ *)(\d{4,})", r"\1'\2'", sql
        )
        if res != sql:
            return CheckerReport(self.name, False, rewritten_sql=res)
        return CheckerReport(self.name, True)


class SelectChecker(BaseChecker):
    """Fix string concatenation in SELECT and flag ``table.*``."""

    name = "select"

    _STAR_RE = re.compile(
        rf"^SELECT.*? ({_IDENT}\.\*).*?FROM", re.IGNORECASE | re.DOTALL
    )

    def check(self, sql, connector, max_rows):
        pre_fixed = sql.replace("|| ' ' ||", ", ").replace("|| ', ' ||", ", ")
        changed = pre_fixed != sql
        if changed:
            sql = pre_fixed
        match = self._STAR_RE.search(sql)
        if match:
            star = match.group(1)
            return CheckerReport(
                self.name,
                False,
                message=(
                    f"1. We have specified that the ambiguous query is the "
                    f"corresponding id column, please replace {star} with the "
                    f"corresponding id column in the above SQL"
                ),
            )
        if changed:
            return CheckerReport(self.name, False, rewritten_sql=sql)
        return CheckerReport(self.name, True)


class MaxMinChecker(BaseChecker):
    """Flag redundant MIN/MAX nested-SELECT / LIMIT patterns."""

    name = "max_min"

    _NESTED_RE = re.compile(
        rf"=\s*\(\s*SELECT\s*(MAX|MIN)\s*\(\s*({_IDENT})\s*\)\s*FROM\s*({_IDENT})",
        re.IGNORECASE,
    )
    _LIMIT_NESTED_RE = re.compile(
        r"= (\(SELECT .* LIMIT \d+\))", re.IGNORECASE | re.DOTALL
    )
    _REDUNDANT_RE = re.compile(
        rf"^SELECT[^\(\)]*? ((MIN|MAX)\(\s*{_IDENT}\s*\)).*?LIMIT 1",
        re.IGNORECASE | re.DOTALL,
    )

    def check(self, sql, connector, max_rows):
        m = self._NESTED_RE.search(sql)
        if m:
            func, col, table = m.group(1), m.group(2), m.group(3)
            order = "DESC" if func.upper() == "MAX" else "ASC"
            return CheckerReport(
                self.name,
                False,
                message=(
                    f"WHERE {col} = (SELECT {func}({col}) FROM {table}): Please "
                    f"use ORDER BY {table}.{col} {order} LIMIT 1 instead of "
                    f"nested SQL"
                ),
            )
        m = self._LIMIT_NESTED_RE.search(sql)
        if m:
            return CheckerReport(
                self.name,
                False,
                message=f"{m.group(1)}: Please use JOIN instead of nested SQL",
            )
        m = self._REDUNDANT_RE.search(sql)
        if m:
            return CheckerReport(
                self.name,
                False,
                message=(
                    f"{m.group(1)}: {m.group(2)} function is redundant due to "
                    f"LIMIT clause, please use ORDER BY + LIMIT instead"
                ),
            )
        return CheckerReport(self.name, True)


class OrderByNullChecker(BaseChecker):
    """Flag ``ORDER BY ... LIMIT n`` that may rank NULLs first."""

    name = "order_by_null"

    _RE = re.compile(r"ORDER BY .*?(?<!DESC )LIMIT +\d+;{0,1}", re.IGNORECASE | re.DOTALL)

    def check(self, sql, connector, max_rows):
        for match in self._RE.finditer(sql):
            fragment = match.group(0)
            if "SUM(" in fragment.upper() or "COUNT(" in fragment.upper():
                continue
            return CheckerReport(
                self.name,
                False,
                message=(
                    f"Please add `IS NOT NULL` condition in the WHERE clause "
                    f"for the ORDER BY column: {fragment}"
                ),
            )
        return CheckerReport(self.name, True)


class ResultChecker(BaseChecker):
    """Execute; pass only on ``success`` (non-empty, non-all-null)."""

    name = "result"

    def check(self, sql, connector, max_rows):
        result_type, _, _, error = _execute(connector, sql, max_rows)
        if result_type == "success":
            return CheckerReport(self.name, True)
        return CheckerReport(
            self.name,
            False,
            message=result_table_str([], [[error or result_type]]),
            details={"execution": True, "result_type": result_type},
        )


class SQLToolChain:
    """Runs the checker chain (single pass) with targeted LLM repair."""

    CHECKERS = (
        SyntaxChecker,
        JoinChecker,
        OrderByLimitChecker,
        TimeChecker,
        SelectChecker,
        MaxMinChecker,
        OrderByNullChecker,
        ResultChecker,
    )

    def __init__(
        self,
        connector: Any,
        complete: LLMComplete,
        question: str,
        hint: str,
        database_schema: str,
        *,
        checker_sampling_budget: int = 1,
        max_rows: int = 100,
    ):
        self._connector = connector
        self._complete = complete
        self._question = question
        self._hint = hint
        self._database_schema = database_schema
        self._checker_sampling_budget = checker_sampling_budget
        self._max_rows = max_rows

    @property
    def dialect(self) -> str:
        return (getattr(self._connector, "dialect", "") or "").lower()

    async def _execution_revision(
        self, sql: str, error_str: str, valid_types: Set[str]
    ) -> Optional[str]:
        """Sample revisions via EXECUTION_CHECKER_PROMPT and pick the most
        frequent valid result-hash."""
        prompt = render_execution_checker(
            self._question,
            self._hint,
            self._database_schema,
            self.dialect,
            sql,
            error_str,
        )
        valid: List[Tuple[str, str]] = []
        for _ in range(self._checker_sampling_budget):
            try:
                raw = await self._complete.complete(
                    [{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_new_tokens=2048,
                )
            except Exception as e:  # pragma: no cover
                logger.warning("execution revision failed: %s", e)
                continue
            new_sql = extract_llm_sql(raw)
            if not new_sql:
                continue
            result_type, _, rows, _ = _execute(
                self._connector, new_sql, self._max_rows
            )
            if result_type in valid_types:
                valid.append((new_sql, hash_result(rows or [])))
        if not valid:
            return None
        counts = Counter(sig for _, sig in valid)
        most_freq = counts.most_common(1)[0][0]
        for new_sql, sig in valid:
            if sig == most_freq:
                return new_sql
        return None

    async def _common_revision(self, sql: str, suggestions: str) -> Optional[str]:
        prompt = render_common_checker(
            self._question,
            self._hint,
            self._database_schema,
            self.dialect,
            sql,
            suggestions,
        )
        try:
            raw = await self._complete.complete(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                max_new_tokens=2048,
            )
            return extract_llm_sql(raw) or None
        except Exception as e:  # pragma: no cover
            logger.warning("common revision failed: %s", e)
            return None

    async def _revise(self, sql: str) -> str:
        """Run the single-pass checker chain, mutating the SQL as needed."""
        for checker_cls in self.CHECKERS:
            checker = checker_cls()
            report = checker.check(sql, self._connector, self._max_rows)
            if report.passed:
                continue
            if report.rewritten_sql:
                sql = report.rewritten_sql
                continue
            if report.details.get("execution"):
                new_sql = await self._execution_revision(
                    sql, report.message, {"success", "empty_result", "all_null_result"}
                )
            else:
                new_sql = await self._common_revision(sql, report.message)
            if new_sql and new_sql != sql:
                sql = new_sql
        return sql

    async def verify_and_repair(
        self, candidates: List[CandidateSQL]
    ) -> List[CandidateSQL]:
        """Deduplicate candidates, run the chain once per unique candidate,
        then map revisions back and mark execution status."""
        # Dedup by normalized SQL while preserving order.
        seen: Dict[str, str] = {}
        order: List[str] = []
        for cand in candidates:
            norm = " ".join(cand.sql.split()).strip().lower()
            if norm not in seen:
                seen[norm] = cand.sql
                order.append(norm)

        revised_map: Dict[str, str] = {}
        for norm in order:
            revised_map[norm] = await self._revise(seen[norm])

        for cand in candidates:
            norm = " ".join(cand.sql.split()).strip().lower()
            cand.sql = revised_map.get(norm, cand.sql)
            result_type, columns, rows, error = _execute(
                self._connector, cand.sql, self._max_rows
            )
            cand.passed = result_type != "execution_error"
            cand.result_columns = columns
            cand.result_rows = rows
            cand.execution_error = error or None
        return candidates
