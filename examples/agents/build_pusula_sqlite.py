"""Build a runnable SQLite replica of the Pusula (MedipolDB) schema.

Reads the de-identified schema catalog (``data/schema_catalog.json`` from the
OMOPIntegration project) and produces a SQLite database that mirrors the
Pusula multi-schema layout using ``ATTACH DATABASE`` (one file per named
schema: ``Hasta``, ``Tedavi``, ``Ortak``, ``LIS``, ``RIS``, ``Stok``, ``IK``,
``MedipolDB``). This lets you run the DeepEye-SQL pipeline without a live
PostgreSQL server.

Usage::

    python examples/agents/build_pusula_sqlite.py \
        --catalog /path/to/schema_catalog.json \
        --out /tmp/pusula_demo \
        --rows 3
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3

_TYPE_MAP = {
    "int": "INTEGER",
    "smallint": "INTEGER",
    "bigint": "INTEGER",
    "tinyint": "INTEGER",
    "bit": "INTEGER",
    "decimal": "REAL",
    "numeric": "REAL",
    "money": "REAL",
    "float": "REAL",
    "real": "REAL",
    "nvarchar": "TEXT",
    "varchar": "TEXT",
    "nchar": "TEXT",
    "char": "TEXT",
    "text": "TEXT",
    "ntext": "TEXT",
    "uniqueidentifier": "TEXT",
    "datetime": "TEXT",
    "smalldatetime": "TEXT",
    "date": "TEXT",
    "time": "TEXT",
    "datetime2": "TEXT",
    "image": "BLOB",
    "varbinary": "BLOB",
    "binary": "BLOB",
}


def col_type(sql_type: str) -> str:
    return _TYPE_MAP.get(str(sql_type).lower(), "TEXT")


def _synthetic_value(col_name: str, sql_type: str, i: int):
    if col_type(sql_type) == "INTEGER":
        return i + 1
    if col_type(sql_type) == "REAL":
        return float(i + 1) + 0.5
    return f"{col_name}_{i + 1}"


def build(catalog_path: str, out_dir: str, rows: int = 3) -> dict:
    """Build the SQLite replica. Returns the schema -> file mapping."""
    with open(catalog_path, encoding="utf-8") as f:
        catalog = json.load(f)

    schemas: dict = {}
    for entry in catalog:
        name = entry.get("name", "")
        if "." not in name:
            continue  # skip malformed entries (e.g. a stray column mis-parsed)
        schema, table = name.split(".", 1)
        schemas.setdefault(schema, []).append((table, entry))

    os.makedirs(out_dir, exist_ok=True)
    main_path = os.path.join(out_dir, "main.sqlite")
    if os.path.exists(main_path):
        os.remove(main_path)

    conn = sqlite3.connect(main_path)
    mapping: dict = {}
    for schema in schemas:
        fname = f"{schema}.sqlite"
        fpath = os.path.join(out_dir, fname)
        if os.path.exists(fpath):
            os.remove(fpath)
        conn.execute(f'ATTACH DATABASE ? AS "{schema}"', (fpath,))
        mapping[schema] = fname

    for schema, tables in schemas.items():
        for table, entry in tables:
            cols = [
                f'"{c["name"]}" {col_type(c.get("data_type", ""))}'
                for c in entry.get("columns", [])
            ]
            if not cols:
                cols = ['"Id" INTEGER']
            conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{schema}"."{table}" ('
                + ", ".join(cols)
                + ")"
            )
            names = [c["name"] for c in entry.get("columns", [])]
            if not names:
                continue
            placeholders = ", ".join("?" * len(names))
            insert = (
                f'INSERT INTO "{schema}"."{table}" ('
                + ", ".join(f'"{n}"' for n in names)
                + f") VALUES ({placeholders})"
            )
            for i in range(rows):
                vals = [
                    _synthetic_value(n, c.get("data_type", ""), i)
                    for n, c in zip(names, entry.get("columns", []))
                ]
                conn.execute(insert, vals)

    conn.commit()
    conn.close()
    with open(os.path.join(out_dir, "schemas.json"), "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)
    return mapping


def connect(out_dir: str):
    """Open the replica as a SQLAlchemy engine with all schemas attached."""
    import sqlalchemy
    from sqlalchemy import event

    with open(os.path.join(out_dir, "schemas.json"), encoding="utf-8") as f:
        mapping = json.load(f)
    engine = sqlalchemy.create_engine(
        f"sqlite:///{os.path.join(out_dir, 'main.sqlite')}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_conn, _record):
        for schema, fname in mapping.items():
            dbapi_conn.execute(
                f'ATTACH DATABASE "{os.path.join(out_dir, fname)}" AS "{schema}"'
            )

    return engine


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Pusula SQLite replica")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--rows", type=int, default=3)
    args = parser.parse_args()
    mapping = build(args.catalog, args.out, args.rows)
    print(f"Built {len(mapping)} schemas in {args.out}:")
    for schema, fname in mapping.items():
        print(f"  {schema} -> {fname}")


if __name__ == "__main__":
    main()
