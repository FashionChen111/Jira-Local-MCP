"""Stable facade for operation queue and execution persistence."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from core.operations.execution_store import ExecutionStore
from core.operations.models import ExecutionClaim, OperationRecord
from core.operations.persistence import get_revisions
from core.operations.queue_store import QueueStore
from core.storage.database import open_database


class OperationStore:
    """SQLite is the sole business-state source; this object holds no state cache."""

    def __init__(self) -> None:
        self._queue = QueueStore()
        self._execution = ExecutionStore(self._queue)

    def initialize(self) -> None:
        # MCP protocol logging is separate and best-effort, so its database must
        # never gate business-state availability. It initializes on first log call.
        connection = open_database("operations")
        connection.close()

    def get_revisions(
        self, connection: sqlite3.Connection | None = None
    ) -> dict[str, int]:
        return get_revisions(connection)

    def get_state(self) -> dict[str, Any]:
        connection = open_database("operations")
        try:
            return {
                "queue": self._queue.read_state(connection),
                "execution": self._execution.read_state(connection),
                "revisions": get_revisions(connection),
            }
        finally:
            connection.close()

    def total_active(self) -> int:
        connection = open_database("operations")
        try:
            queue_count = connection.execute(
                "SELECT COUNT(*) FROM queue_items"
            ).fetchone()[0]
            staging_count = connection.execute(
                "SELECT COUNT(*) FROM execution_staging"
            ).fetchone()[0]
            return int(queue_count) + int(staging_count)
        finally:
            connection.close()

    def enqueue(
        self,
        operation_type: str,
        issue_key: str | None,
        payload: dict[str, Any],
        description: str,
        source: str = "",
    ) -> OperationRecord:
        return self._queue.enqueue(
            operation_type, issue_key, payload, description, source
        )

    def reorder(
        self,
        expected_queue_revision: int,
        groups: list[dict[str, Any]],
        standalone_operation_ids: list[str],
        planned_groups: list[dict[str, Any]] | None = None,
    ) -> int:
        return self._queue.reorder(
            expected_queue_revision,
            groups,
            standalone_operation_ids,
            planned_groups,
        )

    def cancel(
        self,
        expected_queue_revision: int,
        operation_ids: list[str],
        cascade_confirmed: bool,
    ) -> tuple[list[str], int]:
        return self._queue.cancel(
            expected_queue_revision, operation_ids, cascade_confirmed
        )

    def validate_submission(
        self,
        expected_queue_revision: int,
        operation_ids: list[str],
        high_risk_confirmed: bool,
    ) -> None:
        self._execution.validate_submission(
            expected_queue_revision, operation_ids, high_risk_confirmed
        )

    def submit(
        self,
        expected_queue_revision: int,
        operation_ids: list[str],
        high_risk_confirmed: bool,
    ) -> tuple[list[str], dict[str, int]]:
        return self._execution.submit(
            expected_queue_revision, operation_ids, high_risk_confirmed
        )

    def claim_next(self) -> ExecutionClaim | None:
        return self._execution.claim_next()

    def complete_execution(
        self,
        execution_id: str,
        outcome: Literal["success", "failed"],
        summary: str | None,
        error: str | None,
        new_issue_key: str | None,
        elapsed_ms: int,
    ) -> None:
        self._execution.complete_execution(
            execution_id,
            outcome,
            summary,
            error,
            new_issue_key,
            elapsed_ms,
        )

    def recover_interrupted(self) -> int:
        return self._execution.recover_interrupted()

    def resolve_indeterminate(
        self,
        operation_id: str,
        resolution: str,
        high_risk_confirmed: bool,
        new_issue_key: str | None = None,
    ) -> dict[str, Any]:
        return self._execution.resolve_indeterminate(
            operation_id, resolution, high_risk_confirmed, new_issue_key
        )

    def resolve_held(
        self, operation_ids: list[str], action: str
    ) -> dict[str, Any]:
        return self._execution.resolve_held(operation_ids, action)

    def execution_history(
        self,
        start_at: int | None,
        end_at: int | None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        return self._execution.execution_history(
            start_at, end_at, page, page_size
        )

    def read_execution_log(self, limit: int) -> list[dict[str, Any]]:
        return self._execution.read_execution_log(limit)


_store: OperationStore | None = None


def get_store() -> OperationStore:
    global _store
    if _store is None:
        _store = OperationStore()
        _store.initialize()
    return _store


def reset_store() -> None:
    global _store
    _store = None
