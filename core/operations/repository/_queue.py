"""Queued operation persistence and atomic enqueue event writes."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from core.operations.models import OperationRecord
from core.operations.repository._metadata import append_event
from core.operations.repository._records import (
    OPERATION_COLUMNS,
    operation_from_row,
    operation_snapshot,
    operation_values,
)


def queue_operations(connection: sqlite3.Connection) -> list[OperationRecord]:
    """Load every queued immutable operation snapshot."""

    rows = connection.execute(
        f"SELECT {OPERATION_COLUMNS} FROM queue_items"
    ).fetchall()
    return [operation_from_row(row) for row in rows]


def queue_operation(
    connection: sqlite3.Connection, operation_id: str
) -> OperationRecord | None:
    """Load one queued operation by server-generated ID."""

    row = connection.execute(
        f"SELECT {OPERATION_COLUMNS} FROM queue_items WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    return operation_from_row(row) if row is not None else None


def insert_queue(
    connection: sqlite3.Connection,
    operation: OperationRecord,
    *,
    action_code: str,
    event_data: dict[str, object],
) -> None:
    """Insert one immutable queue snapshot and its enqueue event."""

    connection.execute(
        f"INSERT INTO queue_items({OPERATION_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
        operation_values(operation),
    )
    append_event(
        connection,
        operation.operation_id,
        "enqueue",
        action_code,
        {"operation": operation_snapshot(operation), **event_data},
    )


def delete_queue(
    connection: sqlite3.Connection, operation_ids: Sequence[str]
) -> None:
    """Delete a validated set of queued operation IDs."""

    if not operation_ids:
        return
    placeholders = ",".join("?" for _ in operation_ids)
    connection.execute(
        f"DELETE FROM queue_items WHERE operation_id IN ({placeholders})",
        tuple(operation_ids),
    )
