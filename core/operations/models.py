"""Typed persisted operation-attempt and execution-claim models."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    operation_type: str
    issue_key: str | None
    risk_level: str
    payload: dict[str, Any]
    description: str
    source: str
    created_at: int
    attempt_no: int = 1

    def to_public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionClaim:
    """One atomically claimed execution and its persisted operation snapshot."""

    execution_id: str
    operation: OperationRecord
    submission_risk_level: str
    stage_order: int
    submitted_at: int
    started_at: int
