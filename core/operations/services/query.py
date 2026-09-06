"""Read-only operations state, history, and execution-log service."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from core.operations.dependencies import orchestration as dependency_orchestration
from core.operations.models import (
    DependencyState,
    ExecutionHistoryPage,
    ExecutionRecord,
    ExecutionRecordPublic,
    ExecutionStatePublic,
    OperationRecord,
    OperationOverview,
    StrictFrozenModel,
    RiskLevel,
    OperationsStatePublic,
    QueueGroupPublic,
    QueueOperationPublic,
    QueueStatePublic,
    StagingOperationPublic,
    StagingRecord,
    StagingState,
)
from core.operations.repository import DatabaseConnection
from core.operations.repository import dependencies as dependency_repository
from core.operations.repository import execution as execution_repository
from core.operations.repository import metadata as repository_metadata
from core.operations.repository import queue as queue_repository
from core.operations.rules.identifiers import validate_operation_ids
from core.operations.rules.ordering import automatic_queue
from core.operations.rules.safety import submission_problem
from core.storage.database import open_database


class ExecutionOperationOverview(OperationOverview):
    """Authorized execution metadata selected from a public staging record."""

    state: StagingState
    stage_order: int
    execution_id: str | None
    submission_risk_level: RiskLevel
    submitted_at: int
    started_at: int | None
    held_reason: str | None
    blocked_by_execution_id: str | None


class OperationsPageMetadata(StrictFrozenModel):
    """Pagination and revisions read from the same transaction as its items."""

    total: int
    page: int
    page_size: int
    revisions: dict[str, int]


class QueuePage(OperationsPageMetadata):
    """Queue overview records in review order."""

    items: tuple[OperationOverview, ...]


class ExecutionStatePage(OperationsPageMetadata):
    """Active execution overview records in authorized order."""

    items: tuple[ExecutionOperationOverview, ...]


class QueueOperationDetails(StrictFrozenModel):
    """Full public records and IDs absent from the same queue snapshot."""

    items: tuple[QueueOperationPublic, ...]
    missing_operation_ids: tuple[str, ...]
    revisions: dict[str, int]


@dataclass(frozen=True)
class _PreparedDependencies:
    """Dependency inspections and lifecycle facts for one local batch of records."""

    inspections: Mapping[str, dependency_orchestration.DependencyInspection]
    referenced_ids: Mapping[str, tuple[str, ...]]
    states: Mapping[str, DependencyState]


def _validate_page(page: int, page_size: int) -> None:
    if type(page) is not int or page < 1:
        raise ValueError("page 必须为正整数")
    if type(page_size) is not int or not 1 <= page_size <= 200:
        raise ValueError("page_size 必须为 1 至 200 的整数")


def _operation_data(
    operation: OperationRecord,
    dependency_states: Mapping[str, DependencyState],
    referenced_operation_ids: tuple[str, ...],
) -> dict[str, object]:
    return {
        **operation.model_dump(mode="python"),
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

    def _prepare_dependencies(
        self,
        connection: DatabaseConnection,
        records: Iterable[OperationRecord],
        group_ids: Iterable[str] = (),
    ) -> _PreparedDependencies:
        inspections, referenced_ids = self._inspect_records(records)
        producer_ids = dependency_orchestration.merge_referenced_operation_ids(
            referenced_ids.values()
        )
        producer_ids.update(group_ids)
        states = dependency_repository.dependency_states(connection, producer_ids)
        return _PreparedDependencies(inspections, referenced_ids, states)

    @staticmethod
    def _queue_records_public(
        records: Iterable[OperationRecord], dependencies: _PreparedDependencies,
    ) -> tuple[QueueOperationPublic, ...]:
        return tuple(
            _queue_operation_public(
                record, dependencies.states,
                dependencies.inspections[record.operation_id],
                dependencies.referenced_ids[record.operation_id],
            )
            for record in records
        )

    @staticmethod
    def _staging_records_public(
        records: Iterable[StagingRecord], dependencies: _PreparedDependencies,
    ) -> tuple[StagingOperationPublic, ...]:
        return tuple(
            _staging_operation_public(
                record, dependencies.states,
                dependencies.referenced_ids[record.operation_id],
            )
            for record in records
        )

    def get_state(self) -> OperationsStatePublic:
        """Return one consistent queue/execution snapshot and its revisions."""
        connection = open_database("operations")
        try:
            connection.execute("BEGIN")
            queue_records = queue_repository.queue_operations(connection)
            staging_records = execution_repository.staging_records(connection)
            layout = automatic_queue(queue_records)
            dependencies = self._prepare_dependencies(
                connection, (*queue_records, *staging_records),
                (group.group_key for group in layout.groups if group.kind == "pending_issue"),
            )
            queue = QueueStatePublic(
                groups=tuple(
                    QueueGroupPublic(
                        group_key=group.group_key,
                        kind=group.kind,
                        producer_state=(
                            dependencies.states[group.group_key].state
                            if group.kind == "pending_issue" else None
                        ),
                        operations=self._queue_records_public(group.operations, dependencies),
                    )
                    for group in layout.groups
                ),
                standalone=self._queue_records_public(layout.standalone, dependencies),
            )
            execution_parts: dict[StagingState, list[StagingOperationPublic]] = {
                "staged": [], "executing": [], "indeterminate": [], "held": [],
            }
            for operation in self._staging_records_public(staging_records, dependencies):
                execution_parts[operation.state].append(operation)
            return OperationsStatePublic(
                revisions=repository_metadata.revisions(connection),
                queue=queue,
                execution=ExecutionStatePublic(
                    staged=tuple(execution_parts["staged"]),
                    executing=tuple(execution_parts["executing"]),
                    indeterminate=tuple(execution_parts["indeterminate"]),
                    held=tuple(execution_parts["held"]),
                ),
            )
        finally:
            connection.close()

    def list_queue(self, page: int = 1, page_size: int = 50) -> QueuePage:
        """Order the queue globally but prepare public records only for this page."""
        _validate_page(page, page_size)
        connection = open_database("operations")
        try:
            connection.execute("BEGIN")
            records = queue_repository.queue_operations(connection)
            operations = automatic_queue(records).flattened()
            start = (page - 1) * page_size
            page_records = operations[start : start + page_size]
            dependencies = self._prepare_dependencies(connection, page_records)
            return QueuePage(
                items=tuple(
                    OperationOverview.model_validate(
                        operation.model_dump(include=set(OperationOverview.model_fields))
                    )
                    for operation in self._queue_records_public(page_records, dependencies)
                ),
                total=len(operations), page=page, page_size=page_size,
                revisions=repository_metadata.revisions(connection),
            )
        finally:
            connection.close()

    def get_queue_operations(self, operation_ids: list[str]) -> QueueOperationDetails:
        """Read only requested queued records and their dependencies in request order."""
        validate_operation_ids(operation_ids, maximum=200)
        connection = open_database("operations")
        try:
            connection.execute("BEGIN")
            records = queue_repository.queue_operations_by_ids(connection, operation_ids)
            dependencies = self._prepare_dependencies(connection, records)
            by_id = {
                record.operation_id: record
                for record in self._queue_records_public(records, dependencies)
            }
            return QueueOperationDetails(
                items=tuple(by_id[item] for item in operation_ids if item in by_id),
                missing_operation_ids=tuple(item for item in operation_ids if item not in by_id),
                revisions=repository_metadata.revisions(connection),
            )
        finally:
            connection.close()

    def list_execution_state(
        self, page: int = 1, page_size: int = 50,
    ) -> ExecutionStatePage:
        """Read one page of active execution records directly in authorized order."""
        _validate_page(page, page_size)
        connection = open_database("operations")
        try:
            connection.execute("BEGIN")
            records, total = execution_repository.staging_page(
                connection, page=page, page_size=page_size,
            )
            dependencies = self._prepare_dependencies(connection, records)
            return ExecutionStatePage(
                items=tuple(
                    ExecutionOperationOverview.model_validate(
                        operation.model_dump(include=set(ExecutionOperationOverview.model_fields))
                    )
                    for operation in self._staging_records_public(records, dependencies)
                ),
                total=total, page=page, page_size=page_size,
                revisions=repository_metadata.revisions(connection),
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
        """Return recent completed execution records for the MCP execution-log tool."""

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
