"""Strict immutable values shared by the operations domain and its adapters."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


OperationType = Literal[
    "create_issue",
    "clone_issue",
    "move_issue",
    "change_type",
    "transition_issue",
    "update_field",
    "update_custom_field",
    "add_attachment",
    "add_comment",
    "add_worklog",
    "add_issuelink",
]
RiskLevel = Literal["low", "medium", "high"]
QueueGroupKind = Literal["jira_issue", "pending_issue"]
StagingState = Literal["staged", "executing", "indeterminate", "held"]
ExecutionOutcome = Literal["success", "failed", "indeterminate", "dependency_failed"]
Resolution = Literal["succeeded", "not_applied", "abandoned"]
ProducerLifecycleState = Literal[
    "queued",
    "staged",
    "executing",
    "indeterminate",
    "held",
    "success",
    "failed",
    "retry_pending",
    "abandoned",
    "dependency_failed",
    "unknown",
]


class StrictFrozenModel(BaseModel):
    """Base configuration for values crossing an operations module boundary."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
    )


class AttachmentResult(StrictFrozenModel):
    """Stable identity returned by Jira after one attachment upload."""

    id: str = Field(min_length=1, max_length=255)
    filename: str = Field(min_length=1, max_length=1024)

    @field_validator("id", "filename")
    @classmethod
    def nonblank_without_nul(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("附件 ID 和文件名不得为空")
        if "\x00" in value:
            raise ValueError("附件 ID 和文件名不得包含 NUL")
        return value


class OperationExecutionResult(StrictFrozenModel):
    """Typed Jira handler result persisted by the execution service."""

    summary: str
    new_issue_key: str | None = None
    attachment_result: AttachmentResult | None = None


class OperationDraft(StrictFrozenModel):
    """Caller-owned operation data before server identity and risk are assigned."""

    operation_type: OperationType
    issue_key: str | None
    target_issue_key: str | None = None
    payload: dict[str, JsonValue]
    description: str
    source: str = ""


class OperationRecord(StrictFrozenModel):
    """Self-contained immutable operation snapshot persisted in every active table."""

    operation_id: str
    operation_type: OperationType
    issue_key: str | None
    target_issue_key: str | None = None
    risk_level: RiskLevel
    payload: dict[str, JsonValue]
    description: str
    source: str
    created_at: int = Field(ge=0)
    attempt_no: int = Field(default=1, ge=1)

    def with_targets(
        self,
        *,
        issue_key: str | None = None,
        target_issue_key: str | None = None,
        replace_issue_key: bool = False,
        replace_target_issue_key: bool = False,
    ) -> "OperationRecord":
        """Return a validated copy after materializing one or both target keys."""

        changes: dict[str, object] = {}
        if replace_issue_key:
            changes["issue_key"] = issue_key
        if replace_target_issue_key:
            changes["target_issue_key"] = target_issue_key
        data = self.model_dump(mode="python")
        data.update(changes)
        return OperationRecord.model_validate(data)

    def as_operation(self) -> "OperationRecord":
        """Project any specialized snapshot back to its common operation value."""

        return OperationRecord.model_validate(
            self.model_dump(
                mode="python", include=set(OperationRecord.model_fields)
            )
        )


class StagingRecord(OperationRecord):
    """An authorized operation plus its persisted execution-order state."""

    stage_order: int = Field(ge=1)
    state: StagingState
    submission_risk_level: RiskLevel
    execution_id: str | None = None
    submitted_at: int = Field(ge=0)
    started_at: int | None = Field(default=None, ge=0)
    held_reason: str | None = None
    blocked_by_execution_id: str | None = None


class ExecutionRecord(OperationRecord):
    """A historical execution attempt with its reviewed operation snapshot."""

    execution_id: str
    submission_risk_level: RiskLevel
    started_at: int = Field(ge=0)
    finished_at: int | None = Field(default=None, ge=0)
    outcome: ExecutionOutcome | None = None
    summary: str | None = None
    error: str | None = None
    new_issue_key: str | None = None
    attachment_result: AttachmentResult | None = None
    elapsed_ms: int | None = Field(default=None, ge=0)
    resolution: Resolution | None = None
    resolved_at: int | None = Field(default=None, ge=0)


class ExecutionClaim(StrictFrozenModel):
    """One atomically claimed execution and the concrete snapshot sent to Jira."""

    execution_id: str
    operation: OperationRecord
    submission_risk_level: RiskLevel
    stage_order: int = Field(ge=1)
    submitted_at: int = Field(ge=0)
    started_at: int = Field(ge=0)
    preflight_error: str | None = None


class DependencyState(StrictFrozenModel):
    """Current lifecycle fact and typed result for a referenced operation."""

    operation_id: str
    operation_type: OperationType | None
    state: ProducerLifecycleState
    issue_key: str | None = None
    new_issue_key: str | None = None
    attachment_result: AttachmentResult | None = None

    @property
    def active(self) -> bool:
        return self.state in {
            "queued",
            "staged",
            "executing",
            "held",
            "retry_pending",
        }

    @property
    def terminal_failure(self) -> bool:
        return self.state in {
            "failed",
            "indeterminate",
            "abandoned",
            "dependency_failed",
        }


# Kept as a source-compatible alias for integrations importing the old name.
ProducerState = DependencyState


class DependencyBindingResult(StrictFrozenModel):
    """Aggregate effects of broadcasting one producer's resolved Jira key."""

    queue_operation_ids: tuple[str, ...] = ()
    staging_operation_ids: tuple[str, ...] = ()

    @property
    def queue_changed(self) -> bool:
        return bool(self.queue_operation_ids)

    @property
    def execution_changed(self) -> bool:
        return bool(self.staging_operation_ids)


class OperationPublic(StrictFrozenModel):
    """Stable public operation projection without persistence-only fields."""

    operation_id: str
    operation_type: OperationType
    issue_key: str | None
    target_issue_key: str | None
    risk_level: RiskLevel
    payload: dict[str, JsonValue]
    description: str
    source: str
    created_at: int
    attempt_no: int
    dependency_operation_ids: tuple[str, ...] = ()
    dependency_states: dict[str, ProducerLifecycleState] = Field(default_factory=dict)


class QueueOperationPublic(OperationPublic):
    """Queue projection augmented with current submission eligibility."""

    submittable: bool
    submission_block_reason: str | None = None


class StagingOperationPublic(OperationPublic):
    """Public staging projection preserving the authorized execution order."""

    stage_order: int
    state: StagingState
    submission_risk_level: RiskLevel
    execution_id: str | None
    submitted_at: int
    started_at: int | None
    held_reason: str | None
    blocked_by_execution_id: str | None


class ExecutionRecordPublic(OperationPublic):
    """Public execution-history projection."""

    execution_id: str
    submission_risk_level: RiskLevel
    started_at: int
    finished_at: int | None
    outcome: ExecutionOutcome | None
    summary: str | None
    error: str | None
    new_issue_key: str | None
    attachment_result: AttachmentResult | None
    elapsed_ms: int | None
    resolution: Resolution | None
    resolved_at: int | None


class QueueGroupPublic(StrictFrozenModel):
    """One automatically derived Jira or pending-issue group."""

    group_key: str
    kind: QueueGroupKind
    producer_state: ProducerLifecycleState | None = None
    operations: tuple[QueueOperationPublic, ...]


class QueueStatePublic(StrictFrozenModel):
    """Complete automatically ordered review queue."""

    groups: tuple[QueueGroupPublic, ...]
    standalone: tuple[QueueOperationPublic, ...]


class ExecutionStatePublic(StrictFrozenModel):
    """Active execution rows partitioned by lifecycle state."""

    staged: tuple[StagingOperationPublic, ...] = ()
    executing: tuple[StagingOperationPublic, ...] = ()
    indeterminate: tuple[StagingOperationPublic, ...] = ()
    held: tuple[StagingOperationPublic, ...] = ()


class OperationsStatePublic(StrictFrozenModel):
    """Combined Web state with optimistic-concurrency revisions."""

    revisions: dict[str, int]
    queue: QueueStatePublic
    execution: ExecutionStatePublic


class ExecutionHistoryPage(StrictFrozenModel):
    """Paginated immutable execution history."""

    items: tuple[ExecutionRecordPublic, ...]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
