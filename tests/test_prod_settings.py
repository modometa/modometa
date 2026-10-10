import importlib
import sys
from unittest.mock import MagicMock

from django.test import override_settings

import config.settings.prod as prod_settings
from core.apps import configure_sqlite_pragmas


def test_prod_cache_max_entries():
    """Prod settings configures LocMemCache with 1,500 entries to bound memory."""
    cache_cfg = prod_settings.CACHES["default"]
    assert cache_cfg["OPTIONS"]["MAX_ENTRIES"] == 1_500


def test_prod_settings_read_only_mode_enabled(monkeypatch):
    """When DATABASE_READ_ONLY is true, DATABASES uses SQLite immutable URI."""
    monkeypatch.setenv("DATABASE_READ_ONLY", "true")
    monkeypatch.setenv("DATABASE_PATH", "/app/modometa.db")

    if "config.settings.prod" in sys.modules:
        del sys.modules["config.settings.prod"]

    reloaded_prod = importlib.import_module("config.settings.prod")
    assert reloaded_prod.DATABASE_READ_ONLY is True
    db_cfg = reloaded_prod.DATABASES["default"]
    assert db_cfg["NAME"] == "file:/app/modometa.db?mode=ro&immutable=1"
    assert db_cfg["OPTIONS"].get("uri") is True


def test_prod_settings_read_only_mode_disabled(monkeypatch):
    """When DATABASE_READ_ONLY is false, DATABASES uses regular path."""
    monkeypatch.setenv("DATABASE_READ_ONLY", "false")
    monkeypatch.setenv("DATABASE_PATH", "/app/modometa.db")

    if "config.settings.prod" in sys.modules:
        del sys.modules["config.settings.prod"]

    reloaded_prod = importlib.import_module("config.settings.prod")
    assert reloaded_prod.DATABASE_READ_ONLY is False
    db_cfg = reloaded_prod.DATABASES["default"]
    assert db_cfg["NAME"] == "/app/modometa.db"
    assert "uri" not in db_cfg["OPTIONS"]


def test_configure_sqlite_pragmas_non_sqlite():
    """Non-sqlite connections are ignored by configure_sqlite_pragmas."""
    mock_connection = MagicMock()
    mock_connection.vendor = "postgresql"
    configure_sqlite_pragmas(None, mock_connection)
    mock_connection.cursor.assert_not_called()


def test_configure_sqlite_pragmas_sqlite_read_write():
    """SQLite connections receive mmap, cache_size, and temp_store pragmas."""
    mock_connection = MagicMock()
    mock_connection.vendor = "sqlite"
    mock_cursor = MagicMock()
    mock_connection.cursor.return_value.__enter__.return_value = mock_cursor

    with override_settings(DATABASE_READ_ONLY=False):
        configure_sqlite_pragmas(None, mock_connection)

    executed_sqls = [call.args[0] for call in mock_cursor.execute.call_args_list]
    assert "PRAGMA mmap_size = 268435456;" in executed_sqls
    assert "PRAGMA cache_size = -32000;" in executed_sqls
    assert "PRAGMA temp_store = MEMORY;" in executed_sqls
    assert "PRAGMA query_only = ON;" not in executed_sqls


def test_configure_sqlite_pragmas_sqlite_read_only():
    """SQLite connections in DATABASE_READ_ONLY mode receive PRAGMA query_only = ON."""
    mock_connection = MagicMock()
    mock_connection.vendor = "sqlite"
    mock_cursor = MagicMock()
    mock_connection.cursor.return_value.__enter__.return_value = mock_cursor

    with override_settings(DATABASE_READ_ONLY=True):
        configure_sqlite_pragmas(None, mock_connection)

    executed_sqls = [call.args[0] for call in mock_cursor.execute.call_args_list]
    assert "PRAGMA query_only = ON;" in executed_sqls
