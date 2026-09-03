"""Revision, event, and aggregate operations metadata persistence."""

from __future__ import annotations

import sqlite3
from typing import Literal

from core.storage.database import canonical_json, new_id, utc_ms


def revision(connection: sqlite3.Connection, name: str) -> int:
    """Return one optimistic-concurrency revision."""

    row = connection.execute(
        "SELECT value FROM revisions WHERE name=?", (name,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"缺少 revision：{name}")
    return int(row[0])


def revisions(connection: sqlite3.Connection) -> dict[str, int]:
    """Return all optimistic-concurrency revisions."""

    rows = connection.execute("SELECT name,value FROM revisions").fetchall()
    return {row["name"]: int(row["value"]) for row in rows}


def bump_revisions(connection: sqlite3.Connection, *names: str) -> None:
    """Increment each named revision once inside the caller transaction."""

    for name in set(names):
        connection.execute(
            "UPDATE revisions SET value=value+1 WHERE name=?", (name,)
        )


def append_event(
    connection: sqlite3.Connection,
    operation_id: str,
    event_type: Literal["enqueue", "dequeue", "internal"],
    action_code: str,
    data: dict[str, object],
) -> None:
    """Append one operation audit event inside the caller transaction."""

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


def total_active(connection: sqlite3.Connection) -> int:
    """Return the combined queue and staging row count."""

    queued = int(
        connection.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0]
    )
    staged = int(
        connection.execute(
            "SELECT COUNT(*) FROM execution_staging"
        ).fetchone()[0]
    )
    return queued + staged


def has_recovery_block(connection: sqlite3.Connection) -> bool:
    """Return whether indeterminate or held work blocks submission."""

    return (
        connection.execute(
            """SELECT 1 FROM execution_staging
               WHERE state IN ('indeterminate','held') LIMIT 1"""
        ).fetchone()
        is not None
    )
