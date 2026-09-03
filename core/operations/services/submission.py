"""Review submission service: closure checks, risk confirmation, and staging."""

from __future__ import annotations

from core.operations import repository
from core.operations.dependencies import (
    collect_dependency_operation_ids,
    submission_block_reason,
)
from core.operations.errors import (
    ConfirmationRequired,
    ExecutionBlocked,
    RevisionConflict,
    StoreError,
)
from core.operations.models import OperationRecord, RiskLevel
from core.operations.repository import DatabaseConnection
from core.operations.rules.ordering import (
    OrderingError,
    automatic_queue,
    stable_dependency_order,
)
from core.operations.rules.safety import (
    OperationSafetyError,
    assert_safe_for_submit,
    classify_submission_risks,
)
from core.storage.database import open_database, transaction, utc_ms


class SubmissionService:
    """Own the only queue-to-staging authorization transition."""

    def _selection(
        self,
        connection: DatabaseConnection,
        expected_queue_revision: int,
        operation_ids: list[str],
        high_risk_confirmed: bool,
    ) -> tuple[tuple[OperationRecord, ...], dict[str, RiskLevel]]:
        if (
            not operation_ids
            or len(operation_ids) > 1000
            or len(operation_ids) != len(set(operation_ids))
        ):
            raise StoreError("提交 operation ID 必须为 1 至 1000 个且不得重复")
        if repository.revision(connection, "queue") != expected_queue_revision:
            raise RevisionConflict("队列已更新，请刷新后重新确认")
        if repository.has_recovery_block(connection):
            raise ExecutionBlocked(
                "存在不确定或冻结任务，处置完成前不能提交新任务"
            )

        queue_operations = repository.queue_operations(connection)
        operation_by_id = {
            operation.operation_id: operation for operation in queue_operations
        }
        if any(operation_id not in operation_by_id for operation_id in operation_ids):
            raise StoreError("提交包含未知或已处理的 operation")
        selected_ids = set(operation_ids)
        all_dependency_ids = {
            dependency_id
            for operation_id in operation_ids
            for dependency_id in collect_dependency_operation_ids(
                operation_by_id[operation_id]
            )
        }
        dependency_states = repository.dependency_states(
            connection, all_dependency_ids
        )
        display = automatic_queue(queue_operations, dependency_states).flattened()
        selected = tuple(
            operation for operation in display if operation.operation_id in selected_ids
        )

        for operation in selected:
            try:
                assert_safe_for_submit(operation)
            except OperationSafetyError as exc:
                raise StoreError(str(exc)) from exc
            reason = submission_block_reason(operation, dependency_states)
            if reason is not None:
                raise StoreError(reason)
            for dependency_id in collect_dependency_operation_ids(operation):
                state = dependency_states[dependency_id]
                if state.state == "queued" and dependency_id not in selected_ids:
                    raise StoreError(
                        f"提交缺少依赖 operation：{dependency_id}"
                    )

        try:
            selected = stable_dependency_order(selected)
        except OrderingError as exc:
            raise StoreError(str(exc)) from exc
        submission_risks = classify_submission_risks(selected)
        if (
            any(level == "high" for level in submission_risks.values())
            and not high_risk_confirmed
        ):
            raise ConfirmationRequired("本次提交包含高风险操作，需要二次确认")
        return selected, submission_risks

    def validate_submission(
        self,
        expected_queue_revision: int,
        operation_ids: list[str],
        high_risk_confirmed: bool,
    ) -> None:
        """Validate before Jira ping; submit revalidates atomically afterward."""

        connection = open_database("operations")
        try:
            self._selection(
                connection,
                expected_queue_revision,
                operation_ids,
                high_risk_confirmed,
            )
        finally:
            connection.close()

    def submit(
        self,
        expected_queue_revision: int,
        operation_ids: list[str],
        high_risk_confirmed: bool,
    ) -> tuple[list[str], dict[str, int]]:
        """Atomically authorize the stable automatic/topological execution order."""

        with transaction() as connection:
            selected, submission_risks = self._selection(
                connection,
                expected_queue_revision,
                operation_ids,
                high_risk_confirmed,
            )
            stage_base = repository.maximum_stage_order(connection)
            submitted_at = utc_ms()
            for offset, operation in enumerate(selected, 1):
                stage_order = stage_base + offset
                repository.insert_staging(
                    connection,
                    operation,
                    stage_order=stage_order,
                    submission_risk_level=submission_risks[operation.operation_id],
                    submitted_at=submitted_at,
                )
                repository.append_event(
                    connection,
                    operation.operation_id,
                    "dequeue",
                    "submit_for_execution",
                    {
                        "stage_order": stage_order,
                        "submission_risk_level": submission_risks[
                            operation.operation_id
                        ],
                    },
                )
            repository.delete_queue(
                connection, [operation.operation_id for operation in selected]
            )
            repository.bump_revisions(connection, "queue", "execution")
            return (
                [operation.operation_id for operation in selected],
                repository.revisions(connection),
            )
