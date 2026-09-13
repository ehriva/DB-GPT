"""Database schema profile construction and rendering.

The DeepEye-SQL prompts reference a specific schema profile format that
includes per-column types, primary-key markers, descriptions, value
statistics and value examples, plus an explicit foreign-key list. This module
builds that profile from a DB-GPT connector and supports the two mutations the
pipeline performs on it:

* :meth:`SchemaProfile.apply_value_retrieval` — prepend retrieved values to a
  column's value examples.
* :meth:`SchemaProfile.filter_to_linked` — keep only the linked tables/columns
  and force-include primary keys and foreign keys (relational closure).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .schemas import RetrievedValues

logger = logging.getLogger(__name__)

_MAX_VALUE_LENGTH = 100
_MAX_VALUE_EXAMPLES = 3
_STATS_LIMIT = 100000

_TEXT_TYPE_TOKENS = ("char", "text", "varchar", "string", "str", "clob", "enum")


def is_text_column(col_type: str) -> bool:
    """Return True for TEXT-like column types (indexed by value retrieval)."""
    t = (col_type or "").lower()
    return any(tok in t for tok in _TEXT_TYPE_TOKENS)


@dataclass
class ColumnInfo:
    """Schema information for a single column."""

    name: str
    type: str = ""
    primary_key: bool = False
    description: str = ""
    value_examples: List[str] = field(default_factory=list)
    value_statistics: Dict[str, int] = field(default_factory=dict)
    foreign_keys: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class TableInfo:
    """Schema information for a single table."""

    name: str
    description: str = ""
    columns: Dict[str, ColumnInfo] = field(default_factory=dict)

    def ordered_columns(self) -> List[ColumnInfo]:
        return [self.columns[name] for name in sorted(self.columns)]


@dataclass
class SchemaProfile:
    """A mutable database schema profile used across pipeline stages."""

    db_id: str = ""
    tables: Dict[str, TableInfo] = field(default_factory=dict)
    foreign_keys: List[Tuple[str, str, str, str]] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_connector(
        cls,
        connector: Any,
        *,
        include_value_stats: bool = True,
        include_value_examples: bool = True,
        db_id: Optional[str] = None,
    ) -> "SchemaProfile":
        """Build a schema profile from a DB-GPT connector."""
        profile = cls(db_id=db_id or _db_id(connector))
        profile._load_foreign_keys(connector)
        table_names = list(connector.get_table_names())
        for table in table_names:
            tinfo = TableInfo(name=table)
            try:
                tinfo.description = _table_description(connector, table)
            except Exception:  # pragma: no cover
                pass
            try:
                columns = connector.get_columns(table)
            except Exception as e:  # pragma: no cover
                logger.debug("get_columns(%s) failed: %s", table, e)
                columns = []
            for col in columns:
                name = col.get("name", "")
                if not name:
                    continue
                cinfo = ColumnInfo(
                    name=name,
                    type=str(col.get("type", "") or ""),
                    primary_key=bool(col.get("is_in_primary_key")),
                    description=col.get("comment") or "",
                    foreign_keys=profile._fk_targets_for(table, name),
                )
                if include_value_examples and is_text_column(cinfo.type):
                    cinfo.value_examples = _value_examples(connector, table, name)
                if include_value_stats and is_text_column(cinfo.type):
                    cinfo.value_statistics = _value_statistics(connector, table, name)
                tinfo.columns[name] = cinfo
            profile.tables[table] = tinfo
        return profile

    def _load_foreign_keys(self, connector: Any) -> None:
        metadata = getattr(connector, "_metadata", None)
        if metadata is None:
            return
        try:
            for table in metadata.sorted_tables:
                for fk in table.foreign_key_constraints:
                    for elem in fk.elements:
                        src_col = elem.parent.name
                        tgt_table = elem.column.table.name
                        tgt_col = elem.column.name
                        self.foreign_keys.append(
                            (table.name, src_col, tgt_table, tgt_col)
                        )
        except Exception as e:  # pragma: no cover
            logger.debug("FK reflection failed: %s", e)

    def _fk_targets_for(self, table: str, column: str) -> List[Tuple[str, str]]:
        return [
            (t, c)
            for (s_t, s_c, t, c) in self.foreign_keys
            if s_t == table and s_c == column
        ]

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def apply_value_retrieval(
        self, retrieved: RetrievedValues, max_per_column: int = 5
    ) -> None:
        """Prepend retrieved values to each column's value examples."""
        for key, values in retrieved.values.items():
            if "." in key:
                table, column = key.split(".", 1)
            else:
                table, column = "", key
            tinfo = self.tables.get(table)
            if tinfo is None or column not in tinfo.columns:
                continue
            cinfo = tinfo.columns[column]
            retrieved_vals = [v.value for v in values]
            # Prepend retrieved values, drop duplicates, keep original examples.
            merged: List[str] = []
            for v in retrieved_vals:
                if v not in merged:
                    merged.append(v)
            for v in cinfo.value_examples:
                if v not in merged:
                    merged.append(v)
            cinfo.value_examples = merged[:max_per_column]

    def filter_to_linked(
        self,
        linked_tables: Set[str],
        linked_columns: Optional[Dict[str, Set[str]]] = None,
        *,
        force_pk_fk: bool = True,
    ) -> "SchemaProfile":
        """Return a new profile restricted to the linked schema.

        With ``force_pk_fk``, primary keys are always included for linked
        tables, and any foreign-key column whose target (table+column) is also
        linked is force-included together with its target column — this is the
        relational closure step that keeps the schema graph joinable.
        """
        linked_columns = linked_columns or {}
        valid_tables = set(self.tables)
        linked_tables = linked_tables & valid_tables

        # Pass 1: initial keep sets (selected columns, or all columns).
        keep: Dict[str, Set[str]] = {}
        for table in linked_tables:
            tinfo = self.tables[table]
            selected = linked_columns.get(table, set())
            if not selected:
                keep[table] = set(tinfo.columns)
            else:
                keep[table] = selected & set(tinfo.columns)

        # Pass 2: relational closure — force-include PKs and FK columns.
        if force_pk_fk:
            for table in list(linked_tables):
                tinfo = self.tables[table]
                for col_name, cinfo in tinfo.columns.items():
                    if cinfo.primary_key:
                        keep[table].add(col_name)
                    for tgt_table, tgt_col in cinfo.foreign_keys:
                        if (
                            tgt_table in linked_tables
                            and tgt_table in self.tables
                            and tgt_col in self.tables[tgt_table].columns
                        ):
                            keep[table].add(col_name)
                            keep[tgt_table].add(tgt_col)

        new_tables: Dict[str, TableInfo] = {}
        for table in sorted(keep):
            tinfo = self.tables[table]
            new_columns = {
                name: tinfo.columns[name]
                for name in sorted(keep[table])
                if name in tinfo.columns
            }
            if new_columns:
                new_tables[table] = TableInfo(
                    name=table,
                    description=tinfo.description,
                    columns=new_columns,
                )

        fks = [
            (s_t, s_c, t_t, t_c)
            for (s_t, s_c, t_t, t_c) in self.foreign_keys
            if s_t in new_tables and t_t in new_tables
        ]
        return SchemaProfile(
            db_id=self.db_id, tables=new_tables, foreign_keys=fks
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def render(
        self,
        *,
        include_stats: bool = True,
        include_examples: bool = True,
    ) -> str:
        """Render the profile in the DeepEye-SQL prompt format."""
        lines: List[str] = []
        lines.append(f"Database ID: `{self.db_id}`")
        lines.append("Schema:")
        for table_name in sorted(self.tables):
            tinfo = self.tables[table_name]
            col_parts = []
            for cinfo in tinfo.ordered_columns():
                col_parts.append(self._render_column(cinfo, include_stats, include_examples))
            cols_str = ",\n".join(col_parts)
            lines.append(f"- Table: `{table_name}` [\n{cols_str}\n]")
        if self.foreign_keys:
            lines.append("Foreign Keys:")
            for s_t, s_c, t_t, t_c in self.foreign_keys:
                lines.append(f"`{s_t}`.`{s_c}` = `{t_t}`.`{t_c}`")
        return "\n".join(lines)

    def _render_column(
        self, cinfo: ColumnInfo, include_stats: bool, include_examples: bool
    ) -> str:
        segs = [f"`{cinfo.name}`: {cinfo.type}"]
        if cinfo.primary_key:
            segs.append("Primary Key")
        if cinfo.description:
            segs.append(cinfo.description)
        if include_stats and cinfo.value_statistics:
            s = cinfo.value_statistics
            segs.append(
                f"Value Statistics: total={s.get('total', 0)}, "
                f"distinct={s.get('distinct', 0)}, null={s.get('null', 0)}"
            )
        if include_examples and cinfo.value_examples:
            examples = ", ".join(repr(v) for v in cinfo.value_examples)
            segs.append(f"Value Examples: [{examples}]")
        return "  ( " + " | ".join(segs) + " )"


# ---------------------------------------------------------------------------
# Connector helpers
# ---------------------------------------------------------------------------
def _db_id(connector: Any) -> str:
    try:
        return str(connector.get_current_db_name())
    except Exception:
        pass
    return str(getattr(connector, "_engine", None) and getattr(connector, "db_url", ""))


def _table_description(connector: Any, table: str) -> str:
    try:
        comment = connector.get_table_comment(table)
        if isinstance(comment, dict):
            return comment.get("text") or ""
        return str(comment or "")
    except Exception:
        return ""


def _distinct_values(connector: Any, table: str, column: str, limit: int) -> List[str]:
    """Fetch distinct non-empty values (bounded)."""
    dialect = (getattr(connector, "dialect", "") or "").lower()
    if dialect in ("mssql", "sqlserver"):
        sql = (
            f"SELECT DISTINCT TOP ({limit}) {column} FROM {table} "
            f"WHERE {column} IS NOT NULL AND {column} <> ''"
        )
    else:
        sql = (
            f"SELECT DISTINCT {column} FROM {table} "
            f"WHERE {column} IS NOT NULL AND {column} <> '' LIMIT {limit}"
        )
    try:
        result = connector.run(sql)
        if not result:
            return []
        return [str(r[0]) for r in result[1:] if r and r[0] is not None]
    except Exception as e:  # pragma: no cover
        logger.debug("distinct values failed for %s.%s: %s", table, column, e)
        return []


def _value_examples(connector: Any, table: str, column: str) -> List[str]:
    values = _distinct_values(connector, table, column, _MAX_VALUE_EXAMPLES)
    return [v[:_MAX_VALUE_LENGTH] for v in values]


def _value_statistics(connector: Any, table: str, column: str) -> Dict[str, int]:
    dialect = (getattr(connector, "dialect", "") or "").lower()
    if dialect in ("mssql", "sqlserver"):
        sql = (
            f"SELECT COUNT(*), COUNT(DISTINCT {column}), "
            f"COUNT(*) - COUNT({column}) FROM (SELECT TOP {_STATS_LIMIT} {column} "
            f"FROM {table}) AS _t"
        )
    else:
        sql = (
            f"SELECT COUNT(*), COUNT(DISTINCT {column}), "
            f"COUNT(*) - COUNT({column}) FROM "
            f"(SELECT {column} FROM {table} LIMIT {_STATS_LIMIT}) AS _t"
        )
    try:
        result = connector.run(sql)
        if not result or not result[1:]:
            return {}
        row = result[1]
        return {
            "total": int(row[0] or 0),
            "distinct": int(row[1] or 0),
            "null": int(row[2] or 0),
        }
    except Exception as e:  # pragma: no cover
        logger.debug("value statistics failed for %s.%s: %s", table, column, e)
        return {}
