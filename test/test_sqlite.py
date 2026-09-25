"""Shared SQLite connection behavior tests."""

import time
import logging
from app.utils import sqlite_utils


def test_shared_sqlite_connection_enables_wal_and_foreign_keys(tmp_path):
    database = tmp_path / "test.db"

    with sqlite_utils.connect_sqlite(database, row_factory=True) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_slow_operation_logging_is_thresholded(monkeypatch, caplog):
    monkeypatch.setenv("SQLITE_SLOW_OPERATION_MS", "1000")
    with caplog.at_level(logging.WARNING):
        sqlite_utils.log_slow_operation(
            database="settings",
            operation="fast_test",
            started_at=time.perf_counter(),
        )
    assert "fast_test" not in caplog.text

    monkeypatch.setenv("SQLITE_SLOW_OPERATION_MS", "0")
    with caplog.at_level(logging.WARNING):
        sqlite_utils.log_slow_operation(
            database="settings",
            operation="slow_test",
            started_at=time.perf_counter(),
            lock_wait_ms=12.5,
            rows=3,
        )
    assert "sqlite_slow_operation database=settings operation=slow_test" in caplog.text
    assert "lock_wait_ms=12.50 rows=3" in caplog.text
