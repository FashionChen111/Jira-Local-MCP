"""SQLite row codecs for immutable operation snapshots."""

from __future__ import annotations

import sqlite3
from typing import Any

from pydantic import TypeAdapter

from core.operations.models import (
    AttachmentResult,
    ExecutionRecord,
    OperationRecord,
    StagingRecord,
)
from core.storage.database import canonical_json, parse_json


OPERATION_COLUMNS = (
    "operation_id,operation_type,issue_key,target_issue_key,risk_level,"
    "payload_json,description,source,created_at,attempt_no"
)
DatabaseConnection = sqlite3.Connection


def payload_from_row(row: sqlite3.Row) -> dict[str, Any]:
    """Decode one persisted operation payload object."""

    payload = parse_json(row["payload_json"])
    if not isinstance(payload, dict):
        raise ValueError("payload_json 必须是 JSON 对象")
    return payload


_ATTACHMENT_RESULTS = TypeAdapter(tuple[AttachmentResult, ...])


def attachment_results_from_json(value: str) -> tuple[AttachmentResult, ...]:
    """Decode a strict collection of attachment outcomes."""

    return _ATTACHMENT_RESULTS.validate_json(value)


def operation_from_row(row: sqlite3.Row) -> OperationRecord:
    """Decode and strictly validate a self-contained operation row."""

    return OperationRecord.model_validate(
        {
            "operation_id": row["operation_id"],
            "operation_type": row["operation_type"],
            "issue_key": row["issue_key"],
            "target_issue_key": row["target_issue_key"],
            "risk_level": row["risk_level"],
            "payload": payload_from_row(row),
            "description": row["description"],
            "source": row["source"],
            "created_at": row["created_at"],
            "attempt_no": row["attempt_no"],
        }
    )


def staging_from_row(row: sqlite3.Row) -> StagingRecord:
    """Decode and strictly validate an active staging snapshot."""

    data = operation_from_row(row).model_dump(mode="python")
    data.update(
        stage_order=row["stage_order"],
        state=row["state"],
        submission_risk_level=row["submission_risk_level"],
        execution_id=row["execution_id"],
        submitted_at=row["submitted_at"],
        started_at=row["started_at"],
        held_reason=row["held_reason"],
        blocked_by_execution_id=row["blocked_by_execution_id"],
    )
    return StagingRecord.model_validate(data)


def execution_from_row(row: sqlite3.Row) -> ExecutionRecord:
    """Decode and strictly validate a historical execution snapshot."""

    data = operation_from_row(row).model_dump(mode="python")
    data.update(
        execution_id=row["execution_id"],
        submission_risk_level=row["submission_risk_level"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        outcome=row["outcome"],
        summary=row["summary"],
        error=row["error"],
        new_issue_key=row["new_issue_key"],
        attachment_results=attachment_results_from_json(
            row["attachment_results_json"]
        ),
        elapsed_ms=row["elapsed_ms"],
        resolution=row["resolution"],
        resolved_at=row["resolved_at"],
    )
    return ExecutionRecord.model_validate(data)


def operation_values(operation: OperationRecord) -> tuple[Any, ...]:
    """Return the common operation columns in persisted order."""

    return (
        operation.operation_id,
        operation.operation_type,
        operation.issue_key,
        operation.target_issue_key,
        operation.risk_level,
        canonical_json(operation.payload),
        operation.description,
        operation.source,
        operation.created_at,
        operation.attempt_no,
    )


def operation_snapshot(operation: OperationRecord) -> dict[str, Any]:
    """Serialize one event snapshot through the domain model."""

    return operation.model_dump(mode="json")
