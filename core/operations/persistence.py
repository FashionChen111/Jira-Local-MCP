"""Operation-specific SQLite row, revision, and event helpers."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from core.operations.models import OperationRecord
from core.storage.database import (
    canonical_json,
    new_id,
    open_database,
    parse_json,
    utc_ms,
)


def operation_from_row(row: sqlite3.Row) -> OperationRecord:
    return OperationRecord(
        operation_id=row["operation_id"],
        operation_type=row["operation_type"],
        issue_key=row["issue_key"],
        risk_level=row["risk_level"],
        payload=parse_json(row["payload_json"]),  # type: ignore[arg-type]
        description=row["description"],
        source=row["source"],
        created_at=row["created_at"],
        attempt_no=row["attempt_no"],
    )


def operation_values(operation: OperationRecord) -> tuple[Any, ...]:
    return (
        operation.operation_id,
        operation.operation_type,
        operation.issue_key,
        operation.risk_level,
        canonical_json(operation.payload),
        operation.description,
        operation.source,
        operation.created_at,
        operation.attempt_no,
    )


def queue_public(row: sqlite3.Row) -> dict[str, Any]:
    data = operation_from_row(row).to_public_dict()
    data.update(
        layout_kind=row["layout_kind"],
        category=row["category"],
        position=row["position"],
        manual_after_operation_id=row["manual_after_operation_id"],
    )
    return data


def staging_public(row: sqlite3.Row) -> dict[str, Any]:
    data = operation_from_row(row).to_public_dict()
    for key in (
        "stage_order",
        "state",
        "submission_risk_level",
        "execution_id",
        "submitted_at",
        "started_at",
        "held_reason",
        "blocked_by_execution_id",
    ):
        data[key] = row[key]
    return data


def execution_record_public(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = parse_json(item.pop("payload_json"))
    return item


def bump_revisions(connection: sqlite3.Connection, *names: str) -> None:
    for name in set(names):
        connection.execute("UPDATE revisions SET value=value+1 WHERE name=?", (name,))


def revision(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(
        "SELECT value FROM revisions WHERE name=?", (name,)
    ).fetchone()
    return int(row[0])


def get_revisions(
    connection: sqlite3.Connection | None = None,
) -> dict[str, int]:
    own = connection is None
    connection = connection or open_database("operations")
    try:
        rows = connection.execute("SELECT name,value FROM revisions").fetchall()
        return {row["name"]: row["value"] for row in rows}
    finally:
        if own:
            connection.close()


def append_event(
    connection: sqlite3.Connection,
    operation_id: str,
    event_type: Literal["enqueue", "dequeue", "internal"],
    action_code: str,
    data: dict[str, Any],
) -> None:
    connection.execute(
        "INSERT INTO queue_events VALUES (?,?,?,?,?,?)",
        (
            new_id("event"),
            utc_ms(),
            operation_id,
            event_type,
            action_code,
            canonical_json(data),
        ),
    )
