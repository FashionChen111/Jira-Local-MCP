"""Incompatible SQLite schemas fail explicitly without conversion."""
from __future__ import annotations

import sqlite3

import pytest

from core.storage import database as database_mod
from core.storage.database import open_database


def test_schema_initialization_runs_once_per_process_and_path(cfg, monkeypatch):
    calls = 0
    original = database_mod._ensure_schema

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(database_mod, "_ensure_schema", counted)
    first = open_database("operations")
    first.close()
    second = open_database("operations")
    second.close()
    assert calls == 1


def test_operations_v2_schema_contains_references_and_concrete_execution(cfg):
    connection = open_database("operations")
    queue_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(queue_items)").fetchall()
    }
    execution_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(execution_records)").fetchall()
    }
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.close()
    assert version == 2
    assert {"manual_after_operation_id", "issue_references_json"} <= queue_columns
    assert {
        "issue_references_json",
        "executed_issue_key",
        "executed_payload_json",
    } <= execution_columns


def test_incompatible_operations_schema_requires_manual_clear(cfg):
    connection = sqlite3.connect(cfg.operations_db_file)
    connection.execute("PRAGMA user_version=1")
    connection.close()
    with pytest.raises(RuntimeError, match="不兼容"):
        open_database("operations")


def test_incompatible_mcp_log_schema_does_not_block_operation_store(cfg):
    connection = sqlite3.connect(cfg.mcp_logs_db_file)
    connection.execute("PRAGMA user_version=99")
    connection.close()
    from core.operations import OperationStore

    store = OperationStore()
    store.initialize()
    operation = store.enqueue(
        "add_comment", "PROJ-1", {"issue_key": "PROJ-1", "body": "x"}, "comment", "test"
    )
    assert operation.operation_id.startswith("op-")
