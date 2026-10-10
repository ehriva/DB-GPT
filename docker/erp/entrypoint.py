"""Seed container metadata without changing the Windows metadata database."""

import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path, PureWindowsPath

import tomlkit

ERP_URI = "file:/erp/erp_trial.sqlite?mode=ro&uri=true"

# Directories that ship SQLite datasources inside this container. Windows-absolute
# db_path values from the host metadata are resolved against these by file name.
SQLITE_DATASOURCE_DIRS = (
    Path("/app/pilot/examples"),
    Path("/data/monthly_sales_cost"),
)


def prepare_metadata(source: Path, target: Path, erp: Path) -> None:
    if not erp.is_file():
        raise FileNotFoundError(f"ERP database is missing: {erp}")
    # Verify readability without creating a missing database or changing its data.
    with closing(sqlite3.connect(f"{erp.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
    target.parent.mkdir(parents=True, exist_ok=True)
    # DB-GPT locates Alembic scripts beside an absolute metadata database path.
    shutil.copytree(
        source.parent / "alembic",
        target.parent / "alembic",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy2(source.parent / "alembic.ini", target.parent / "alembic.ini")
    if not target.exists():
        if not source.is_file():
            raise FileNotFoundError(f"Source metadata database is missing: {source}")
        temporary = target.with_suffix(".initializing")
        try:
            with (
                closing(
                    sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
                ) as original,
                closing(sqlite3.connect(temporary)) as copied,
            ):
                original.backup(copied)
            migrate_erp_path(temporary)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        migrate_erp_path(target)


def migrate_erp_path(target: Path) -> None:
    with closing(sqlite3.connect(target)) as db, db:
        row = db.execute(
            "SELECT db_type FROM connect_config WHERE db_name = ?",
            ("erp_trial_readonly",),
        ).fetchone()
        if row != ("sqlite",):
            raise RuntimeError(
                "Expected registered SQLite datasource erp_trial_readonly "
                "in source metadata. Register it before starting Docker."
            )
        db.execute(
            "UPDATE connect_config SET db_path = ? WHERE db_name = ?",
            (ERP_URI, "erp_trial_readonly"),
        )
        # Metadata seeded on Windows can also reference bundled or exported
        # databases. Resolve only files that actually ship with this container;
        # anything else keeps its original path untouched.
        for name, path in db.execute(
            "SELECT db_name, db_path FROM connect_config WHERE db_type = 'sqlite'"
        ).fetchall():
            if not path or not PureWindowsPath(path).is_absolute():
                continue
            for base in SQLITE_DATASOURCE_DIRS:
                candidate = base / PureWindowsPath(path).name
                if candidate.is_file():
                    with closing(
                        sqlite3.connect(f"{candidate.as_uri()}?mode=ro", uri=True)
                    ) as demo:
                        demo.execute(
                            "SELECT name FROM sqlite_master LIMIT 1"
                        ).fetchall()
                    db.execute(
                        "UPDATE connect_config SET db_path = ? WHERE db_name = ?",
                        (f"file:{candidate}?mode=ro&uri=true", name),
                    )
                    break


def prepare_config(source: Path, target: Path, metadata: Path) -> None:
    config = tomlkit.parse(source.read_text(encoding="utf-8"))
    config["service"]["web"]["host"] = "0.0.0.0"
    config["service"]["web"]["port"] = 5670
    config["service"]["web"]["database"]["path"] = str(metadata)
    for model in config["models"]["llms"]:
        if model["provider"] == "proxy/deepseek":
            if os.environ.get("DEEPSEEK_API_KEY"):
                model["api_key"] = os.environ["DEEPSEEK_API_KEY"]
            if os.environ.get("DEEPSEEK_API_BASE"):
                model["api_base"] = os.environ["DEEPSEEK_API_BASE"]
    # Runtime credentials stay out of the image, persistent volume and logs.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(tomlkit.dumps(config))


def main() -> None:
    metadata = Path("/app/erp-state/dbgpt.db")
    prepare_metadata(
        Path("/seed/meta_data/dbgpt.db"), metadata, Path("/erp/erp_trial.sqlite")
    )
    config = Path("/tmp/dbgpt-erp.toml")
    prepare_config(Path("/seed/config.toml"), config, metadata)
    os.execvp("dbgpt", ["dbgpt", "start", "webserver", "--config", str(config)])


if __name__ == "__main__":
    main()
