"""Consistent SQLite connections and low-noise slow-operation diagnostics."""

import logging
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)

_wal_paths = set()
_wal_lock = threading.Lock()


def _convert_boolean(value: bytes) -> bool:
    """Convert a SQLite BOOLEAN value to a Python bool."""
    return value not in {b"0", b"false", b"False", b""}


def _convert_json(value: bytes):
    """Convert a SQLite JSON value to its Python representation."""
    return json.loads(value.decode("utf-8"))


sqlite3.register_converter("BOOLEAN", _convert_boolean)
sqlite3.register_converter("JSON", _convert_json)
sqlite3.register_adapter(dict, json.dumps)
sqlite3.register_adapter(list, json.dumps)


def connect_sqlite(
    path,
    *,
    timeout: float = 5.0,
    row_factory: bool = False,
    typed: bool = False,
) -> sqlite3.Connection:
    """Open a consistently configured connection to a local SQLite database."""
    database_path = Path(path)
    connection = sqlite3.connect(
        database_path,
        timeout=timeout,
        check_same_thread=False,
        detect_types=sqlite3.PARSE_DECLTYPES if typed else 0,
    )
    try:
        connection.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        if row_factory:
            connection.row_factory = sqlite3.Row

        resolved_path = str(database_path.resolve())
        if resolved_path not in _wal_paths:
            with _wal_lock:
                if resolved_path not in _wal_paths:
                    journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                    if str(journal_mode).lower() == "wal":
                        _wal_paths.add(resolved_path)
                    else:
                        logger.warning(
                            "SQLite WAL unavailable path=%s journal_mode=%s",
                            database_path,
                            journal_mode,
                        )
        return connection
    except Exception:
        connection.close()
        raise


def slow_operation_threshold_ms() -> float:
    """Return the threshold above which successful DB operations are logged."""
    try:
        return max(0.0, float(os.environ.get("SQLITE_SLOW_OPERATION_MS", "100")))
    except ValueError:
        return 100.0


def log_slow_operation(
    *,
    database: str,
    operation: str,
    started_at: float,
    lock_wait_ms: Optional[float] = None,
    rows: Optional[int] = None,
) -> None:
    """Log only slow successful operations, keeping routine DB traffic quiet."""
    total_ms = (time.perf_counter() - started_at) * 1000
    if total_ms < slow_operation_threshold_ms():
        return
    logger.warning(
        "sqlite_slow_operation database=%s operation=%s total_ms=%.2f "
        "lock_wait_ms=%s rows=%s",
        database,
        operation,
        total_ms,
        f"{lock_wait_ms:.2f}" if lock_wait_ms is not None else "unknown",
        rows if rows is not None else "unknown",
    )
