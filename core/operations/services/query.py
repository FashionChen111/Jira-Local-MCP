"""Read-only operations state, history, and execution-log service."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from core.operations.models import (
    ExecutionHistoryPage,
    ExecutionRecord,
    ExecutionRecordPublic,
    ExecutionStatePublic,
    OperationRecord,
    OperationsStatePublic,
    ProducerState,
    QueueGroupPublic,
    QueueOperationPublic,
    QueueStatePublic,
    StagingOperationPublic,
    StagingRecord,
    StagingState,
)
from core.operations.repository import OperationRepository
from core.operations.rules.dependencies import (
    dependency_operation_ids,
    dependency_state_map,
    submission_block_reason,
)
from core.operations.rules.ordering import automatic_queue
from core.storage.database import open_database


def _operation_data(
    operation: OperationRecord,
    producer_states: Mapping[str, ProducerState],
) -> dict[str, object]:
    return {
        **operation.model_dump(mode="python"),
        "dependency_operation_ids": dependency_operation_ids(operation),
        "dependency_states": dependency_state_map(operation, producer_states),
    }


def _queue_operation_public(
    operation: OperationRecord,
    producer_states: Mapping[str, ProducerState],
) -> QueueOperationPublic:
    reason = submission_block_reason(operation, producer_states)
    return QueueOperationPublic.model_validate(
        {
            **_operation_data(operation, producer_states),
            "submittable": reason is None,
            "submission_block_reason": reason,
        }
    )


def _staging_operation_public(
    staging: StagingRecord,
    producer_states: Mapping[str, ProducerState],
) -> StagingOperationPublic:
    data = _operation_data(staging.as_operation(), producer_states)
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
        data[key] = getattr(staging, key)
    return StagingOperationPublic.model_validate(data)


def _execution_record_public(
    record: ExecutionRecord,
    producer_states: Mapping[str, ProducerState],
) -> ExecutionRecordPublic:
    data = _operation_data(record.as_operation(), producer_states)
    for key in (
        "execution_id",
        "submission_risk_level",
        "started_at",
        "finished_at",
        "outcome",
        "summary",
        "error",
        "new_issue_key",
        "elapsed_ms",
        "resolution",
        "resolved_at",
    ):
        data[key] = getattr(record, key)
    return ExecutionRecordPublic.model_validate(data)


class QueryService:
    """Own all read models while repository results stay strictly typed."""

    def __init__(self, repository: OperationRepository) -> None:
        self._repository = repository

    @staticmethod
    def _dependency_ids(records: Iterable[OperationRecord]) -> set[str]:
        return {
            dependency_id
            for record in records
            for dependency_id in dependency_operation_ids(record)
        }

    def get_state(self) -> OperationsStatePublic:
        """Return one consistent queue/execution snapshot and its revisions."""

        connection = open_database("operations")
        try:
            queue_records = self._repository.queue_operations(connection)
            staging_records = self._repository.staging_records(connection)
            producer_ids = self._dependency_ids(queue_records)
            producer_ids.update(self._dependency_ids(staging_records))
            producer_ids.update(
                operation.operation_id
                for operation in queue_records
                if operation.operation_type in {"create_issue", "clone_issue"}
            )
            producer_states = self._repository.producer_states(
                connection, producer_ids
            )
            layout = automatic_queue(queue_records, producer_states)
            queue = QueueStatePublic(
                groups=tuple(
                    QueueGroupPublic(
                        group_key=group.group_key,
                        kind=group.kind,
                        producer_state=(
                            producer_states.get(
                                group.group_key,
                                ProducerState(
                                    operation_id=group.group_key,
                                    operation_type=None,
                                    state="unknown",
                                ),
                            ).state
                            if group.kind == "pending_issue"
                            else None
                        ),
                        operations=tuple(
                            _queue_operation_public(operation, producer_states)
                            for operation in group.operations
                        ),
                    )
                    for group in layout.groups
                ),
                standalone=tuple(
                    _queue_operation_public(operation, producer_states)
                    for operation in layout.standalone
                ),
            )
            execution_parts: dict[StagingState, list[StagingOperationPublic]] = {
                "staged": [],
                "executing": [],
                "indeterminate": [],
                "held": [],
            }
            for staging in staging_records:
                execution_parts[staging.state].append(
                    _staging_operation_public(staging, producer_states)
                )
            execution = ExecutionStatePublic(
                staged=tuple(execution_parts["staged"]),
                executing=tuple(execution_parts["executing"]),
                indeterminate=tuple(execution_parts["indeterminate"]),
                held=tuple(execution_parts["held"]),
            )
            return OperationsStatePublic(
                revisions=self._repository.revisions(connection),
                queue=queue,
                execution=execution,
            )
        finally:
            connection.close()

    def execution_history(
        self,
        start_at: int | None,
        end_at: int | None,
        page: int = 1,
        page_size: int = 50,
    ) -> ExecutionHistoryPage:
        """Return a validated paginated execution history."""

        if page < 1 or not 1 <= page_size <= 200:
            raise ValueError("page 必须大于 0，page_size 必须在 1 至 200 之间")
        connection = open_database("operations")
        try:
            records, total = self._repository.execution_history(
                connection,
                start_at=start_at,
                end_at=end_at,
                page=page,
                page_size=page_size,
            )
            states = self._repository.producer_states(
                connection, self._dependency_ids(records)
            )
            return ExecutionHistoryPage(
                items=tuple(
                    _execution_record_public(record, states) for record in records
                ),
                total=total,
                page=page,
                page_size=page_size,
            )
        finally:
            connection.close()

    def read_execution_log(self, limit: int) -> list[dict[str, object]]:
        """Return recent completed execution records for the MCP resource."""

        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 至 1000 之间")
        connection = open_database("operations")
        try:
            records = self._repository.execution_log(connection, limit)
            states = self._repository.producer_states(
                connection, self._dependency_ids(records)
            )
            return [
                _execution_record_public(record, states).model_dump(mode="json")
                for record in records
            ]
        finally:
            connection.close()

    def total_active(self) -> int:
        """Return the number of queue and staging rows."""

        connection = open_database("operations")
        try:
            return self._repository.total_active(connection)
        finally:
            connection.close()
