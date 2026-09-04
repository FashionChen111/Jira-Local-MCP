"""Pure cross-type dependency lifecycle orchestration."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from typing_extensions import assert_never

from core.operations.dependencies import _attachment, _issue
from core.operations.errors import DependencyError
from core.operations.models import (
    AttachmentDependencyResolution,
    AttachmentResult,
    DependencyResolution,
    DependencyState,
    IssueDependencyResolution,
    OperationRecord,
    OperationType,
)
from core.operations.rules.identifiers import is_jira_issue_key


@dataclass(frozen=True)
class DependencyInspection:
    """References and diagnostics produced by one consumer scan."""

    issue_references: tuple[_issue.IssueReference, ...]
    attachment_inspection: _attachment.AttachmentReferenceInspection


@dataclass(frozen=True)
class DependencyResolutionEvent:
    """One immutable event body derived from an actual materialization."""

    resolution: DependencyResolution
    reference_paths: tuple[str, ...]

    @property
    def action_code(
        self,
    ) -> Literal["issue_key_resolved", "attachment_resolved"]:
        """Return the stable event action for the typed producer result."""

        if isinstance(self.resolution, IssueDependencyResolution):
            return "issue_key_resolved"
        if isinstance(self.resolution, AttachmentDependencyResolution):
            return "attachment_resolved"
        assert_never(self.resolution)

    @property
    def producer_operation_id(self) -> str:
        """Return the already-validated producer operation ID."""

        return self.resolution.producer_operation_id

    @property
    def data(self) -> Mapping[str, object]:
        """Return a fresh serializable projection of the immutable event."""

        if isinstance(self.resolution, IssueDependencyResolution):
            return {
                "new_issue_key": self.resolution.issue_key,
                "fields": self.reference_paths,
            }
        if isinstance(self.resolution, AttachmentDependencyResolution):
            return {
                "attachment_result": (
                    self.resolution.attachment_result.model_dump(mode="json")
                ),
                "paths": self.reference_paths,
            }
        assert_never(self.resolution)

@dataclass(frozen=True)
class DependencyMaterialization:
    """One final immutable snapshot and the changes that produced it."""

    operation: OperationRecord
    events: tuple[DependencyResolutionEvent, ...]


@dataclass(frozen=True)
class WaitingDependency:
    """A valid dependency whose producer may still complete."""


@dataclass(frozen=True)
class UnavailableDependency:
    """A missing, invalid, or terminal dependency that cannot materialize."""


DependencyStateDecision = (
    DependencyResolution | WaitingDependency | UnavailableDependency
)


def inspect_dependencies(operation: OperationRecord) -> DependencyInspection:
    """Inspect issue references plus attachment references and diagnostics."""

    return DependencyInspection(
        issue_references=_issue.collect_references(operation),
        attachment_inspection=_attachment.inspect_references(operation),
    )


def collect_referenced_operation_ids(
    inspection: DependencyInspection,
) -> tuple[str, ...]:
    """Return distinct referenced operation IDs in first-reference order."""

    referenced_operation_ids = [
        reference.referenced_operation_id
        for reference in inspection.issue_references
    ]
    referenced_operation_ids.extend(
        reference.referenced_operation_id
        for reference in inspection.attachment_inspection.references
    )
    return tuple(dict.fromkeys(referenced_operation_ids))


def merge_referenced_operation_ids(
    referenced_operation_id_groups: Iterable[Iterable[str]],
) -> set[str]:
    """Return unique referenced operation IDs across consumer groups."""

    return {
        referenced_operation_id
        for referenced_operation_ids in referenced_operation_id_groups
        for referenced_operation_id in referenced_operation_ids
    }


def validate_reference_syntax(
    operation: OperationRecord,
    inspection: DependencyInspection,
    *,
    allow_unresolved: bool,
) -> None:
    """Assert issue and attachment syntax for one lifecycle boundary."""

    _issue.validate_reference_syntax(
        operation, allow_unresolved=allow_unresolved
    )
    _attachment.validate_reference_syntax(
        operation,
        inspection.attachment_inspection,
        allow_unresolved=allow_unresolved,
    )


def validate_reference_contract(
    operation: OperationRecord,
    inspection: DependencyInspection,
    states: Mapping[str, DependencyState],
) -> None:
    """Assert producer existence, capability, context, and lifecycle."""

    _issue.validate_reference_contract(inspection.issue_references, states)
    _attachment.validate_reference_contract(
        operation,
        inspection.attachment_inspection.references,
        states,
    )


def resolution_from_state(
    state: DependencyState,
) -> DependencyResolution | None:
    """Project one complete successful producer state to its typed result."""

    if _issue.is_producer(state.operation_type):
        return _issue.resolution_from_state(state)
    if _attachment.is_producer(state.operation_type):
        return _attachment.resolution_from_state(state)
    return None


def collect_resolutions(
    inspection: DependencyInspection,
    states: Mapping[str, DependencyState],
) -> tuple[DependencyResolution, ...]:
    """Collect available typed results in stable referenced-producer order."""

    resolutions: list[DependencyResolution] = []
    for referenced_operation_id in collect_referenced_operation_ids(inspection):
        state = states.get(referenced_operation_id)
        if state is None:
            continue
        resolution = resolution_from_state(state)
        if resolution is not None:
            resolutions.append(resolution)
    return tuple(resolutions)


def materialize_references(
    operation: OperationRecord,
    resolutions: tuple[DependencyResolution, ...],
) -> DependencyMaterialization:
    """Materialize typed results once and report each actual field/path change."""

    if not resolutions:
        return DependencyMaterialization(operation=operation, events=())

    unique_resolutions: list[DependencyResolution] = []
    seen_producers: set[str] = set()
    issue_keys_by_producer: dict[str, str] = {}
    attachment_results_by_producer: dict[str, AttachmentResult] = {}
    for resolution in resolutions:
        producer_operation_id = resolution.producer_operation_id
        if producer_operation_id in seen_producers:
            continue
        seen_producers.add(producer_operation_id)
        unique_resolutions.append(resolution)
        if isinstance(resolution, IssueDependencyResolution):
            issue_keys_by_producer[producer_operation_id] = resolution.issue_key
        elif isinstance(resolution, AttachmentDependencyResolution):
            attachment_results_by_producer[producer_operation_id] = (
                resolution.attachment_result
            )
        else:
            assert_never(resolution)

    materialized, issue_changes = _issue.materialize_references(
        operation, issue_keys_by_producer
    )
    materialized, attachment_changes = _attachment.materialize_references(
        materialized, attachment_results_by_producer
    )
    changes_by_producer = dict(issue_changes + attachment_changes)
    events = tuple(
        DependencyResolutionEvent(
            resolution=resolution,
            reference_paths=changes_by_producer[
                resolution.producer_operation_id
            ],
        )
        for resolution in unique_resolutions
        if resolution.producer_operation_id in changes_by_producer
    )
    return DependencyMaterialization(operation=materialized, events=events)


def submission_reference_problem(
    operation: OperationRecord,
    inspection: DependencyInspection,
    states: Mapping[str, DependencyState],
) -> str | None:
    """Return the first state/context problem preventing submission."""

    issue_problem = _issue.reference_problem(
        inspection.issue_references,
        states,
        allow_available_resolution=False,
    )
    if issue_problem is not None:
        return issue_problem
    return _attachment.reference_problem(
        operation,
        inspection.attachment_inspection.references,
        states,
        allow_available_resolution=False,
    )


def classify_state(state: DependencyState) -> DependencyStateDecision:
    """Classify a producer state as resolved, waiting, or unavailable."""

    resolution = resolution_from_state(state)
    if resolution is not None:
        return resolution
    if (
        _issue.is_producer(state.operation_type)
        or _attachment.is_producer(state.operation_type)
    ) and state.active:
        return WaitingDependency()
    return UnavailableDependency()


def resolution_from_producer_result(
    *,
    producer_operation_id: str,
    operation_type: OperationType,
    successful: bool,
    new_issue_key: str | None,
    attachment_result: AttachmentResult | None,
) -> DependencyResolution | None:
    """Validate one operation result and project any dependency resolution."""

    if not successful:
        if new_issue_key is not None or attachment_result is not None:
            raise DependencyError("未成功的 operation 不得包含生产者结果")
        return None
    if _issue.is_producer(operation_type):
        if attachment_result is not None:
            raise DependencyError("创建/克隆 operation 不得包含附件结果")
        if not is_jira_issue_key(new_issue_key):
            raise DependencyError("创建/克隆成功结果必须包含合法的新事务 Key")
        return IssueDependencyResolution(
            producer_operation_id=producer_operation_id,
            issue_key=new_issue_key,
        )
    if _attachment.is_producer(operation_type):
        if new_issue_key is not None:
            raise DependencyError("附件上传 operation 不得包含新事务 Key")
        if attachment_result is None:
            raise DependencyError(
                "附件上传成功结果必须包含附件 ID 和最终文件名"
            )
        return AttachmentDependencyResolution(
            producer_operation_id=producer_operation_id,
            attachment_result=attachment_result,
        )
    if new_issue_key is not None or attachment_result is not None:
        raise DependencyError("非生产者 operation 不得包含生产者结果")
    return None


def pending_issue_group_id(operation: OperationRecord) -> str | None:
    """Return the pending Jira issue group implied by one operation."""

    return _issue.pending_issue_group_id(operation)


def attachment_reference_description_paths(
    operation_type: str,
) -> tuple[str, ...]:
    """Return description paths from the attachment reference path source."""

    return _attachment.attachment_reference_description_paths(operation_type)
