"""Queue write service: enqueue immutable snapshots and cancel dependency closures."""

from __future__ import annotations

from pydantic import ValidationError

from core.operations.errors import DependencyConfirmationRequired, RevisionConflict, StoreError
from core.operations.models import OperationDraft, OperationRecord
from core.operations.repository import OperationRepository
from core.operations.rules.dependencies import (
    DependencyError,
    dependency_fields,
    dependency_operation_ids,
    materialize_targets,
    validate_referenced_producers,
)
from core.operations.rules.ordering import automatic_queue
from core.operations.rules.safety import OperationSafetyError, validate_for_enqueue
from core.storage.database import new_id, transaction, utc_ms


class QueueService:
    """Own enqueue/cancel use cases without coordinating other services."""

    def __init__(self, repository: OperationRepository) -> None:
        self._repository = repository

    def enqueue(
        self,
        operation_type: str,
        issue_key: str | None,
        payload: dict[str, object],
        description: str,
        source: str = "",
        target_issue_key: str | None = None,
    ) -> OperationRecord:
        """Validate, immediately materialize completed producers, and enqueue."""

        try:
            draft = OperationDraft.model_validate(
                {
                    "operation_type": operation_type,
                    "issue_key": issue_key,
                    "target_issue_key": target_issue_key,
                    "payload": payload,
                    "description": description,
                    "source": source,
                }
            )
            risk_level, risk_reason = validate_for_enqueue(draft)
        except (ValidationError, OperationSafetyError) as exc:
            raise StoreError(str(exc)) from exc

        operation = OperationRecord(
            operation_id=new_id("op"),
            operation_type=draft.operation_type,
            issue_key=draft.issue_key,
            target_issue_key=draft.target_issue_key,
            risk_level=risk_level,
            payload=draft.payload,
            description=draft.description,
            source=draft.source,
            created_at=utc_ms(),
            attempt_no=1,
        )
        original_fields = dependency_fields(operation)
        with transaction() as connection:
            states = self._repository.producer_states(
                connection, dependency_operation_ids(operation)
            )
            try:
                resolved = validate_referenced_producers(operation, states)
            except DependencyError as exc:
                raise StoreError(str(exc)) from exc
            operation = materialize_targets(operation, resolved)
            self._repository.insert_queue(
                connection,
                operation,
                action_code="mcp_append",
                event_data={"source": source, "risk_reason": risk_reason},
            )
            for producer_id, new_issue_key in resolved.items():
                fields = [name for name, value in original_fields if value == producer_id]
                self._repository.append_event(
                    connection,
                    operation.operation_id,
                    "internal",
                    "issue_key_resolved",
                    {
                        "producer_operation_id": producer_id,
                        "new_issue_key": new_issue_key,
                        "fields": fields,
                        "location": "queue",
                        "resolved_during_enqueue": True,
                    },
                )
            self._repository.bump_revisions(connection, "queue")
        return operation

    def cancel(
        self,
        expected_queue_revision: int,
        operation_ids: list[str],
        cascade_confirmed: bool,
    ) -> tuple[list[str], int]:
        """Cancel selected queue entries and their direct-reference closure."""

        if (
            not operation_ids
            or len(operation_ids) > 1000
            or len(operation_ids) != len(set(operation_ids))
        ):
            raise StoreError("取消 operation ID 必须为 1 至 1000 个且不得重复")
        with transaction() as connection:
            if self._repository.revision(connection, "queue") != expected_queue_revision:
                raise RevisionConflict("队列已更新，请刷新后重新确认取消范围")
            operations = self._repository.queue_operations(connection)
            operation_by_id = {
                operation.operation_id: operation for operation in operations
            }
            if any(operation_id not in operation_by_id for operation_id in operation_ids):
                raise StoreError("取消请求包含未知或已处理的 operation")

            requested = set(operation_ids)
            expanded = set(requested)
            changed = True
            while changed:
                changed = False
                for operation in operations:
                    if operation.operation_id in expanded:
                        continue
                    if set(dependency_operation_ids(operation)) & expanded:
                        expanded.add(operation.operation_id)
                        changed = True

            display = automatic_queue(operations).flattened()
            ordered_ids = [
                operation.operation_id
                for operation in display
                if operation.operation_id in expanded
            ]
            cascaded = [item for item in ordered_ids if item not in requested]
            if cascaded and not cascade_confirmed:
                raise DependencyConfirmationRequired(
                    f"取消生产者将同时取消 {len(cascaded)} 个依赖 operation",
                    ordered_ids,
                )
            for operation_id in ordered_ids:
                self._repository.append_event(
                    connection,
                    operation_id,
                    "dequeue",
                    (
                        "cancelled_by_user"
                        if operation_id in requested
                        else "cascade_cancelled"
                    ),
                    {"requested_operation_ids": operation_ids},
                )
            self._repository.delete_queue(connection, ordered_ids)
            self._repository.bump_revisions(connection, "queue")
            return ordered_ids, self._repository.revision(connection, "queue")
