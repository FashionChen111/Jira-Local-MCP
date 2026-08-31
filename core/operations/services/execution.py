"""Worker execution and conservative startup-recovery state service."""

from __future__ import annotations

from typing import Literal

from core.operations.errors import StoreError
from core.operations.models import ExecutionClaim, OperationRecord, StagingRecord
from core.operations.repository import OperationRepository
from core.operations.rules.dependencies import (
    PRODUCER_OPERATION_TYPES,
    dependency_operation_ids,
    is_jira_issue_key,
)
from core.operations.rules.safety import OperationSafetyError, assert_safe_to_execute
from core.storage.database import new_id, transaction, utc_ms


class ExecutionService:
    """Own the atomic staging and worker execution lifecycle."""

    def __init__(self, repository: OperationRepository) -> None:
        self._repository = repository

    @staticmethod
    def _operation(record: StagingRecord) -> OperationRecord:
        return record.as_operation()

    def claim_next(self) -> ExecutionClaim | None:
        """Atomically claim the first authorized item after dependency checks."""

        with transaction() as connection:
            if any(
                record.state == "indeterminate"
                for record in self._repository.staging_records(connection)
            ):
                return None
            while True:
                records = self._repository.staging_records(connection)
                if not records or records[0].state != "staged":
                    return None
                staging = records[0]
                operation = self._operation(staging)
                dependencies = dependency_operation_ids(operation)
                if dependencies:
                    states = self._repository.producer_states(connection, dependencies)
                    changed = False
                    for dependency_id in dependencies:
                        state = states[dependency_id]
                        if state.state == "success" and is_jira_issue_key(
                            state.new_issue_key
                        ):
                            binding = self._repository.bind_produced_issue(
                                connection,
                                producer_operation_id=dependency_id,
                                new_issue_key=state.new_issue_key,
                            )
                            revisions: list[str] = []
                            if binding.queue_changed:
                                revisions.append("queue")
                            if binding.execution_changed:
                                revisions.append("execution")
                            if revisions:
                                self._repository.bump_revisions(connection, *revisions)
                            changed = True
                            break
                        if (
                            state.terminal_failure
                            or state.state == "unknown"
                            or state.operation_type not in PRODUCER_OPERATION_TYPES
                        ):
                            self._repository.terminalize_dependency_failure(
                                connection,
                                staging,
                                producer_operation_id=dependency_id,
                            )
                            self._repository.bump_revisions(
                                connection, "execution", "history"
                            )
                            changed = True
                            break
                    if changed:
                        continue
                    return None

                preflight_error: str | None = None
                try:
                    assert_safe_to_execute(operation)
                except OperationSafetyError as exc:
                    preflight_error = str(exc)
                execution_id = new_id("exec")
                started_at = utc_ms()
                if not self._repository.mark_claimed(
                    connection, operation.operation_id, execution_id, started_at
                ):
                    continue
                self._repository.insert_execution(
                    connection,
                    operation,
                    execution_id=execution_id,
                    submission_risk_level=staging.submission_risk_level,
                    started_at=started_at,
                )
                self._repository.bump_revisions(connection, "execution")
                return ExecutionClaim(
                    execution_id=execution_id,
                    operation=operation,
                    submission_risk_level=staging.submission_risk_level,
                    stage_order=staging.stage_order,
                    submitted_at=staging.submitted_at,
                    started_at=started_at,
                    preflight_error=preflight_error,
                )

    def complete_execution(
        self,
        execution_id: str,
        outcome: Literal["success", "failed"],
        summary: str | None,
        error: str | None,
        new_issue_key: str | None,
        elapsed_ms: int,
    ) -> None:
        """Persist a worker result and broadcast producer outcomes atomically."""

        with transaction() as connection:
            record = self._repository.execution_record(connection, execution_id)
            if record is None:
                raise StoreError("execution record 不存在")
            if record.outcome is not None:
                if record.outcome == outcome:
                    residue = self._repository.staging_record(
                        connection, record.operation_id
                    )
                    self._repository.delete_staging_by_execution(
                        connection, execution_id
                    )
                    if residue is not None:
                        self._repository.bump_revisions(connection, "execution")
                    return
                raise StoreError("execution record 已有不同终态")
            is_producer = record.operation_type in PRODUCER_OPERATION_TYPES
            if is_producer and outcome == "success" and not is_jira_issue_key(
                new_issue_key
            ):
                raise StoreError("创建/克隆成功结果必须包含合法的新事务 Key")
            if elapsed_ms < 0:
                raise StoreError("elapsed_ms 不得为负数")
            if not self._repository.finish_execution(
                connection,
                execution_id,
                finished_at=utc_ms(),
                outcome=outcome,
                summary=summary,
                error=error,
                new_issue_key=new_issue_key,
                elapsed_ms=elapsed_ms,
            ):
                raise StoreError("execution record 终态写入冲突")

            queue_changed = False
            if is_producer and outcome == "success":
                binding = self._repository.bind_produced_issue(
                    connection,
                    producer_operation_id=record.operation_id,
                    new_issue_key=new_issue_key,  # type: ignore[arg-type]
                )
                queue_changed = binding.queue_changed
            elif is_producer and outcome == "failed":
                self._repository.terminalize_dependants(
                    connection, record.operation_id
                )
            self._repository.delete_staging_by_execution(connection, execution_id)
            names = ["execution", "history"]
            if queue_changed:
                names.append("queue")
            self._repository.bump_revisions(connection, *names)

    def recover_interrupted(self) -> int:
        """Convert orphaned executing rows to indeterminate and hold later work."""

        with transaction() as connection:
            recovered = 0
            execution_changed = False
            history_changed = False
            for staging in self._repository.staging_records(
                connection, {"executing"}
            ):
                if staging.execution_id is None:
                    raise StoreError("executing staging 缺少 execution ID")
                record = self._repository.execution_record(
                    connection, staging.execution_id
                )
                if record is None:
                    raise StoreError(
                        "executing staging 缺少 execution record，拒绝自动恢复"
                    )
                if record.outcome is not None:
                    self._repository.delete_staging_by_operation(
                        connection, staging.operation_id
                    )
                    execution_changed = True
                    continue
                now = utc_ms()
                self._repository.mark_execution_indeterminate(
                    connection, staging.execution_id, now
                )
                self._repository.set_staging_state(
                    connection,
                    staging.operation_id,
                    state="indeterminate",
                    execution_id=staging.execution_id,
                    started_at=staging.started_at,
                )
                self._repository.hold_later_staging(
                    connection,
                    stage_order=staging.stage_order,
                    blocked_by_execution_id=staging.execution_id,
                )
                recovered += 1
                execution_changed = True
                history_changed = True
            names: list[str] = []
            if execution_changed:
                names.append("execution")
            if history_changed:
                names.append("history")
            if names:
                self._repository.bump_revisions(connection, *names)
            return recovered
