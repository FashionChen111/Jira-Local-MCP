"""Human resolution service for indeterminate and held operations."""

from __future__ import annotations

from core.operations.dependencies import orchestration as dependency_orchestration
from core.operations.errors import ConfirmationRequired, DependencyError, StoreError
from core.operations.models import AttachmentResult, OperationRecord, Resolution
from core.operations.repository import dependencies as dependency_repository
from core.operations.repository import execution as execution_repository
from core.operations.repository import metadata as repository_metadata
from core.operations.repository import queue as queue_repository
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
            staging = execution_repository.staging_record(connection, operation_id)
            if staging is None or staging.state != "indeterminate":
                raise StoreError("仅允许处置 indeterminate operation")
            if staging.execution_id is None:
                raise StoreError("indeterminate operation 缺少 execution ID")
            try:
                dependency_resolution = (
                    dependency_orchestration.resolution_from_producer_result(
                        producer_operation_id=operation_id,
                        operation_type=staging.operation_type,
                        successful=resolution == "succeeded",
                        new_issue_key=new_issue_key,
                        attachment_result=attachment_result,
                    )
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
            if not execution_repository.resolve_execution(
                connection,
                staging.execution_id,
                resolution=resolution,
                resolved_at=utc_ms(),
                new_issue_key=new_issue_key,
                attachment_result=attachment_result,
            ):
                raise StoreError("execution record 已处置或状态不一致")
            execution_repository.delete_staging_by_operation(
                connection, operation_id
            )

            queue_changed = False
            if dependency_resolution is not None:
                binding = dependency_repository.broadcast_dependency_resolution(
                    connection, dependency_resolution
                )
                queue_changed = binding.queue_changed
            elif resolution == "abandoned":
                dependency_repository.terminalize_dependants(
                    connection, operation_id
                )
            elif resolution == "not_applied":
                operation_data = staging.as_operation().model_dump(mode="python")
                operation_data["attempt_no"] = staging.attempt_no + 1
                operation = OperationRecord.model_validate(operation_data)
                queue_repository.insert_queue(
                    connection,
                    operation,
                    action_code="requeued_not_applied",
                    event_data={"previous_execution_id": staging.execution_id},
                )
                queue_changed = True

            names = ["execution", "history"]
            if queue_changed:
                names.append("queue")
            repository_metadata.bump_revisions(connection, *names)
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
            records = execution_repository.staging_records_by_ids(
                connection, operation_ids
            )
            if len(records) != len(operation_ids) or any(
                record.state != "held" for record in records
            ):
                raise StoreError("held 处置包含未知或非 held operation")

            if action == "resume":
                selected = set(operation_ids)
                inspection_by_operation_id = {
                    record.operation_id: (
                        dependency_orchestration.inspect_dependencies(record)
                    )
                    for record in records
                }
                referenced_ids_by_operation_id = {
                    operation_id: (
                        dependency_orchestration.collect_referenced_operation_ids(
                            inspection
                        )
                    )
                    for operation_id, inspection in (
                        inspection_by_operation_id.items()
                    )
                }
                all_dependencies = (
                    dependency_orchestration.merge_referenced_operation_ids(
                        referenced_ids_by_operation_id.values()
                    )
                )
                states = dependency_repository.dependency_states(
                    connection, all_dependencies
                )
                active_by_id = {
                    record.operation_id: record
                    for record in execution_repository.staging_records(connection)
                }
                for record in records:
                    for referenced_operation_id in (
                        referenced_ids_by_operation_id[record.operation_id]
                    ):
                        state = states[referenced_operation_id]
                        decision = dependency_orchestration.classify_state(state)
                        if not isinstance(
                            decision,
                            (
                                dependency_orchestration.WaitingDependency,
                                dependency_orchestration.UnavailableDependency,
                            ),
                        ):
                            raise StoreError(
                                "held operation 包含已成功但尚未物化的依赖引用，"
                                f"数据不一致：{referenced_operation_id}"
                            )
                        producer = active_by_id.get(referenced_operation_id)
                        will_run_first = bool(
                            isinstance(
                                decision,
                                dependency_orchestration.WaitingDependency,
                            )
                            and producer is not None
                            and producer.stage_order < record.stage_order
                            and (
                                producer.state in {"staged", "executing"}
                                or (
                                    producer.state == "held"
                                    and referenced_operation_id in selected
                                )
                            )
                        )
                        if not will_run_first:
                            raise StoreError(
                                "held operation 的前置依赖尚未解析；"
                                "请同时恢复前置生产者或重新入队："
                                f"{referenced_operation_id}"
                            )

            queue_changed = False
            history_changed = False
            for record in records:
                if action == "resume":
                    execution_repository.set_staging_state(
                        connection, record.operation_id, state="staged"
                    )
                    repository_metadata.append_event(
                        connection,
                        record.operation_id,
                        "internal",
                        "held_resumed",
                        {},
                    )
                elif action == "requeue":
                    execution_repository.delete_staging_by_operation(
                        connection, record.operation_id
                    )
                    queue_repository.insert_queue(
                        connection,
                        record.as_operation(),
                        action_code="held_requeued",
                        event_data={
                            "blocked_by_execution_id": record.blocked_by_execution_id
                        },
                    )
                    queue_changed = True
                else:
                    repository_metadata.append_event(
                        connection,
                        record.operation_id,
                        "internal",
                        "held_abandoned",
                        {
                            "blocked_by_execution_id": record.blocked_by_execution_id
                        },
                    )
                    execution_repository.delete_staging_by_operation(
                        connection, record.operation_id
                    )
                    affected = dependency_repository.terminalize_dependants(
                        connection, record.operation_id
                    )
                    history_changed = history_changed or affected > 0
            names = ["execution"]
            if queue_changed:
                names.append("queue")
            if history_changed:
                names.append("history")
            repository_metadata.bump_revisions(connection, *names)
            return {"operation_ids": operation_ids, "action": action}
