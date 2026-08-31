"""Human resolution service for indeterminate and held operations."""

from __future__ import annotations

from core.operations.errors import ConfirmationRequired, StoreError
from core.operations.models import OperationRecord
from core.operations.repository import OperationRepository
from core.operations.rules.dependencies import (
    PRODUCER_OPERATION_TYPES,
    dependency_operation_ids,
    is_jira_issue_key,
)
from core.storage.database import transaction, utc_ms


class RecoveryService:
    """Own explicit human decisions after uncertain or held execution."""

    def __init__(self, repository: OperationRepository) -> None:
        self._repository = repository

    def resolve_indeterminate(
        self,
        operation_id: str,
        resolution: str,
        high_risk_confirmed: bool,
        new_issue_key: str | None = None,
    ) -> dict[str, object]:
        """Record the human conclusion for an indeterminate execution."""

        if resolution not in {"succeeded", "not_applied", "abandoned"}:
            raise StoreError("不支持的人工处置结论")
        with transaction() as connection:
            staging = self._repository.staging_record(connection, operation_id)
            if staging is None or staging.state != "indeterminate":
                raise StoreError("仅允许处置 indeterminate operation")
            if staging.execution_id is None:
                raise StoreError("indeterminate operation 缺少 execution ID")
            is_producer = staging.operation_type in PRODUCER_OPERATION_TYPES
            if is_producer and resolution == "succeeded" and not is_jira_issue_key(
                new_issue_key
            ):
                raise StoreError("创建/克隆标记为已生效时必须填写真实 issue Key")
            if not is_producer and new_issue_key is not None:
                raise StoreError("只有创建/克隆 operation 可以补录新事务 Key")
            if resolution != "succeeded" and new_issue_key is not None:
                raise StoreError("仅已生效处置可以提供新事务 Key")
            if (
                resolution == "not_applied"
                and staging.submission_risk_level == "high"
                and not high_risk_confirmed
            ):
                raise ConfirmationRequired(
                    "高风险 operation 重新入队前需要二次确认"
                )
            if not self._repository.resolve_execution(
                connection,
                staging.execution_id,
                resolution=resolution,  # type: ignore[arg-type]
                resolved_at=utc_ms(),
                new_issue_key=new_issue_key,
            ):
                raise StoreError("execution record 已处置或状态不一致")
            self._repository.delete_staging_by_operation(connection, operation_id)

            queue_changed = False
            if is_producer and resolution == "succeeded":
                binding = self._repository.bind_produced_issue(
                    connection,
                    producer_operation_id=operation_id,
                    new_issue_key=new_issue_key,  # type: ignore[arg-type]
                )
                queue_changed = binding.queue_changed
            elif is_producer and resolution == "abandoned":
                self._repository.terminalize_dependants(connection, operation_id)
            elif resolution == "not_applied":
                operation_data = staging.as_operation().model_dump(mode="python")
                operation_data["attempt_no"] = staging.attempt_no + 1
                operation = OperationRecord.model_validate(operation_data)
                self._repository.insert_queue(
                    connection,
                    operation,
                    action_code="requeued_not_applied",
                    event_data={"previous_execution_id": staging.execution_id},
                )
                queue_changed = True

            names = ["execution", "history"]
            if queue_changed:
                names.append("queue")
            self._repository.bump_revisions(connection, *names)
            return {
                "operation_id": operation_id,
                "execution_id": staging.execution_id,
                "resolution": resolution,
                "new_issue_key": new_issue_key,
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
            records = self._repository.staging_records_by_ids(
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
                    for dependency_id in dependency_operation_ids(record)
                }
                states = self._repository.producer_states(
                    connection, all_dependencies
                )
                active_by_id = {
                    record.operation_id: record
                    for record in self._repository.staging_records(connection)
                }
                for record in records:
                    for dependency_id in dependency_operation_ids(record):
                        state = states[dependency_id]
                        if state.state == "success":
                            continue
                        producer = active_by_id.get(dependency_id)
                        will_run_first = bool(
                            producer is not None
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
                                "held operation 的创建/克隆依赖尚未解析；"
                                f"请同时恢复前置生产者或重新入队：{dependency_id}"
                            )

            queue_changed = False
            history_changed = False
            for record in records:
                if action == "resume":
                    self._repository.set_staging_state(
                        connection, record.operation_id, state="staged"
                    )
                    self._repository.append_event(
                        connection,
                        record.operation_id,
                        "internal",
                        "held_resumed",
                        {},
                    )
                elif action == "requeue":
                    self._repository.delete_staging_by_operation(
                        connection, record.operation_id
                    )
                    self._repository.insert_queue(
                        connection,
                        record.as_operation(),
                        action_code="held_requeued",
                        event_data={
                            "blocked_by_execution_id": record.blocked_by_execution_id
                        },
                    )
                    queue_changed = True
                else:
                    self._repository.append_event(
                        connection,
                        record.operation_id,
                        "internal",
                        "held_abandoned",
                        {
                            "blocked_by_execution_id": record.blocked_by_execution_id
                        },
                    )
                    self._repository.delete_staging_by_operation(
                        connection, record.operation_id
                    )
                    if record.operation_type in PRODUCER_OPERATION_TYPES:
                        affected = self._repository.terminalize_dependants(
                            connection, record.operation_id
                        )
                        history_changed = history_changed or affected > 0
            names = ["execution"]
            if queue_changed:
                names.append("queue")
            if history_changed:
                names.append("history")
            self._repository.bump_revisions(connection, *names)
            return {"operation_ids": operation_ids, "action": action}
