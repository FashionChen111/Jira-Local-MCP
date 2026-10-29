"""Public staging and execution-history persistence capabilities."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from core.operations.models import (
    AttachmentResult,
    OperationExecutionOutcome,
    ExecutionRecord,
    OperationRecord,
    Resolution,
    RiskLevel,
    StagingRecord,
)
from core.operations.repository._records import (
    OPERATION_COLUMNS,
    execution_from_row,
    operation_values,
    staging_from_row,
)
from core.storage.database import canonical_json


def staging_records(
    connection: sqlite3.Connection,
    states: Iterable[str] | None = None,
) -> list[StagingRecord]:
    """Load active staging rows in their authorized order."""

    params: tuple[str, ...] = ()
    where = ""
    if states is not None:
        state_values = tuple(states)
        if not state_values:
            return []
        placeholders = ",".join("?" for _ in state_values)
        where = f" WHERE state IN ({placeholders})"
        params = state_values
    rows = connection.execute(
        f"SELECT * FROM execution_staging{where} ORDER BY stage_order", params
    ).fetchall()
    return [staging_from_row(row) for row in rows]


def staging_page(
    connection: sqlite3.Connection, *, page: int, page_size: int,
) -> tuple[list[StagingRecord], int]:
    """Load one authorized-order page and its total within the caller's snapshot."""
    total = connection.execute("SELECT COUNT(*) FROM execution_staging").fetchone()[0]
    rows = connection.execute(
        "SELECT * FROM execution_staging ORDER BY stage_order LIMIT ? OFFSET ?",
        (page_size, (page - 1) * page_size),
    ).fetchall()
    return [staging_from_row(row) for row in rows], total


def staging_record(
    connection: sqlite3.Connection, operation_id: str
) -> StagingRecord | None:
    """Load one active staging row by operation ID."""

    row = connection.execute(
        "SELECT * FROM execution_staging WHERE operation_id=?", (operation_id,)
    ).fetchone()
    return staging_from_row(row) if row is not None else None


def staging_records_by_ids(
    connection: sqlite3.Connection, operation_ids: Sequence[str]
) -> list[StagingRecord]:
    """Load selected staging rows in authorized order."""

    if not operation_ids:
        return []
    placeholders = ",".join("?" for _ in operation_ids)
    rows = connection.execute(
        f"""SELECT * FROM execution_staging
            WHERE operation_id IN ({placeholders}) ORDER BY stage_order""",
        tuple(operation_ids),
    ).fetchall()
    return [staging_from_row(row) for row in rows]


def maximum_stage_order(connection: sqlite3.Connection) -> int:
    """Return the current largest authorized stage order."""

    return int(
        connection.execute(
            "SELECT COALESCE(MAX(stage_order),0) FROM execution_staging"
        ).fetchone()[0]
    )


def insert_staging(
    connection: sqlite3.Connection,
    operation: OperationRecord,
    *,
    stage_order: int,
    submission_risk_level: RiskLevel,
    submitted_at: int,
) -> None:
    """Insert one reviewed operation into the authorized staging order."""

    connection.execute(
        f"""INSERT INTO execution_staging(
          {OPERATION_COLUMNS},stage_order,state,submission_risk_level,
          execution_id,submitted_at,started_at,held_reason,blocked_by_execution_id
        ) VALUES (?,?,?,?,?,?,?,?,?, ?,?,'staged',?,NULL,?,NULL,NULL,NULL)""",
        operation_values(operation)
        + (stage_order, submission_risk_level, submitted_at),
    )


def delete_staging_by_operation(
    connection: sqlite3.Connection, operation_id: str
) -> None:
    """Delete one active staging row by operation ID."""

    connection.execute(
        "DELETE FROM execution_staging WHERE operation_id=?", (operation_id,)
    )


def delete_staging_by_execution(
    connection: sqlite3.Connection, execution_id: str
) -> None:
    """Delete one active staging row by execution ID."""

    connection.execute(
        "DELETE FROM execution_staging WHERE execution_id=?", (execution_id,)
    )


def set_staging_state(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    state: str,
    execution_id: str | None = None,
    started_at: int | None = None,
    held_reason: str | None = None,
    blocked_by_execution_id: str | None = None,
) -> None:
    """Replace all lifecycle metadata for one staging row."""

    connection.execute(
        """UPDATE execution_staging
           SET state=?,execution_id=?,started_at=?,held_reason=?,
               blocked_by_execution_id=?
           WHERE operation_id=?""",
        (
            state,
            execution_id,
            started_at,
            held_reason,
            blocked_by_execution_id,
            operation_id,
        ),
    )


def mark_claimed(
    connection: sqlite3.Connection,
    operation_id: str,
    execution_id: str,
    started_at: int,
) -> bool:
    """Atomically transition one staged row to executing."""

    cursor = connection.execute(
        """UPDATE execution_staging SET state='executing',execution_id=?,started_at=?
           WHERE operation_id=? AND state='staged'""",
        (execution_id, started_at, operation_id),
    )
    return cursor.rowcount == 1


