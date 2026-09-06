"""Queued operation persistence and atomic enqueue event writes."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from core.operations.models import OperationRecord
from core.operations.repository.metadata import append_event
from core.operations.repository._records import (
    OPERATION_COLUMNS,
    operation_from_row,
    operation_snapshot,
    operation_values,
)


def queue_operations(connection: sqlite3.Connection) -> list[OperationRecord]:
    """Load every queued immutable operation snapshot."""

    rows = connection.execute(
        f"""SELECT {OPERATION_COLUMNS} FROM queue_items
            ORDER BY created_at,operation_id"""
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


def queue_operations_by_ids(
    connection: sqlite3.Connection, operation_ids: Sequence[str],
) -> list[OperationRecord]:
    """Load only requested queued snapshots; callers determine result order."""
    records: list[OperationRecord] = []
    requested = tuple(dict.fromkeys(operation_ids))
    for start in range(0, len(requested), 400):
        chunk = requested[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT {OPERATION_COLUMNS} FROM queue_items "
            f"WHERE operation_id IN ({placeholders})",
            chunk,
        ).fetchall()
        records.extend(operation_from_row(row) for row in rows)
    return records


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
