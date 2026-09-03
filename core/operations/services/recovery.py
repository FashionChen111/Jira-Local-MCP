"""Human resolution service for indeterminate and held operations."""

from __future__ import annotations

from core.operations import repository
from core.operations.dependencies import (
    MaterializableDependency,
    WaitingDependency,
    classify_dependency_state,
    collect_dependency_operation_ids,
    dependency_resolution_from_result,
)
from core.operations.errors import ConfirmationRequired, DependencyError, StoreError
from core.operations.models import AttachmentResult, OperationRecord, Resolution
from core.storage.database import transaction, utc_ms


class RecoveryService:
    """Own explicit human decisions after uncertain or held execution."""

    def resolve_indeterminate(
        self,
        operation_id: str,
        resolution: Resolution,
        high_risk_confirmed: bool,
        new_issue_key: str | None = None,
        attachment_result: AttachmentResult | None = None,
    ) -> dict[str, object]:
        """Record the human conclusion for an indeterminate execution."""

        if resolution not in {"succeeded", "not_applied", "abandoned"}:
            raise StoreError("不支持的人工处置结论")
        with transaction() as connection:
            staging = repository.staging_record(connection, operation_id)
            if staging is None or staging.state != "indeterminate":
                raise StoreError("仅允许处置 indeterminate operation")
            if staging.execution_id is None:
                raise StoreError("indeterminate operation 缺少 execution ID")
            try:
                dependency_resolution = dependency_resolution_from_result(
                    producer_operation_id=operation_id,
                    operation_type=staging.operation_type,
                    successful=resolution == "succeeded",
                    new_issue_key=new_issue_key,
                    attachment_result=attachment_result,
                )
            except DependencyError as exc:
                raise StoreError(str(exc)) from exc
            if (
                resolution == "not_applied"
                and staging.submission_risk_level == "high"
                and not high_risk_confirmed
            ):
                raise ConfirmationRequired(
                    "高风险 operation 重新入队前需要二次确认"
                )
            if not repository.resolve_execution(
                connection,
                staging.execution_id,
                resolution=resolution,
                resolved_at=utc_ms(),
                new_issue_key=new_issue_key,
                attachment_result=attachment_result,
            ):
                raise StoreError("execution record 已处置或状态不一致")
            repository.delete_staging_by_operation(connection, operation_id)

            queue_changed = False
            if dependency_resolution is not None:
                binding = repository.broadcast_dependency_resolution(
                    connection, dependency_resolution
                )
                queue_changed = binding.queue_changed
            elif resolution == "abandoned":
                repository.terminalize_dependants(connection, operation_id)
            elif resolution == "not_applied":
                operation_data = staging.as_operation().model_dump(mode="python")
                operation_data["attempt_no"] = staging.attempt_no + 1
                operation = OperationRecord.model_validate(operation_data)
                repository.insert_queue(
                    connection,
                    operation,
                    action_code="requeued_not_applied",
                    event_data={"previous_execution_id": staging.execution_id},
                )
                queue_changed = True

            names = ["execution", "history"]
            if queue_changed:
                names.append("queue")
            repository.bump_revisions(connection, *names)
            return {
                "operation_id": operation_id,
                "execution_id": staging.execution_id,
                "resolution": resolution,
                "new_issue_key": new_issue_key,
                "attachment_result": (
                    attachment_result.model_dump(mode="json")
                    if attachment_result is not None
                    else None
                ),
            }

    def resolve_held(
        self, operation_ids: list[str], action: str
    ) -> dict[str, object]:
        """Resume, requeue, or abandon held operations without reordering them."""

        if (
            not operation_ids
            or len(operation_ids) > 1000
            or len(operation_ids) != len(set(operation_ids))
        ):
            raise StoreError("held 处置需要 1 至 1000 个不重复 operation ID")
        if action not in {"resume", "requeue", "abandon"}:
            raise StoreError("不支持的 held 处置动作")
        with transaction() as connection:
            records = repository.staging_records_by_ids(
                connection, operation_ids
            )
            if len(records) != len(operation_ids) or any(
                record.state != "held" for record in records
            ):
                raise StoreError("held 处置包含未知或非 held operation")

            if action == "resume":
                selected = set(operation_ids)
                all_dependencies = {
                    dependency_id
                    for record in records
                    for dependency_id in collect_dependency_operation_ids(record)
                }
                states = repository.dependency_states(
                    connection, all_dependencies
                )
                active_by_id = {
                    record.operation_id: record
                    for record in repository.staging_records(connection)
                }
                for record in records:
                    for dependency_id in collect_dependency_operation_ids(record):
                        state = states[dependency_id]
                        decision = classify_dependency_state(state)
                        if isinstance(decision, MaterializableDependency):
                            continue
                        producer = active_by_id.get(dependency_id)
                        will_run_first = bool(
                            isinstance(decision, WaitingDependency)
                            and producer is not None
                            and producer.stage_order < record.stage_order
                            and (
                                producer.state in {"staged", "executing"}
                                or (
                                    producer.state == "held"
                                    and dependency_id in selected
                                )
                            )
                        )
                        if not will_run_first:
                            raise StoreError(
                                "held operation 的前置依赖尚未解析；"
                                f"请同时恢复前置生产者或重新入队：{dependency_id}"
                            )

            queue_changed = False
            history_changed = False
            for record in records:
                if action == "resume":
                    repository.set_staging_state(
                        connection, record.operation_id, state="staged"
                    )
                    repository.append_event(
                        connection,
                        record.operation_id,
                        "internal",
                        "held_resumed",
                        {},
                    )
                elif action == "requeue":
                    repository.delete_staging_by_operation(
                        connection, record.operation_id
                    )
                    repository.insert_queue(
                        connection,
                        record.as_operation(),
                        action_code="held_requeued",
                        event_data={
                            "blocked_by_execution_id": record.blocked_by_execution_id
                        },
                    )
                    queue_changed = True
                else:
                    repository.append_event(
                        connection,
                        record.operation_id,
                        "internal",
                        "held_abandoned",
                        {
                            "blocked_by_execution_id": record.blocked_by_execution_id
                        },
                    )
                    repository.delete_staging_by_operation(
                        connection, record.operation_id
                    )
                    affected = repository.terminalize_dependants(
                        connection, record.operation_id
                    )
                    history_changed = history_changed or affected > 0
            names = ["execution"]
            if queue_changed:
                names.append("queue")
            if history_changed:
                names.append("history")
            repository.bump_revisions(connection, *names)
            return {"operation_ids": operation_ids, "action": action}
