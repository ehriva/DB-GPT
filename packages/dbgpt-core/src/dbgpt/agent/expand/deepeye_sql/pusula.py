"""Pusula (MedipolDB) connector factory.

Pusula is a Turkish hospital information system whose de-identified MedipolDB
schema splits tables across several named schemas. This module wires the
multi-schema reflection into a single connector for the DeepEye-SQL pipeline.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

from .multischema import wrap_multi_schema

logger = logging.getLogger(__name__)

# Named schemas in the Pusula/MedipolDB source (from data/schema_catalog.json).
PUSULA_SCHEMAS = [
    "Hasta",
    "Tedavi",
    "Ortak",
    "LIS",
    "RIS",
    "Stok",
    "IK",
    "MedipolDB",
]


def pusula_db_url() -> Optional[str]:
    """Return the Pusula database URL from the environment, if configured."""
    return (
        os.getenv("PUSULA_DB_URL")
        or os.getenv("OMOP_SOURCE_DB_URL")
        or os.getenv("MEDIPOLDB_DB_URL")
    )


def build_pusula_connector(
    db_url: Optional[str] = None,
    schemas: Optional[List[str]] = None,
    engine_args: Optional[dict] = None,
) -> Any:
    """Build a schema-aware connector for the Pusula database.

    Args:
        db_url: SQLAlchemy URL (e.g.
            ``postgresql+psycopg://postgres:postgres@localhost:5433/medipol_dev``).
            Defaults to ``PUSULA_DB_URL`` / ``OMOP_SOURCE_DB_URL`` /
            ``MEDIPOLDB_DB_URL`` env vars.
        schemas: Schemas to reflect. Defaults to :data:`PUSULA_SCHEMAS` (any
            that exist in the database).
        engine_args: Optional engine arguments passed to ``create_engine``.
    """
    url = db_url or pusula_db_url()
    if not url:
        raise ValueError(
            "No Pusula database URL configured. Set PUSULA_DB_URL (or "
            "OMOP_SOURCE_DB_URL / MEDIPOLDB_DB_URL) or pass db_url=."
        )
    from dbgpt.datasource.rdbms.base import RDBMSConnector

    connector = RDBMSConnector.from_uri(url, engine_args)
    return wrap_multi_schema(connector, schemas=schemas or PUSULA_SCHEMAS)
