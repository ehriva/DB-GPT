"""Existing business files must not become silently created empty databases."""

from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from dbgpt_ext.datasource.rdbms.conn_sqlite import SQLiteConnector
from dbgpt_serve.datasource.manages.connect_config_db import ConnectConfigDao
from dbgpt_serve.datasource.manages.connector_manager import ConnectorManager


def manager_for(path):
    manager = ConnectorManager.__new__(ConnectorManager)
    manager.storage = SimpleNamespace(
        get_db_config=lambda name: {
            "db_pwd": "",
            "db_type": "sqlite",
            "db_path": str(path),
        }
    )
    manager.get_cls_by_dbtype = lambda kind: SQLiteConnector
    return manager


def test_missing_business_path_does_not_create_file(tmp_path):
    path = tmp_path / "missing" / "sales.sqlite"
    with pytest.raises(FileNotFoundError):
        manager_for(path)._build_connector("sales")
    assert not path.parent.exists()


def test_windows_absolute_path_is_rejected_in_linux(monkeypatch):
    import dbgpt_ext.datasource.rdbms.conn_sqlite as module

    monkeypatch.setattr(module.sys, "platform", "linux")
    with pytest.raises(ValueError, match="容器"):
        manager_for("D:/code/sales.sqlite")._build_connector("sales")


def test_existing_unicode_file_uri_is_readonly(tmp_path):
    path = tmp_path / "销售 成本.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sales(amount INTEGER)"))
        connection.execute(text("INSERT INTO sales VALUES (10)"))
    engine.dispose()
    connector = manager_for(path.as_uri() + "?mode=ro&uri=true")._build_connector(
        "sales"
    )
    with connector._engine.connect() as connection:
        assert connection.execute(text("SELECT amount FROM sales")).scalar() == 10
        with pytest.raises(OperationalError):
            connection.execute(text("INSERT INTO sales VALUES (20)"))
    connector._engine.dispose()


def test_memory_and_explicit_creation_remain_supported(tmp_path):
    memory = manager_for(":memory:")._build_connector("memory")
    assert list(memory.get_table_names()) == []
    memory._engine.dispose()
    path = tmp_path / "created" / "temp.sqlite"
    connector = SQLiteConnector.from_file_path(str(path))
    assert path.is_file()
    connector._engine.dispose()


def test_update_uses_bound_parameters_and_closes_session():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE connect_config (db_name TEXT, db_type TEXT, db_path TEXT, comment TEXT)"
            )
        )
        connection.execute(
            text("INSERT INTO connect_config VALUES ('sales', 'sqlite', 'old', '')")
        )
    dao = ConnectConfigDao.__new__(ConnectConfigDao)
    dao.get_db_config = lambda name: {"db_name": name}
    sessions = []

    def session():
        result = sessionmaker(bind=engine)()
        result.close = Mock(wraps=result.close)
        sessions.append(result)
        return result

    dao.get_raw_session = session
    assert dao.update_db_info("sales", "sqlite", "price's.sqlite", comment="用户's库")
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT db_path FROM connect_config")).scalar()
            == "price's.sqlite"
        )
    assert sessions[0].close.called
    engine.dispose()


def test_failed_update_raises_and_closes_session():
    session = Mock()
    session.execute.side_effect = RuntimeError("write failed")
    dao = ConnectConfigDao.__new__(ConnectConfigDao)
    dao.get_db_config = lambda name: {"db_name": name}
    dao.get_raw_session = lambda: session
    with pytest.raises(RuntimeError):
        dao.update_db_info("sales", "sqlite", "new.sqlite")
    session.rollback.assert_called_once()
    session.close.assert_called_once()


def test_edit_invalidates_only_after_success():
    manager = ConnectorManager.__new__(ConnectorManager)
    manager.storage = SimpleNamespace(update_db_info=Mock(return_value=True))
    manager.invalidate_connector = Mock()
    manager._connector_cache_lock = threading.Lock()
    manager._connector_creation_locks = {}
    config = SimpleNamespace(
        db_name="sales",
        db_type="sqlite",
        file_path=":memory:",
        db_host="",
        db_port=0,
        db_user="",
        db_pwd="",
        comment="",
    )
    assert manager.edit_db(config)
    manager.invalidate_connector.assert_called_once_with("sales")
    manager.invalidate_connector.reset_mock()
    manager.storage.update_db_info.side_effect = RuntimeError("write failed")
    with pytest.raises(RuntimeError):
        manager.edit_db(config)
    manager.invalidate_connector.assert_not_called()
