import importlib.util
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import tomlkit

spec = importlib.util.spec_from_file_location(
    "erp_entrypoint", Path(__file__).with_name("entrypoint.py")
)
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.db"
        self.target = self.root / "state" / "dbgpt.db"
        self.erp = self.root / "erp.sqlite"
        versions = self.root / "alembic" / "versions"
        versions.mkdir(parents=True)
        (versions / "existing_revision.py").write_text("revision = 'existing'\n")
        (self.root / "alembic.ini").write_text("[alembic]\n")
        with closing(sqlite3.connect(self.erp)) as db, db:
            db.execute("CREATE TABLE sales (amount INTEGER)")
            db.execute("INSERT INTO sales VALUES (42)")
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute(
                "CREATE TABLE connect_config (db_name TEXT, db_type TEXT, db_path TEXT)"
            )
            db.executemany(
                "INSERT INTO connect_config VALUES (?, ?, ?)",
                [
                    ("erp_trial_readonly", "sqlite", "file:D%3A/erp.sqlite?mode=ro"),
                    ("other", "sqlite", "untouched.db"),
                ],
            )
            db.execute("CREATE TABLE history (message TEXT)")
            db.execute("INSERT INTO history VALUES ('original')")

    def test_backup_preserves_source_and_unrelated_data(self):
        original = self.source.read_bytes()
        entrypoint.prepare_metadata(self.source, self.target, self.erp)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(
            (
                self.target.parent / "alembic" / "versions" / "existing_revision.py"
            ).read_text(),
            "revision = 'existing'\n",
        )
        with closing(sqlite3.connect(self.target)) as db, db:
            rows = dict(db.execute("SELECT db_name, db_path FROM connect_config"))
            self.assertEqual(rows["erp_trial_readonly"], entrypoint.ERP_URI)
            self.assertEqual(rows["other"], "untouched.db")
            self.assertEqual(
                db.execute("SELECT * FROM history").fetchall(), [("original",)]
            )

    def test_restart_retains_container_history(self):
        entrypoint.prepare_metadata(self.source, self.target, self.erp)
        with closing(sqlite3.connect(self.target)) as db, db:
            db.execute("INSERT INTO history VALUES ('docker')")
        entrypoint.prepare_metadata(self.source, self.target, self.erp)
        with closing(sqlite3.connect(self.target)) as db, db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM history").fetchone(), (2,)
            )

    def test_windows_demo_path_uses_existing_bundled_database_readonly(self):
        examples = self.root / "examples"
        examples.mkdir()
        demo = examples / "Walmart_Sales.db"
        with closing(sqlite3.connect(demo)) as db, db:
            db.execute("CREATE TABLE walmart_sales (sales REAL)")
            db.execute("INSERT INTO walmart_sales VALUES (42)")
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute(
                "INSERT INTO connect_config VALUES (?, ?, ?)",
                ("Walmart_Sales", "sqlite", r"D:\host\pilot\examples\Walmart_Sales.db"),
            )
        original = demo.read_bytes()

        def container_path(value):
            return examples if value == "/app/pilot/examples" else Path(value)

        with patch.object(entrypoint, "Path", side_effect=container_path):
            entrypoint.prepare_metadata(self.source, self.target, self.erp)
        with closing(sqlite3.connect(self.target)) as db:
            uri = db.execute(
                "SELECT db_path FROM connect_config WHERE db_name=?", ("Walmart_Sales",)
            ).fetchone()[0]
        with closing(sqlite3.connect(uri.replace("&uri=true", ""), uri=True)) as db:
            self.assertEqual(
                db.execute("SELECT sales FROM walmart_sales").fetchone(), (42,)
            )
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                db.execute("DELETE FROM walmart_sales")
        self.assertEqual(demo.read_bytes(), original)

    def test_missing_registration_fails_without_publishing_copy(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("DELETE FROM connect_config WHERE db_name='erp_trial_readonly'")
        with self.assertRaisesRegex(RuntimeError, "registered SQLite"):
            entrypoint.prepare_metadata(self.source, self.target, self.erp)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.target.with_suffix(".initializing").exists())

    def test_missing_erp_fails_before_copying(self):
        with self.assertRaises(FileNotFoundError):
            entrypoint.prepare_metadata(self.source, self.target, self.root / "missing")
        self.assertFalse(self.target.exists())

    def test_erp_uri_reads_and_rejects_writes(self):
        uri = entrypoint.ERP_URI.replace("/erp/erp_trial.sqlite", self.erp.as_posix())
        # uri=true is the SQLAlchemy option; sqlite3 receives mode=ro itself.
        with closing(sqlite3.connect(uri.replace("&uri=true", ""), uri=True)) as db:
            self.assertEqual(db.execute("SELECT amount FROM sales").fetchall(), [(42,)])
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                db.execute("INSERT INTO sales VALUES (1)")

    def test_config_preserves_models_and_applies_optional_environment(self):
        source = self.root / "source.toml"
        source.write_text(
            '[service.web]\nhost="localhost"\nport=9999\n'
            '[service.web.database]\ntype="sqlite"\npath="old.db"\n'
            '[[models.llms]]\nprovider="proxy/deepseek"\nname="existing"\n'
            'api_key="test-only"\n'
            '[[models.embeddings]]\nname="existing-embedding"\ndevice="cpu"\n',
            encoding="utf-8",
        )
        before = source.read_bytes()
        target = self.root / "runtime.toml"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "", "DEEPSEEK_API_BASE": ""}):
            entrypoint.prepare_config(source, target, self.target)
        config = tomlkit.parse(target.read_text(encoding="utf-8"))
        self.assertEqual(config["models"]["llms"][0]["api_key"], "test-only")
        self.assertEqual(config["models"]["embeddings"][0]["device"], "cpu")
        self.assertEqual(config["service"]["web"]["port"], 5670)
        self.assertEqual(config["service"]["web"]["database"]["path"], str(self.target))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "override-test-only"}):
            entrypoint.prepare_config(source, target, self.target)
        config = tomlkit.parse(target.read_text(encoding="utf-8"))
        self.assertEqual(config["models"]["llms"][0]["api_key"], "override-test-only")
        self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