def insert_execution(
    connection: sqlite3.Connection,
    operation: OperationRecord,
    *,
    execution_id: str,
    submission_risk_level: RiskLevel,
    started_at: int,
) -> None:
    """Insert the historical execution snapshot for one atomic claim."""

    connection.execute(
        """INSERT INTO execution_records(
          execution_id,operation_id,attempt_no,operation_type,issue_key,
          target_issue_key,risk_level,submission_risk_level,payload_json,
          description,source,created_at,started_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            execution_id,
            operation.operation_id,
            operation.attempt_no,
            operation.operation_type,
            operation.issue_key,
            operation.target_issue_key,
            operation.risk_level,
            submission_risk_level,
            canonical_json(operation.payload),
            operation.description,
            operation.source,
            operation.created_at,
            started_at,
        ),
    )


def execution_record(
    connection: sqlite3.Connection, execution_id: str
) -> ExecutionRecord | None:
    """Load one historical execution record by execution ID."""

    row = connection.execute(
        "SELECT * FROM execution_records WHERE execution_id=?", (execution_id,)
    ).fetchone()
    return execution_from_row(row) if row is not None else None


def finish_execution(
    connection: sqlite3.Connection,
    execution_id: str,
    *,
    finished_at: int,
    outcome: OperationExecutionOutcome,
    summary: str | None,
    error: str | None,
    new_issue_key: str | None,
    attachment_results: tuple[AttachmentResult, ...],
    elapsed_ms: int,
) -> bool:
    """Atomically persist one previously unfinished worker result."""

    cursor = connection.execute(
        """UPDATE execution_records
           SET finished_at=?,outcome=?,summary=?,error=?,new_issue_key=?,
               attachment_results_json=?,elapsed_ms=?
           WHERE execution_id=? AND outcome IS NULL""",
        (
            finished_at,
            outcome,
            summary,
            error,
            new_issue_key,
            canonical_json([item.model_dump(mode="json") for item in attachment_results]),
            elapsed_ms,
            execution_id,
        ),
    )
    return cursor.rowcount == 1


def mark_execution_indeterminate(
    connection: sqlite3.Connection, execution_id: str, finished_at: int
) -> None:
    """Mark an unfinished execution as indeterminate after interruption."""

    connection.execute(
        """UPDATE execution_records SET outcome='indeterminate',finished_at=?
           WHERE execution_id=? AND outcome IS NULL""",
        (finished_at, execution_id),
    )


def hold_later_staging(
    connection: sqlite3.Connection,
    *,
    stage_order: int,
    blocked_by_execution_id: str,
) -> None:
    """Hold all later staged work behind an indeterminate execution."""

    connection.execute(
        """UPDATE execution_staging
           SET state='held',held_reason='blocked_by_indeterminate',
               blocked_by_execution_id=?
           WHERE stage_order>? AND state='staged'""",
        (blocked_by_execution_id, stage_order),
    )


def resolve_execution(
    connection: sqlite3.Connection,
    execution_id: str,
    *,
    resolution: Resolution,
    resolved_at: int,
    new_issue_key: str | None,
    attachment_results: tuple[AttachmentResult, ...],
) -> bool:
    """Atomically record one human conclusion for an indeterminate result."""

    attachment_json = canonical_json([item.model_dump(mode="json") for item in attachment_results])
    cursor = connection.execute(
        """UPDATE execution_records
           SET resolution=?,resolved_at=?,new_issue_key=COALESCE(?,new_issue_key),
               attachment_results_json=?
           WHERE execution_id=? AND outcome='indeterminate' AND resolution IS NULL""",
        (
            resolution,
            resolved_at,
            new_issue_key,
            attachment_json,
            execution_id,
        ),
    )
    return cursor.rowcount == 1


def execution_history(
    connection: sqlite3.Connection,
    *,
    start_at: int | None,
    end_at: int | None,
    page: int,
    page_size: int,
) -> tuple[list[ExecutionRecord], int]:
    """Load one filtered, paginated execution history page."""

    clauses = ["outcome IS NOT NULL"]
    params: list[Any] = []
    if start_at is not None:
        clauses.append("started_at>=?")
        params.append(start_at)
    if end_at is not None:
        clauses.append("started_at<?")
        params.append(end_at)
    where = " AND ".join(clauses)
    total = int(
        connection.execute(
            f"SELECT COUNT(*) FROM execution_records WHERE {where}", params
        ).fetchone()[0]
    )
    rows = connection.execute(
        f"""SELECT * FROM execution_records WHERE {where}
            ORDER BY started_at DESC,execution_id DESC LIMIT ? OFFSET ?""",
        params + [page_size, (page - 1) * page_size],
    ).fetchall()
    return [execution_from_row(row) for row in rows], total


def execution_log(
    connection: sqlite3.Connection, limit: int
) -> list[ExecutionRecord]:
    """Load recent terminal execution records for the MCP resource."""

    rows = connection.execute(
        """SELECT * FROM execution_records WHERE outcome IS NOT NULL
           ORDER BY started_at DESC,execution_id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [execution_from_row(row) for row in rows]
