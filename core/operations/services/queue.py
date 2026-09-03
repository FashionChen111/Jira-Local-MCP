"""Queue write service: enqueue immutable snapshots and cancel dependency closures."""

from __future__ import annotations

from pydantic import ValidationError

from core.operations import repository
from core.operations.dependencies import (
    collect_dependency_operation_ids,
    collect_dependency_references,
    collect_dependency_resolutions,
    dependency_resolution_events,
    description_reference_paths,
    materialize_dependency_references,
    validate_dependency_contract,
    validate_dependency_syntax,
)
from core.operations.errors import (
    DependencyConfirmationRequired,
    DependencyError,
    RevisionConflict,
    StoreError,
)
from core.operations.models import OperationDraft, OperationRecord
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
    paths = description_reference_paths(operation_type)
    if operation_type == "create_issue":
        selected = False
        description: str | None = None
        description_file = result.get("description_file")
        if description_file is not None:
            description = read_text_file(
                description_file, field_path="description_file"
            )
            selected = True
        elif "description" in result and result["description"] is not None:
            description = snapshot_text_source(
                result["description"], field_path="description"
            )
            selected = True
        else:
            nested_path = next(
                (path for path in paths if path != "description"), None
            )
            nested = (
                _nested_description(result, nested_path)
                if nested_path is not None
                else None
            )
            if nested is not None and nested_path is not None:
                _, _, nested_value = nested
                description = snapshot_text_source(
                    nested_value, field_path=nested_path
                )
                selected = True
            elif "description" in result:
                description = snapshot_text_source(
                    result["description"], field_path="description"
                )
                selected = True
        result.pop("description_file", None)
        extra_fields = result.get("extra_fields")
        if isinstance(extra_fields, dict) and "description" in extra_fields:
            copied_extra_fields = dict(extra_fields)
            copied_extra_fields.pop("description")
            result["extra_fields"] = copied_extra_fields
        if selected:
            result["description"] = description
        return result

    for path in paths:
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
        original_operation = operation
        with transaction() as connection:
            try:
                references = collect_dependency_references(operation)
                validate_dependency_syntax(
                    operation,
                    allow_unresolved=True,
                    references=references,
                )
                states = repository.dependency_states(
                    connection,
                    collect_dependency_operation_ids(
                        operation, references=references
                    ),
                )
                validate_dependency_contract(
                    operation, states, references=references
                )
                resolutions = collect_dependency_resolutions(
                    operation, states, references=references
                )
            except DependencyError as exc:
                raise StoreError(str(exc)) from exc
            operation = materialize_dependency_references(operation, resolutions)
            repository.insert_queue(
                connection,
                operation,
                action_code="mcp_append",
                event_data={"source": source, "risk_reason": risk_reason},
            )
            for event in dependency_resolution_events(
                original_operation,
                resolutions,
                references=references,
            ):
                repository.append_event(
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
            repository.bump_revisions(connection, "queue")
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
            if repository.revision(connection, "queue") != expected_queue_revision:
                raise RevisionConflict("队列已更新，请刷新后重新确认取消范围")
            operations = repository.queue_operations(connection)
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
            changed = True
            while changed:
                changed = False
                for operation in operations:
                    if operation.operation_id in expanded:
                        continue
                    if set(collect_dependency_operation_ids(operation)) & expanded:
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
                repository.append_event(
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
            repository.delete_queue(connection, ordered_ids)
            repository.bump_revisions(connection, "queue")
            return ordered_ids, repository.revision(connection, "queue")
