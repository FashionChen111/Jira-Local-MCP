"""Connection, schema, transaction, ID, time, and JSON helpers."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Literal

from config import get_config

OPERATIONS_SCHEMA_VERSION = 3
MCP_LOG_SCHEMA_VERSION = 1
OPERATIONS_SCHEMA_IDENTITY = (
    "jira-local-mcp-operations-attachment-results-v3"
)
BUSY_TIMEOUT_MS = 10_000
_INITIALIZATION_LOCK = threading.Lock()
_INITIALIZED_DATABASES: set[tuple[str, str]] = set()
_SQL_DIR = Path(__file__).with_name("sql")


def utc_ms() -> int:
    return time.time_ns() // 1_000_000


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_json(value: str) -> object:
    return json.loads(value)


def _connect(path: str, *, initialize_journal: bool = False) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    if initialize_journal:
        connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _load_schema(filename: str) -> str:
    return (_SQL_DIR / filename).read_text(encoding="utf-8")


_OPERATIONS_SCHEMA = _load_schema(f"operations_v{OPERATIONS_SCHEMA_VERSION}.sql")
_MCP_SCHEMA = _load_schema(f"mcp_logs_v{MCP_LOG_SCHEMA_VERSION}.sql")


def _ensure_schema(
    connection: sqlite3.Connection,
    version: int,
    schema: str,
    label: str,
    schema_identity: str | None = None,
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current not in (0, version):
            raise RuntimeError(
                f"{label} schema 版本 {current} 与所需版本 {version} 不兼容；请清理对应 SQLite 文件"
            )
        if current == 0:
            if connection.execute(
                "SELECT 1 FROM sqlite_master LIMIT 1"
            ).fetchone() is not None:
                raise RuntimeError(
                    f"{label} schema 未标记版本且已有结构；请清理对应 SQLite 文件"
                )
            statement = ""
            for line in schema.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError("不完整的 schema SQL")
            connection.execute(f"PRAGMA user_version={version}")
        if schema_identity is not None:
            try:
                row = connection.execute(
                    "SELECT value FROM schema_metadata WHERE name='schema_identity'"
                ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise RuntimeError(
                    f"{label} schema 缺少当前结构标识；请清理对应 SQLite 文件"
                ) from exc
            if row is None or row[0] != schema_identity:
                raise RuntimeError(
                    f"{label} schema 结构标识不兼容；请清理对应 SQLite 文件"
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _database_details(
    kind: Literal["operations", "mcp_logs"],
) -> tuple[str, int, str, str, str | None]:
    cfg = get_config()
    if kind == "operations":
        return (
            cfg.operations_db_file,
            OPERATIONS_SCHEMA_VERSION,
            _OPERATIONS_SCHEMA,
            "operations",
            OPERATIONS_SCHEMA_IDENTITY,
        )
    return cfg.mcp_logs_db_file, MCP_LOG_SCHEMA_VERSION, _MCP_SCHEMA, "mcp_logs", None


def _initialize_database(kind: Literal["operations", "mcp_logs"]) -> str:
    path, version, schema, label, schema_identity = _database_details(kind)
    resolved = os.path.normcase(os.path.abspath(path))
    key = (kind, resolved)
    if key in _INITIALIZED_DATABASES:
        return path
    with _INITIALIZATION_LOCK:
        if key in _INITIALIZED_DATABASES:
            return path
        connection = _connect(path, initialize_journal=True)
        try:
            _ensure_schema(connection, version, schema, label, schema_identity)
        finally:
            connection.close()
        _INITIALIZED_DATABASES.add(key)
    return path


def open_database(kind: Literal["operations", "mcp_logs"]) -> sqlite3.Connection:
    return _connect(_initialize_database(kind))


def reset_database_initialization() -> None:
    """Clear process-local initialization state after a test configuration switch."""
    with _INITIALIZATION_LOCK:
        _INITIALIZED_DATABASES.clear()


@contextmanager
def transaction(
    kind: Literal["operations", "mcp_logs"] = "operations",
) -> Generator[sqlite3.Connection, None, None]:
    connection = open_database(kind)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
