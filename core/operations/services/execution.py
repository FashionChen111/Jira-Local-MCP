"""Worker execution and conservative startup-recovery state service."""

from __future__ import annotations

from typing import Literal

from core.operations import repository
from core.operations.dependencies import (
    MaterializableDependency,
    WaitingDependency,
    classify_dependency_state,
    collect_dependency_operation_ids,
    dependency_resolution_from_result,
)
from core.operations.errors import DependencyError, StoreError
from core.operations.models import (
    AttachmentResult,
    ExecutionClaim,
    OperationRecord,
    StagingRecord,
)
from core.operations.rules.safety import OperationSafetyError, assert_safe_to_execute
from core.storage.database import new_id, transaction, utc_ms


class ExecutionService:
    """Own the atomic staging and worker execution lifecycle."""

    @staticmethod
    def _operation(record: StagingRecord) -> OperationRecord:
        return record.as_operation()

    def claim_next(self) -> ExecutionClaim | None:
        """Atomically claim the first authorized item after dependency checks."""

        with transaction() as connection:
            if any(
                record.state == "indeterminate"
                for record in repository.staging_records(connection)
            ):
                return None
            while True:
                records = repository.staging_records(connection)
                if not records or records[0].state != "staged":
                    return None
                staging = records[0]
                operation = self._operation(staging)
                dependencies = collect_dependency_operation_ids(operation)
                if dependencies:
                    states = repository.dependency_states(
                        connection, dependencies
                    )
                    changed = False
                    for dependency_id in dependencies:
                        state = states[dependency_id]
                        decision = classify_dependency_state(state)
                        if isinstance(decision, MaterializableDependency):
                            binding = repository.broadcast_dependency_resolution(
                                connection, decision.resolution
                            )
                            revisions: list[str] = []
                            if binding.queue_changed:
                                revisions.append("queue")
                            if binding.execution_changed:
                                revisions.append("execution")
                            if revisions:
                                repository.bump_revisions(connection, *revisions)
                            changed = True
                            break
                        if not isinstance(decision, WaitingDependency):
                            repository.terminalize_dependency_failure(
                                connection,
                                staging,
                                producer_operation_id=dependency_id,
                            )
                            repository.bump_revisions(
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
                if not repository.mark_claimed(
                    connection, operation.operation_id, execution_id, started_at
                ):
                    continue
                repository.insert_execution(
                    connection,
                    operation,
                    execution_id=execution_id,
                    submission_risk_level=staging.submission_risk_level,
                    started_at=started_at,
                )
                repository.bump_revisions(connection, "execution")
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
        attachment_result: AttachmentResult | None = None,
    ) -> None:
        """Persist a worker result and broadcast producer outcomes atomically."""

        with transaction() as connection:
            record = repository.execution_record(connection, execution_id)
            if record is None:
                raise StoreError("execution record 不存在")
            if record.outcome is not None:
                if record.outcome == outcome:
                    residue = repository.staging_record(
                        connection, record.operation_id
                    )
                    repository.delete_staging_by_execution(
                        connection, execution_id
                    )
                    if residue is not None:
                        repository.bump_revisions(connection, "execution")
                    return
                raise StoreError("execution record 已有不同终态")
            try:
                dependency_resolution = dependency_resolution_from_result(
                    producer_operation_id=record.operation_id,
                    operation_type=record.operation_type,
                    successful=outcome == "success",
                    new_issue_key=new_issue_key,
                    attachment_result=attachment_result,
                )
            except DependencyError as exc:
                raise StoreError(str(exc)) from exc
            if elapsed_ms < 0:
                raise StoreError("elapsed_ms 不得为负数")
            if not repository.finish_execution(
                connection,
                execution_id,
                finished_at=utc_ms(),
                outcome=outcome,
                summary=summary,
                error=error,
                new_issue_key=new_issue_key,
                attachment_result=attachment_result,
                elapsed_ms=elapsed_ms,
            ):
                raise StoreError("execution record 终态写入冲突")

            queue_changed = False
            if dependency_resolution is not None:
                binding = repository.broadcast_dependency_resolution(
                    connection, dependency_resolution
                )
                queue_changed = binding.queue_changed
            elif outcome == "failed":
                repository.terminalize_dependants(
                    connection, record.operation_id
                )
            repository.delete_staging_by_execution(connection, execution_id)
            names = ["execution", "history"]
            if queue_changed:
                names.append("queue")
            repository.bump_revisions(connection, *names)

    def recover_interrupted(self) -> int:
        """Convert orphaned executing rows to indeterminate and hold later work."""

        with transaction() as connection:
            recovered = 0
            execution_changed = False
            history_changed = False
            for staging in repository.staging_records(
                connection, {"executing"}
            ):
                if staging.execution_id is None:
                    raise StoreError("executing staging 缺少 execution ID")
                record = repository.execution_record(
                    connection, staging.execution_id
                )
                if record is None:
                    raise StoreError(
                        "executing staging 缺少 execution record，拒绝自动恢复"
                    )
                if record.outcome is not None:
                    repository.delete_staging_by_operation(
                        connection, staging.operation_id
                    )
                    execution_changed = True
                    continue
                now = utc_ms()
                repository.mark_execution_indeterminate(
                    connection, staging.execution_id, now
                )
                repository.set_staging_state(
                    connection,
                    staging.operation_id,
                    state="indeterminate",
                    execution_id=staging.execution_id,
                    started_at=staging.started_at,
                )
                repository.hold_later_staging(
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
                repository.bump_revisions(connection, *names)
            return recovered
