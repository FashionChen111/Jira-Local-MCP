"""Queue write service: enqueue immutable snapshots and cancel dependency closures."""

from __future__ import annotations

from pydantic import ValidationError

from core.operations.dependencies import orchestration as dependency_orchestration
from core.operations.errors import (
    DependencyError,
    DependencyScopeIncomplete,
    RevisionConflict,
    StoreError,
)
from core.operations.models import OperationDraft, OperationRecord
from core.operations.repository import dependencies as dependency_repository
from core.operations.repository import metadata as repository_metadata
from core.operations.repository import queue as queue_repository
from core.operations.rules.identifiers import validate_operation_ids
from core.operations.rules.ordering import automatic_queue
from core.operations.rules.safety import OperationSafetyError, validate_for_enqueue
from core.storage.database import new_id, transaction, utc_ms
from core.textfile_loader import (
    TextFileError,
    read_text_file,
    snapshot_text_source,
)


def _nested_description(
    payload: dict[str, object], path: str
) -> tuple[dict[object, object], str, object] | None:
    container_name, field_name = path.split(".", 1)
    container = payload.get(container_name)
    if not isinstance(container, dict) or field_name not in container:
        return None
    return container, field_name, container[field_name]


def _snapshot_description_payload(
    operation_type: str, payload: dict[str, object]
) -> dict[str, object]:
    """Normalize supported descriptions with shallow copy-on-write ownership."""

    result = dict(payload)
    if operation_type == "create_issue":
        description_file = result.get("description_file")
        if description_file is not None:
            result["description"] = read_text_file(
                description_file, field_path="description_file"
            )
        elif "description" in result:
            result["description"] = snapshot_text_source(
                result["description"], field_path="description"
            )
        result.pop("description_file", None)
        return result

    for path in dependency_orchestration.attachment_reference_description_paths(
        operation_type
    ):
        nested = _nested_description(result, path)
        if nested is None:
            continue
        container, field_name, value = nested
        snapped = snapshot_text_source(value, field_path=path)
        if isinstance(value, dict):
            copied_container = dict(container)
            copied_container[field_name] = snapped
            container_name, _ = path.split(".", 1)
            result[container_name] = copied_container
    return result


class QueueService:
    """Own enqueue/cancel use cases without coordinating other services."""

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
            payload = _snapshot_description_payload(operation_type, payload)
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
        except (ValidationError, OperationSafetyError, TextFileError) as exc:
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
        with transaction() as connection:
            try:
                inspection = dependency_orchestration.inspect_dependencies(
                    operation
                )
                dependency_orchestration.validate_reference_syntax(
                    operation,
                    inspection,
                    allow_unresolved=True,
                )
                referenced_operation_ids = (
                    dependency_orchestration.collect_referenced_operation_ids(
                        inspection
                    )
                )
                states = dependency_repository.dependency_states(
                    connection,
                    referenced_operation_ids,
                )
                dependency_orchestration.validate_reference_contract(
                    operation,
                    inspection,
                    states,
                )
                resolutions = dependency_orchestration.collect_resolutions(
                    inspection,
                    states,
                )
            except DependencyError as exc:
                raise StoreError(str(exc)) from exc
            events: tuple[
                dependency_orchestration.DependencyResolutionEvent, ...
            ] = ()
            if resolutions:
                materialization = dependency_orchestration.materialize_references(
                    operation,
                    resolutions,
                )
                operation = materialization.operation
                events = materialization.events
            queue_repository.insert_queue(
                connection,
                operation,
                action_code="mcp_append",
                event_data={"source": source, "risk_reason": risk_reason},
            )
            for event in events:
                repository_metadata.append_event(
                    connection,
                    operation.operation_id,
                    "internal",
                    event.action_code,
                    {
                        "producer_operation_id": event.producer_operation_id,
                        **event.data,
                        "location": "queue",
                        "resolved_during_enqueue": True,
                    },
                )
            repository_metadata.bump_revisions(connection, "queue")
        return operation

    def cancel(
        self,
        expected_queue_revision: int,
        operation_ids: list[str],
    ) -> tuple[list[str], int]:
        """Cancel an explicit dependency-closed scope in one transaction."""

        try:
            validate_operation_ids(operation_ids, maximum=1000)
        except ValueError as exc:
            raise StoreError(str(exc)) from exc
        if type(expected_queue_revision) is not int or expected_queue_revision < 0:
            raise StoreError("expected_queue_revision 必须为非负整数")
        with transaction() as connection:
            if (
                repository_metadata.revision(connection, "queue")
                != expected_queue_revision
            ):
                raise RevisionConflict("队列已更新，请刷新后重新确认取消范围")
            operations = queue_repository.queue_operations(connection)
            operation_by_id = {
                operation.operation_id: operation for operation in operations
            }
            if any(
                operation_id not in operation_by_id
                for operation_id in operation_ids
            ):
                raise StoreError("取消请求包含未知或已处理的 operation")

            requested = set(operation_ids)
            expanded = set(requested)
            dependants_by_producer: dict[str, list[str]] = {}
            for operation in operations:
                inspection = dependency_orchestration.inspect_dependencies(
                    operation
                )
                for referenced_operation_id in (
                    dependency_orchestration.collect_referenced_operation_ids(
                        inspection
                    )
                ):
                    dependants_by_producer.setdefault(
                        referenced_operation_id, []
                    ).append(operation.operation_id)
            pending = list(requested)
            while pending:
                producer_operation_id = pending.pop()
                for dependant_operation_id in dependants_by_producer.get(
                    producer_operation_id, ()
                ):
                    if dependant_operation_id in expanded:
                        continue
                    expanded.add(dependant_operation_id)
                    pending.append(dependant_operation_id)

            display = automatic_queue(operations).flattened()
            ordered_ids = [
                operation.operation_id
                for operation in display
                if operation.operation_id in expanded
            ]
            missing = [item for item in ordered_ids if item not in requested]
            if missing:
                raise DependencyScopeIncomplete(
                    f"取消范围缺少 {len(missing)} 个依赖 operation；请提交完整范围",
                    ordered_ids,
                    missing,
                )
            for operation_id in ordered_ids:
                repository_metadata.append_event(
                    connection,
                    operation_id,
                    "dequeue",
                    "cancelled_by_user",
                    {"requested_operation_ids": operation_ids},
                )
            queue_repository.delete_queue(connection, ordered_ids)
            repository_metadata.bump_revisions(connection, "queue")
            return ordered_ids, repository_metadata.revision(connection, "queue")
