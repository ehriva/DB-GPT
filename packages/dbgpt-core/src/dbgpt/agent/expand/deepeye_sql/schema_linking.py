"""Robust schema linking with relational closure (Stage 1b of DeepEye-SQL).

Three fault-tolerant linking strategies run in parallel — direct linking,
reversed linking (generate a draft SQL then statically extract its schema),
and value-based linking — their results are unioned, and relational closure
force-includes primary keys and foreign keys so the schema graph stays
joinable.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .llm import LLMComplete
from .prompts import render_direct_link, render_generation_prompt
from .schema_profile import SchemaProfile
from .schemas import LinkedSchema, RetrievedValues
from .util import extract_llm_sql, extract_xml_result

logger = logging.getLogger(__name__)

_TABLE_TAG_RE = re.compile(
    r'<table\b[^>]*?table_name\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</table>',
    re.IGNORECASE | re.DOTALL,
)
_COLUMN_TAG_RE = re.compile(
    r'<column\b[^>]*?column_name\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _parse_direct_link_result(xml_text: str) -> Dict[str, Set[str]]:
    """Parse the DIRECT_LINKING_PROMPT XML output into tables -> columns."""
    result: Dict[str, Set[str]] = {}
    for table_match in _TABLE_TAG_RE.finditer(xml_text):
        table_name = table_match.group(1).strip()
        block = table_match.group(2)
        cols = {c.strip() for c in _COLUMN_TAG_RE.findall(block)}
        result.setdefault(table_name, set()).update(cols)
    return result


class RobustSchemaLinker:
    """Fault-tolerant schema linker (direct + reversed + value + closure)."""

    def __init__(
        self,
        connector: Any,
        complete: LLMComplete,
        *,
        value_distance_threshold: float = 0.05,
        sampling_budget: int = 1,
    ):
        self._connector = connector
        self._complete = complete
        self._value_distance_threshold = value_distance_threshold
        self._sampling_budget = sampling_budget

    @property
    def dialect(self) -> str:
        return (getattr(self._connector, "dialect", "") or "").lower()

    def _name_maps(self, profile: SchemaProfile) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
        table_map: Dict[str, str] = {}
        column_map: Dict[str, Dict[str, str]] = {}
        for table in profile.tables:
            table_map[table.lower()] = table
            # Also map a dotted base name (e.g. schema.table).
            base = table.split(".")[-1].lower()
            table_map.setdefault(base, table)
            column_map[table] = {}
            for col in profile.tables[table].columns:
                column_map[table][col.lower()] = col
        return table_map, column_map

    async def _direct_link(
        self,
        question: str,
        hint: str,
        database_schema: str,
        profile: SchemaProfile,
    ) -> Dict[str, Set[str]]:
        table_map, column_map = self._name_maps(profile)
        tables: Set[str] = set()
        columns: Dict[str, Set[str]] = {}

        async def _sample() -> Tuple[Set[str], Dict[str, Set[str]]]:
            prompt = render_direct_link(question, hint, database_schema)
            raw = await self._complete.complete(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                max_new_tokens=2048,
            )
            parsed = _parse_direct_link_result(extract_xml_result(raw))
            t: Set[str] = set()
            c: Dict[str, Set[str]] = {}
            for raw_table, raw_cols in parsed.items():
                table = table_map.get(raw_table.lower(), raw_table)
                t.add(table)
                mapped = {column_map.get(table, {}).get(col.lower(), col) for col in raw_cols}
                c.setdefault(table, set()).update(mapped)
            return t, c

        results = await asyncio.gather(
            *[_sample() for _ in range(self._sampling_budget)]
        )
        for t, c in results:
            tables |= t
            for table, cols in c.items():
                columns.setdefault(table, set()).update(cols)
        return {"tables": tables, "columns": columns}

    async def _reversed_link(
        self,
        question: str,
        hint: str,
        database_schema: str,
        profile: SchemaProfile,
        few_shot_examples: Optional[List[tuple]] = None,
    ) -> Dict[str, Set[str]]:
        tables: Set[str] = set()
        columns: Dict[str, Set[str]] = {}

        async def _draft() -> Optional[str]:
            generator = "icl" if few_shot_examples else "divide_conquer"
            from .prompts import render_few_shot_examples

            prompt = render_generation_prompt(
                generator,
                question,
                hint,
                database_schema,
                self.dialect,
                few_shot_examples=(
                    render_few_shot_examples(few_shot_examples)
                    if few_shot_examples
                    else ""
                ),
            )
            try:
                raw = await self._complete.complete(
                    [{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_new_tokens=2048,
                )
                return extract_llm_sql(raw) or None
            except Exception as e:  # pragma: no cover
                logger.warning("reversed-link draft failed: %s", e)
                return None

        drafts = [
            d for d in await asyncio.gather(
                *[_draft() for _ in range(self._sampling_budget)]
            )
            if d
        ]
        for sql in drafts:
            t, c = self._extract_tables_and_columns(sql, profile)
            tables |= t
            for table, cols in c.items():
                columns.setdefault(table, set()).update(cols)
        return {"tables": tables, "columns": columns}

    def _extract_tables_and_columns(
        self, sql: str, profile: SchemaProfile
    ) -> Tuple[Set[str], Dict[str, Set[str]]]:
        """Statically extract used tables/columns via substring containment."""
        sql_lower = sql.lower()
        tables: Set[str] = set()
        columns: Dict[str, Set[str]] = {}
        for table in profile.tables:
            variants = {table.lower(), table.split(".")[-1].lower()}
            used = any(v in sql_lower for v in variants)
            if not used:
                continue
            tables.add(table)
            for col in profile.tables[table].columns:
                if col.lower() in sql_lower:
                    columns.setdefault(table, set()).add(col)
        return tables, columns

    def _value_link(self, retrieved: RetrievedValues) -> Dict[str, Set[str]]:
        tables: Set[str] = set()
        columns: Dict[str, Set[str]] = {}
        for key, values in retrieved.values.items():
            if "." in key:
                table, column = key.split(".", 1)
            else:
                table, column = "", key
            # Link the column if any retrieved value is below the distance
            # threshold (distance = 1 - cosine similarity).
            if any(v.distance < self._value_distance_threshold for v in values):
                tables.add(table)
                if column:
                    columns.setdefault(table, set()).add(column)
        return {"tables": tables, "columns": columns}

    async def link(
        self,
        question: str,
        hint: str,
        profile: SchemaProfile,
        retrieved: RetrievedValues,
        *,
        few_shot_examples: Optional[List[tuple]] = None,
    ) -> LinkedSchema:
        """Run the robust schema-linking pipeline and enforce closure."""
        database_schema = profile.render()

        async def _value_link_async() -> Dict[str, Set[str]]:
            return self._value_link(retrieved)

        direct, reversed_, value = await asyncio.gather(
            self._direct_link(question, hint, database_schema, profile),
            self._reversed_link(
                question, hint, database_schema, profile, few_shot_examples
            ),
            _value_link_async(),
        )

        union_tables: Set[str] = (
            direct["tables"] | reversed_["tables"] | value["tables"]
        )
        union_columns: Dict[str, Set[str]] = {}
        for contrib in (direct, reversed_, value):
            for table, cols in contrib["columns"].items():
                union_columns.setdefault(table, set()).update(cols)

        # Keep only real tables.
        valid_tables = set(profile.tables)
        union_tables &= valid_tables
        union_columns = {t: c for t, c in union_columns.items() if t in valid_tables}

        # Relational closure: force-include PKs and FKs for linked tables.
        linked_profile = profile.filter_to_linked(
            union_tables, union_columns, force_pk_fk=True
        )
        closure_tables = set(linked_profile.tables)
        closure_columns: Dict[str, List[str]] = {
            t: sorted(linked_profile.tables[t].columns) for t in closure_tables
        }

        return LinkedSchema(
            tables=closure_tables,
            columns=closure_columns,
            schema_text=linked_profile.render(),
            fk_edges=[
                (s_t, t_t) for (s_t, _s_c, t_t, _t_c) in linked_profile.foreign_keys
            ],
            source={
                "direct": direct["tables"],
                "reversed": reversed_["tables"],
                "value": value["tables"],
            },
        )
