"""Strict immutable values shared by the operations domain and its adapters."""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


OperationType = Literal[
    "create_issue",
    "clone_issue",
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
OperationExecutionOutcome = Literal["success", "warning", "failed"]
ExecutionOutcome = OperationExecutionOutcome | Literal["indeterminate", "dependency_failed"]
Resolution = Literal["succeeded", "not_applied", "abandoned"]
ProducerLifecycleState = Literal[
    "queued",
    "staged",
    "executing",
    "indeterminate",
    "held",
    "success",
    "failed",
    "warning",
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


class AttachmentIdentity(StrictFrozenModel):
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


class AttachmentResult(StrictFrozenModel):
    """One attachment interaction and its confirmed identity, if successful."""

    requested_filename: str = Field(min_length=1, max_length=1024)
    status: Literal["success", "failed", "unknown", "unattempted"]
    source_attachment_id: str | None = Field(
        default=None, min_length=1, max_length=255
    )
    attachment: AttachmentIdentity | None = None
    error: str | None = None

    @field_validator("requested_filename", "source_attachment_id", "error")
    @classmethod
    def nonblank_without_nul(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("附件名称、源 ID 和错误信息不得为空或包含 NUL")
        return value

    @model_validator(mode="after")
    def consistent_outcome(self) -> AttachmentResult:
        if (self.status == "success") != (self.attachment is not None):
            raise ValueError("仅成功结果允许且必须包含已确认附件身份")
        if (self.status in ("failed", "unknown")) != (self.error is not None):
            raise ValueError("失败或结果未知必须包含错误，其余状态不得包含错误")
        return self


class OperationExecutionResult(StrictFrozenModel):
    """Typed Jira handler result persisted by the execution service."""

    summary: str
    new_issue_key: str | None = None
    attachment_results: tuple[AttachmentResult, ...] = ()
    outcome: OperationExecutionOutcome = "success"
    error: str | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> OperationExecutionResult:
        if self.outcome == "success" and any(
            item.status != "success" for item in self.attachment_results
        ):
            raise ValueError("附件处理未全部完成，不能返回 success")
        if self.outcome == "failed":
            if not self.error or not self.error.strip():
                raise ValueError("失败结果必须包含错误信息")
        elif self.error is not None:
            raise ValueError("仅失败结果允许包含顶层错误")
        return self


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
    attachment_results: tuple[AttachmentResult, ...] = ()
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
    """Persisted lifecycle projection for one referenced operation.

    ``operation_issue_key`` is the producer operation's Jira issue context.
    Result fields mirror storage; consumers obtain a typed resolution through
    the dependency package instead of inferring a type from nullable fields.
    """

    operation_id: str
    operation_type: OperationType | None
    state: ProducerLifecycleState
    operation_issue_key: str | None = None
    new_issue_key: str | None = None
    attachment_results: tuple[AttachmentResult, ...] = ()

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


class IssueDependencyResolution(StrictFrozenModel):
    """A Jira issue key produced by a completed create or clone operation."""

    kind: Literal["issue"] = "issue"
    producer_operation_id: str
    issue_key: str


class AttachmentDependencyResolution(StrictFrozenModel):
    """A Jira attachment identity produced by a completed upload operation."""

    kind: Literal["attachment"] = "attachment"
    producer_operation_id: str
    attachment: AttachmentIdentity


DependencyResolution = IssueDependencyResolution | AttachmentDependencyResolution


class DependencyBindingResult(StrictFrozenModel):
    """Active consumers changed by broadcasting one typed dependency result."""

    queue_operation_ids: tuple[str, ...] = ()
    staging_operation_ids: tuple[str, ...] = ()

    @property
    def queue_changed(self) -> bool:
        return bool(self.queue_operation_ids)

    @property
    def execution_changed(self) -> bool:
        return bool(self.staging_operation_ids)


class OperationOverview(StrictFrozenModel):
    """Common public metadata selected by queue and execution overviews."""

    operation_id: str
    operation_type: OperationType
    issue_key: str | None
    target_issue_key: str | None
    risk_level: RiskLevel
    description: str
    created_at: int


class OperationPublic(OperationOverview):
    """Stable public operation projection without persistence-only fields."""

    payload: dict[str, JsonValue]
    source: str
    attempt_no: int
    dependency_states: dict[str, ProducerLifecycleState] = Field(
        default_factory=dict,
        description="依赖 operation ID 到生命周期状态的映射；包含 unknown，不表达执行顺序。",
    )


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
    attachment_results: tuple[AttachmentResult, ...] = ()
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
