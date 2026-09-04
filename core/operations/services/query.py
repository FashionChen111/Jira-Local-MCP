"""Read-only operations state, history, and execution-log service."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from core.operations.dependencies import orchestration as dependency_orchestration
from core.operations.models import (
    DependencyState,
    ExecutionHistoryPage,
    ExecutionRecord,
    ExecutionRecordPublic,
    ExecutionStatePublic,
    OperationRecord,
    OperationsStatePublic,
    QueueGroupPublic,
    QueueOperationPublic,
    QueueStatePublic,
    StagingOperationPublic,
    StagingRecord,
    StagingState,
)
from core.operations.repository import dependencies as dependency_repository
from core.operations.repository import execution as execution_repository
from core.operations.repository import metadata as repository_metadata
from core.operations.repository import queue as queue_repository
from core.operations.rules.ordering import automatic_queue
from core.operations.rules.safety import submission_problem
from core.storage.database import open_database


def _operation_data(
    operation: OperationRecord,
    dependency_states: Mapping[str, DependencyState],
    referenced_operation_ids: tuple[str, ...],
) -> dict[str, object]:
    return {
        **operation.model_dump(mode="python"),
        "dependency_operation_ids": referenced_operation_ids,
        "dependency_states": {
            referenced_operation_id: (
                state.state
                if (
                    state := dependency_states.get(referenced_operation_id)
                ) is not None
                else "unknown"
            )
            for referenced_operation_id in referenced_operation_ids
        },
    }


def _queue_operation_public(
    operation: OperationRecord,
    dependency_states: Mapping[str, DependencyState],
    inspection: dependency_orchestration.DependencyInspection,
    referenced_operation_ids: tuple[str, ...],
) -> QueueOperationPublic:
    reason = submission_problem(operation, inspection, dependency_states)
    return QueueOperationPublic.model_validate(
        {
            **_operation_data(
                operation,
                dependency_states,
                referenced_operation_ids,
            ),
            "submittable": reason is None,
            "submission_block_reason": reason,
        }
    )


def _staging_operation_public(
    staging: StagingRecord,
    dependency_states: Mapping[str, DependencyState],
    referenced_operation_ids: tuple[str, ...],
) -> StagingOperationPublic:
    data = _operation_data(
        staging.as_operation(),
        dependency_states,
        referenced_operation_ids,
    )
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
    dependency_states: Mapping[str, DependencyState],
    referenced_operation_ids: tuple[str, ...],
) -> ExecutionRecordPublic:
    data = _operation_data(
        record.as_operation(),
        dependency_states,
        referenced_operation_ids,
    )
    for key in (
        "execution_id",
        "submission_risk_level",
        "started_at",
        "finished_at",
        "outcome",
        "summary",
        "error",
        "new_issue_key",
        "attachment_result",
        "elapsed_ms",
        "resolution",
        "resolved_at",
    ):
        data[key] = getattr(record, key)
    return ExecutionRecordPublic.model_validate(data)


class QueryService:
    """Own all read models while repository results stay strictly typed."""

    @staticmethod
    def _inspect_records(
        records: Iterable[OperationRecord],
    ) -> tuple[
        dict[str, dependency_orchestration.DependencyInspection],
        dict[str, tuple[str, ...]],
    ]:
        inspections: dict[
            str, dependency_orchestration.DependencyInspection
        ] = {}
        referenced_ids: dict[str, tuple[str, ...]] = {}
        for record in records:
            inspection = dependency_orchestration.inspect_dependencies(record)
            inspections[record.operation_id] = inspection
            referenced_ids[record.operation_id] = (
                dependency_orchestration.collect_referenced_operation_ids(
                    inspection
                )
            )
        return inspections, referenced_ids

    def get_state(self) -> OperationsStatePublic:
        """Return one consistent queue/execution snapshot and its revisions."""

        connection = open_database("operations")
        try:
            queue_records = queue_repository.queue_operations(connection)
            staging_records = execution_repository.staging_records(connection)
            inspection_by_operation_id, referenced_ids_by_operation_id = (
                self._inspect_records((*queue_records, *staging_records))
            )
            producer_ids = dependency_orchestration.merge_referenced_operation_ids(
                referenced_ids_by_operation_id.values()
            )
            producer_ids.update(
                group_id
                for operation in queue_records
                if (
                    group_id := dependency_orchestration.pending_issue_group_id(
                        operation
                    )
                )
                is not None
            )
            dependency_states = dependency_repository.dependency_states(
                connection, producer_ids
            )
            layout = automatic_queue(queue_records, dependency_states)
            queue = QueueStatePublic(
                groups=tuple(
                    QueueGroupPublic(
                        group_key=group.group_key,
                        kind=group.kind,
                        producer_state=(
                            (
                                state.state
                                if (
                                    state := dependency_states.get(
                                        group.group_key
                                    )
                                )
                                is not None
                                else "unknown"
                            )
                            if group.kind == "pending_issue"
                            else None
                        ),
                        operations=tuple(
                            _queue_operation_public(
                                operation,
                                dependency_states,
                                inspection_by_operation_id[
                                    operation.operation_id
                                ],
                                referenced_ids_by_operation_id[
                                    operation.operation_id
                                ],
                            )
                            for operation in group.operations
                        ),
                    )
                    for group in layout.groups
                ),
                standalone=tuple(
                    _queue_operation_public(
                        operation,
                        dependency_states,
                        inspection_by_operation_id[operation.operation_id],
                        referenced_ids_by_operation_id[operation.operation_id],
                    )
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
                    _staging_operation_public(
                        staging,
                        dependency_states,
                        referenced_ids_by_operation_id[staging.operation_id],
                    )
                )
            execution = ExecutionStatePublic(
                staged=tuple(execution_parts["staged"]),
                executing=tuple(execution_parts["executing"]),
                indeterminate=tuple(execution_parts["indeterminate"]),
                held=tuple(execution_parts["held"]),
            )
            return OperationsStatePublic(
                revisions=repository_metadata.revisions(connection),
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
            records, total = execution_repository.execution_history(
                connection,
                start_at=start_at,
                end_at=end_at,
                page=page,
                page_size=page_size,
            )
            _, referenced_ids_by_operation_id = self._inspect_records(records)
            states = dependency_repository.dependency_states(
                connection,
                dependency_orchestration.merge_referenced_operation_ids(
                    referenced_ids_by_operation_id.values()
                ),
            )
            return ExecutionHistoryPage(
                items=tuple(
                    _execution_record_public(
                        record,
                        states,
                        referenced_ids_by_operation_id[record.operation_id],
                    )
                    for record in records
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
            records = execution_repository.execution_log(connection, limit)
            _, referenced_ids_by_operation_id = self._inspect_records(records)
            states = dependency_repository.dependency_states(
                connection,
                dependency_orchestration.merge_referenced_operation_ids(
                    referenced_ids_by_operation_id.values()
                ),
            )
            return [
                _execution_record_public(
                    record,
                    states,
                    referenced_ids_by_operation_id[record.operation_id],
                ).model_dump(mode="json")
                for record in records
            ]
        finally:
            connection.close()

    def total_active(self) -> int:
        """Return the number of queue and staging rows."""

        connection = open_database("operations")
        try:
            return repository_metadata.total_active(connection)
        finally:
            connection.close()
