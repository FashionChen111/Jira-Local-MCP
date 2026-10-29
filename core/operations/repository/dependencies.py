"""Cross-table dependency state, materialization, and failure persistence."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from typing import Literal

from typing_extensions import assert_never

from core.operations.dependencies import orchestration as dependency_orchestration
from core.operations.models import (
    AttachmentDependencyResolution,
    DependencyBindingResult,
    DependencyResolution,
    DependencyState,
    IssueDependencyResolution,
    OperationRecord,
    StagingRecord,
)
from core.operations.repository import execution as execution_repository
from core.operations.repository import metadata as repository_metadata
from core.operations.repository import queue as queue_repository
from core.operations.repository._records import attachment_results_from_json
from core.operations.rules.validation import is_jira_issue_key
from core.storage.database import canonical_json, new_id, utc_ms


_ConsumerTable = Literal["queue_items", "execution_staging"]
_ConsumerLocation = Literal["queue", "staging"]


def dependency_states(
    connection: sqlite3.Connection,
    operation_ids: Iterable[str],
) -> dict[str, DependencyState]:
    """Batch-load lifecycle facts with active rows taking precedence."""

    requested = tuple(dict.fromkeys(operation_ids))
    if not requested:
        return {}
    result: dict[str, DependencyState] = {}
    for chunk_start in range(0, len(requested), 400):
        chunk = requested[chunk_start : chunk_start + 400]
        placeholders = ",".join("?" for _ in chunk)
        queue_rows = connection.execute(
            f"""SELECT operation_id,operation_type,issue_key
                FROM queue_items WHERE operation_id IN ({placeholders})""",
            chunk,
        ).fetchall()
        for row in queue_rows:
            result[row["operation_id"]] = DependencyState(
                operation_id=row["operation_id"],
                operation_type=row["operation_type"],
                state="queued",
                operation_issue_key=row["issue_key"],
            )

        remaining = tuple(item for item in chunk if item not in result)
        if remaining:
            marks = ",".join("?" for _ in remaining)
            staging_rows = connection.execute(
                f"""SELECT s.operation_id,s.operation_type,s.issue_key,s.state,
                           r.outcome,r.resolution,r.new_issue_key,
                           r.attachment_results_json
                    FROM execution_staging AS s
                    LEFT JOIN execution_records AS r ON r.execution_id=s.execution_id
                    WHERE s.operation_id IN ({marks})""",
                remaining,
            ).fetchall()
            for row in staging_rows:
                if row["outcome"] is not None:
                    result[row["operation_id"]] = _dependency_state_from_result(
                        row["operation_id"], row
                    )
                else:
                    result[row["operation_id"]] = DependencyState(
                        operation_id=row["operation_id"],
                        operation_type=row["operation_type"],
                        state=row["state"],
                        operation_issue_key=row["issue_key"],
                    )

        remaining = tuple(item for item in chunk if item not in result)
        if remaining:
            marks = ",".join("?" for _ in remaining)
            history_rows = connection.execute(
                f"""SELECT operation_id,operation_type,issue_key,outcome,resolution,
                           new_issue_key,attachment_results_json,
                           attempt_no,started_at,execution_id
                    FROM execution_records WHERE operation_id IN ({marks})
                    ORDER BY attempt_no DESC,started_at DESC,execution_id DESC""",
                remaining,
            ).fetchall()
            for row in history_rows:
                if row["operation_id"] not in result:
                    result[row["operation_id"]] = _dependency_state_from_result(
                        row["operation_id"], row
                    )

    for operation_id in requested:
        result.setdefault(
            operation_id,
            DependencyState(
                operation_id=operation_id,
                operation_type=None,
                state="unknown",
            ),
        )
    return result


def _dependency_state_from_result(
    operation_id: str, row: sqlite3.Row
) -> DependencyState:
    outcome = row["outcome"]
    resolution = row["resolution"]
    if outcome == "success" or (
        outcome == "indeterminate" and resolution == "succeeded"
    ):
        state = "success"
    elif outcome == "indeterminate" and resolution == "not_applied":
        state = "retry_pending"
    elif outcome == "indeterminate" and resolution == "abandoned":
        state = "abandoned"
    elif outcome == "indeterminate":
        state = "indeterminate"
    else:
        state = outcome or "executing"
    return DependencyState(
        operation_id=operation_id,
        operation_type=row["operation_type"],
        state=state,
        operation_issue_key=row["issue_key"],
        new_issue_key=row["new_issue_key"],
        attachment_results=attachment_results_from_json(
            row["attachment_results_json"]
        ),
    )


def broadcast_dependency_resolution(
    connection: sqlite3.Connection,
    resolution: DependencyResolution,
) -> DependencyBindingResult:
    """Materialize one typed result across active queue and staging consumers."""

    if isinstance(resolution, IssueDependencyResolution):
        return _broadcast_issue_resolution(connection, resolution)
    if isinstance(resolution, AttachmentDependencyResolution):
        return _broadcast_attachment_resolution(connection, resolution)
    assert_never(resolution)


def _broadcast_issue_resolution(
    connection: sqlite3.Connection,
    resolution: IssueDependencyResolution,
) -> DependencyBindingResult:
    if not is_jira_issue_key(resolution.issue_key):
        raise ValueError("生产者结果必须包含合法 Jira Key")
    queue_operation_ids = _materialize_issue_consumers(
        connection,
        "queue_items",
        "queue",
        resolution,
    )
    staging_operation_ids = _materialize_issue_consumers(
        connection,
        "execution_staging",
        "staging",
        resolution,
    )
    return DependencyBindingResult(
        queue_operation_ids=queue_operation_ids,
        staging_operation_ids=staging_operation_ids,
    )


def _broadcast_attachment_resolution(
    connection: sqlite3.Connection,
    resolution: AttachmentDependencyResolution,
) -> DependencyBindingResult:
    queue_operation_ids = _materialize_attachment_consumers(
        connection,
        "queue_items",
        "queue",
        resolution,
    )
    staging_operation_ids = _materialize_attachment_consumers(
        connection,
        "execution_staging",
        "staging",
        resolution,
    )
    return DependencyBindingResult(
        queue_operation_ids=queue_operation_ids,
        staging_operation_ids=staging_operation_ids,
    )


def _materialize_issue_consumers(
    connection: sqlite3.Connection,
    table: _ConsumerTable,
    location: _ConsumerLocation,
    resolution: IssueDependencyResolution,
) -> tuple[str, ...]:
    rows = connection.execute(
        f"""SELECT operation_id,operation_type,issue_key,target_issue_key
            FROM {table}
            WHERE issue_key=? OR (
                operation_type='add_issuelink' AND target_issue_key=?
            ) ORDER BY created_at,operation_id""",
        (resolution.producer_operation_id, resolution.producer_operation_id),
    ).fetchall()
    changed_operation_ids: list[str] = []
    for row in rows:
        fields: list[str] = []
        if row["issue_key"] == resolution.producer_operation_id:
            fields.append("issue_key")
        if (
            row["operation_type"] == "add_issuelink"
            and row["target_issue_key"] == resolution.producer_operation_id
        ):
            fields.append("target_issue_key")
        event = dependency_orchestration.DependencyResolutionEvent(
            resolution=resolution,
            reference_paths=tuple(fields),
        )
        changed_operation_ids.append(row["operation_id"])
        repository_metadata.append_event(
            connection,
            row["operation_id"],
            "internal",
            event.action_code,
            {
                "producer_operation_id": event.producer_operation_id,
                **event.data,
                "location": location,
            },
        )

    if changed_operation_ids:
        connection.execute(
            f"UPDATE {table} SET issue_key=? WHERE issue_key=?",
            (resolution.issue_key, resolution.producer_operation_id),
        )
        connection.execute(
            f"""UPDATE {table} SET target_issue_key=?
                WHERE operation_type='add_issuelink' AND target_issue_key=?""",
            (resolution.issue_key, resolution.producer_operation_id),
        )
    return tuple(changed_operation_ids)


def _materialize_attachment_consumers(
    connection: sqlite3.Connection,
    table: _ConsumerTable,
    location: _ConsumerLocation,
    resolution: AttachmentDependencyResolution,
) -> tuple[str, ...]:
    changed_operation_ids: list[str] = []
    records: Sequence[OperationRecord]
    if table == "queue_items":
        records = queue_repository.queue_operations(connection)
    else:
        records = execution_repository.staging_records(connection)
    for record in records:
        materialization = dependency_orchestration.materialize_references(
            record,
            (resolution,),
        )
        if not materialization.events:
            continue
        event = materialization.events[0]
        connection.execute(
            f"UPDATE {table} SET payload_json=? WHERE operation_id=?",
            (
                canonical_json(materialization.operation.payload),
                record.operation_id,
            ),
        )
        changed_operation_ids.append(record.operation_id)
        repository_metadata.append_event(
            connection,
            record.operation_id,
            "internal",
            event.action_code,
            {
                "producer_operation_id": event.producer_operation_id,
                **event.data,
                "location": location,
            },
        )
    return tuple(changed_operation_ids)


def terminalize_dependency_failure(
    connection: sqlite3.Connection,
    staging: StagingRecord,
    *,
    producer_operation_id: str,
) -> None:
    """Persist one dependency failure and remove its active staging row."""

    execution_id = new_id("exec")
    occurred_at = utc_ms()
    summary = (
        "未调用 Jira：依赖 operation 未产生可用结果 "
        f"({producer_operation_id})"
    )
    connection.execute(
        """INSERT INTO execution_records(
          execution_id,operation_id,attempt_no,operation_type,issue_key,
          target_issue_key,
          risk_level,submission_risk_level,payload_json,description,source,created_at,
          started_at,finished_at,outcome,summary,error,new_issue_key,elapsed_ms
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'dependency_failed',?,?,NULL,0)""",
        (
            execution_id,
            staging.operation_id,
            staging.attempt_no,
            staging.operation_type,
            staging.issue_key,
            staging.target_issue_key,
            staging.risk_level,
            staging.submission_risk_level,
            canonical_json(staging.payload),
            staging.description,
            staging.source,
            staging.created_at,
            occurred_at,
            occurred_at,
            summary,
            f"dependency_failed: {producer_operation_id}",
        ),
    )
    repository_metadata.append_event(
        connection,
        staging.operation_id,
        "internal",
        "dependency_failed",
        {"producer_operation_id": producer_operation_id},
    )
    execution_repository.delete_staging_by_operation(
        connection, staging.operation_id
    )


def terminalize_dependants(
    connection: sqlite3.Connection, producer_operation_id: str
) -> int:
    """Terminalize staged or held operations referencing one producer."""

    affected = 0
    for staging in execution_repository.staging_records(
        connection, {"staged", "held"}
    ):
        inspection = dependency_orchestration.inspect_dependencies(staging)
        referenced_operation_ids = (
            dependency_orchestration.collect_referenced_operation_ids(inspection)
        )
        if producer_operation_id not in referenced_operation_ids:
            continue
        terminalize_dependency_failure(
            connection,
            staging,
            producer_operation_id=producer_operation_id,
        )
        affected += 1
    return affected
