"""Schema-aware multi-schema reflection adapter.

DB-GPT's :class:`~dbgpt.datasource.rdbms.base.RDBMSConnector` reflects only the
database's default schema. Many production schemas (e.g. the Pusula/MedipolDB
HIS) split tables across several named schemas (``Hasta``, ``Tedavi``,
``Ortak``, …). :class:`SchemaAwareConnector` wraps a connector and exposes the
*union* of tables across all non-system schemas using schema-qualified names
(``"Hasta"."Hasta"``), so the DeepEye-SQL pipeline can reflect and query them.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .util import split_qualified

logger = logging.getLogger(__name__)

DEFAULT_EXCLUDED_SCHEMAS = {
    "information_schema",
    "pg_catalog",
    "pg_toast",
    "sys",
    "mysql",
    "performance_schema",
    "tempdb",
    "model",
    "msdb",
    "sqlite_master",
}


class SchemaAwareConnector:
    """Wrap a connector to reflect tables across multiple schemas."""

    def __init__(
        self,
        connector: Any,
        schemas: Optional[List[str]] = None,
        exclude_schemas: Optional[List[str]] = None,
    ):
        self._connector = connector
        engine = getattr(connector, "_engine", None)
        if engine is None:
            raise ValueError(
                "SchemaAwareConnector requires a connector exposing a SQLAlchemy "
                "`_engine` attribute (e.g. RDBMSConnector)."
            )
        from sqlalchemy import inspect

        self._inspector = inspect(engine)
        available = set(self._inspector.get_schema_names())

        if schemas:
            self._schemas = [s for s in schemas if s in available]
        else:
            excluded = {s.lower() for s in (exclude_schemas or DEFAULT_EXCLUDED_SCHEMAS)}
            self._schemas = [s for s in available if s.lower() not in excluded]

        self._tables: List[str] = []
        for schema in self._schemas:
            try:
                for name in self._inspector.get_table_names(schema=schema):
                    self._tables.append(f"{schema}.{name}")
            except Exception as e:  # pragma: no cover - dialect-specific
                logger.debug("reflect tables in schema %s failed: %s", schema, e)
        self._tables = sorted(set(self._tables))

    @property
    def schemas(self) -> List[str]:
        return list(self._schemas)

    # ------------------------------------------------------------------
    # Delegation (execution / identity)
    # ------------------------------------------------------------------
    @property
    def dialect(self) -> str:
        return (getattr(self._connector, "dialect", "") or "").lower()

    @property
    def db_type(self) -> str:
        return getattr(self._connector, "db_type", "") or ""

    @property
    def db_url(self) -> str:
        try:
            return str(self._connector.db_url)
        except Exception:
            return ""

    def get_current_db_name(self) -> str:
        fn = getattr(self._connector, "get_current_db_name", None)
        if callable(fn):
            try:
                return str(fn())
            except Exception:
                pass
        return self._schemas[0] if self._schemas else ""

    def run(self, sql: str, fetch: str = "all") -> List:
        return self._connector.run(sql, fetch)

    def query_ex(self, *args: Any, **kwargs: Any):
        fn = getattr(self._connector, "query_ex", None)
        if callable(fn):
            return fn(*args, **kwargs)
        return self._connector.run(args[0] if args else "")

    # ------------------------------------------------------------------
    # Reflection (schema-qualified)
    # ------------------------------------------------------------------
    def get_table_names(self) -> List[str]:
        return list(self._tables)

    def get_columns(self, table: str) -> List[Dict[str, Any]]:
        schema, name = split_qualified(table)
        try:
            raw = self._inspector.get_columns(name, schema=schema or None)
        except Exception:
            return []
        return [
            {
                "name": c.get("name", ""),
                "type": str(c.get("type", "") or ""),
                "is_in_primary_key": bool(c.get("primary_key")),
                "comment": c.get("comment") or "",
            }
            for c in raw
        ]

    def get_table_comment(self, table: str) -> Dict[str, Any]:
        schema, name = split_qualified(table)
        try:
            comment = self._inspector.get_table_comment(name, schema=schema or None)
            return {"text": (comment or {}).get("text") or ""}
        except Exception:
            return {"text": ""}

    def get_foreign_keys(
        self, table: Optional[str] = None
    ) -> List[Tuple[str, str, str, str]]:
        """Return ``(src_table, src_col, tgt_table, tgt_col)`` FK tuples.

        Table names are schema-qualified (``schema.table``).
        """
        fks: List[Tuple[str, str, str, str]] = []
        for schema in self._schemas:
            for name in self._inspector.get_table_names(schema=schema):
                if table is not None:
                    t_schema, t_name = split_qualified(table)
                    if not (t_schema == schema and t_name == name):
                        continue
                try:
                    for fk in self._inspector.get_foreign_keys(name, schema=schema):
                        ref_schema = fk.get("referred_schema") or schema
                        ref_table = fk.get("referred_table")
                        for sc, tc in zip(
                            fk.get("constrained_columns", []),
                            fk.get("referred_columns", []),
                        ):
                            fks.append(
                                (
                                    f"{schema}.{name}",
                                    sc,
                                    f"{ref_schema}.{ref_table}",
                                    tc,
                                )
                            )
                except Exception as e:  # pragma: no cover - dialect-specific
                    logger.debug("reflect FKs for %s.%s failed: %s", schema, name, e)
        return fks


def wrap_multi_schema(
    connector: Any,
    schemas: Optional[List[str]] = None,
    exclude_schemas: Optional[List[str]] = None,
) -> SchemaAwareConnector:
    """Wrap a connector for multi-schema reflection (idempotent)."""
    if isinstance(connector, SchemaAwareConnector):
        return connector
    return SchemaAwareConnector(connector, schemas, exclude_schemas)
