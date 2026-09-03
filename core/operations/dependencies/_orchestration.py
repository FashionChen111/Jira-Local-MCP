"""Cross-type dependency lifecycle rules exposed by the package boundary."""

from __future__ import annotations

from collections.abc import Mapping

from typing_extensions import assert_never

from core.operations.dependencies import _attachment, _issue
from core.operations.dependencies._types import (
    DependencyReferences,
    DependencyResolutionEvent,
    DependencyResolutions,
    DependencyStateDecision,
    MaterializableDependency,
    UnavailableDependency,
    WaitingDependency,
)
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


def collect_dependency_references(
    operation: OperationRecord,
) -> DependencyReferences:
    """Collect valid references and syntax diagnostics without state lookup.

    This function does not raise for malformed attachment placeholders. Call
    :func:`validate_dependency_syntax` at the relevant lifecycle boundary.
    """

    return DependencyReferences(
        issues=_issue.collect_references(operation),
        attachments=_attachment.inspect_references(operation),
    )


def collect_dependency_operation_ids(
    operation: OperationRecord,
    *,
    references: DependencyReferences | None = None,
) -> tuple[str, ...]:
    """Return distinct producer operation IDs in stable reference order."""

    collected = references or collect_dependency_references(operation)
    values = [reference.operation_id for reference in collected.issues]
    values.extend(
        reference.operation_id
        for reference in collected.attachments.references
    )
    return tuple(dict.fromkeys(values))


def validate_dependency_syntax(
    operation: OperationRecord,
    *,
    allow_unresolved: bool,
    references: DependencyReferences | None = None,
) -> None:
    """Assert issue and attachment syntax for one lifecycle boundary."""

    collected = references or collect_dependency_references(operation)
    _issue.validate_reference_syntax(
        operation, allow_unresolved=allow_unresolved
    )
    _attachment.validate_reference_syntax(
        operation,
        collected.attachments,
        allow_unresolved=allow_unresolved,
    )


def validate_dependency_contract(
    operation: OperationRecord,
    states: Mapping[str, DependencyState],
    *,
    references: DependencyReferences | None = None,
) -> None:
    """Assert producer existence, capability, context, and lifecycle."""

    collected = references or collect_dependency_references(operation)
    _issue.validate_reference_contract(collected.issues, states)
    _attachment.validate_reference_contract(
        operation, collected.attachments.references, states
    )


def collect_dependency_resolutions(
    operation: OperationRecord,
    states: Mapping[str, DependencyState],
    *,
    references: DependencyReferences | None = None,
) -> DependencyResolutions:
    """Collect currently available results after contract validation."""

    collected = references or collect_dependency_references(operation)
    return DependencyResolutions(
        issue_keys=_issue.collect_resolutions(collected.issues, states),
        attachments=_attachment.collect_resolutions(
            collected.attachments.references, states
        ),
    )


def materialize_dependency_references(
    operation: OperationRecord, resolutions: DependencyResolutions
) -> OperationRecord:
    """Materialize issue targets followed by embedded attachment references."""

    materialized = _issue.materialize_references(
        operation, resolutions.issue_keys
    )
    return _attachment.materialize_references(
        materialized, resolutions.attachments
    )


def dependency_resolution_events(
    operation: OperationRecord,
    resolutions: DependencyResolutions,
    *,
    references: DependencyReferences | None = None,
) -> tuple[DependencyResolutionEvent, ...]:
    """Build stable enqueue-time events for immediately resolved references."""

    collected = references or collect_dependency_references(operation)
    events: list[DependencyResolutionEvent] = []
    for producer_id, new_issue_key in resolutions.issue_keys.items():
        fields = [
            reference.path
            for reference in collected.issues
            if reference.operation_id == producer_id
        ]
        events.append(
            DependencyResolutionEvent(
                action_code="issue_key_resolved",
                producer_operation_id=producer_id,
                data={"new_issue_key": new_issue_key, "fields": fields},
            )
        )
    for producer_id, attachment_result in resolutions.attachments.items():
        paths = list(
            dict.fromkeys(
                reference.path
                for reference in collected.attachments.references
                if reference.operation_id == producer_id
            )
        )
        events.append(
            DependencyResolutionEvent(
                action_code="attachment_resolved",
                producer_operation_id=producer_id,
                data={
                    "attachment_result": attachment_result.model_dump(
                        mode="json"
                    ),
                    "paths": paths,
                },
            )
        )
    return tuple(events)


def dependency_state_map(
    operation: OperationRecord,
    states: Mapping[str, DependencyState],
) -> dict[str, str]:
    """Return public lifecycle labels for all referenced producer IDs."""

    return {
        operation_id: states.get(
            operation_id,
            DependencyState(
                operation_id=operation_id,
                operation_type=None,
                state="unknown",
            ),
        ).state
        for operation_id in collect_dependency_operation_ids(operation)
    }


def submission_block_reason(
    operation: OperationRecord,
    states: Mapping[str, DependencyState],
) -> str | None:
    """Return the first syntax, issue, or attachment submission blocker."""

    references = collect_dependency_references(operation)
    try:
        validate_dependency_syntax(
            operation,
            allow_unresolved=True,
            references=references,
        )
    except DependencyError as exc:
        return str(exc)
    return _issue.submission_block_reason(
        references.issues, states
    ) or _attachment.submission_block_reason(
        operation, references.attachments, states
    )


def classify_dependency_state(state: DependencyState) -> DependencyStateDecision:
    """Classify a producer fact as materializable, waiting, or unavailable."""

    if _issue.is_producer(state.operation_type):
        resolution = _issue.resolution_from_state(state)
    elif _attachment.is_producer(state.operation_type):
        resolution = _attachment.resolution_from_state(state)
    else:
        return UnavailableDependency()
    if resolution is not None:
        return MaterializableDependency(resolution)
    if state.active:
        return WaitingDependency()
    return UnavailableDependency()


def dependency_resolution_from_result(
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


def materialize_dependency_resolution(
    operation: OperationRecord, resolution: DependencyResolution
) -> OperationRecord:
    """Materialize one typed result into one consumer snapshot."""

    if isinstance(resolution, IssueDependencyResolution):
        return _issue.materialize_references(
            operation,
            {resolution.producer_operation_id: resolution.issue_key},
        )
    if isinstance(resolution, AttachmentDependencyResolution):
        return _attachment.materialize_references(
            operation,
            {
                resolution.producer_operation_id: (
                    resolution.attachment_result
                )
            },
        )
    assert_never(resolution)


def dependency_resolution_reference_paths(
    operation: OperationRecord, resolution: DependencyResolution
) -> tuple[str, ...]:
    """Return consumer paths targeted by one typed producer result."""

    references = collect_dependency_references(operation)
    if isinstance(resolution, IssueDependencyResolution):
        return tuple(
            reference.path
            for reference in references.issues
            if reference.operation_id == resolution.producer_operation_id
        )
    if isinstance(resolution, AttachmentDependencyResolution):
        return tuple(
            dict.fromkeys(
                reference.path
                for reference in references.attachments.references
                if reference.operation_id
                == resolution.producer_operation_id
            )
        )
    assert_never(resolution)


def pending_issue_group_id(operation: OperationRecord) -> str | None:
    """Return the pending Jira issue group implied by one operation."""

    return _issue.pending_group_id(operation)


def description_reference_paths(
    operation_type: str,
) -> tuple[str, ...]:
    """Return description paths from the attachment reference path source."""

    return _attachment.description_paths(operation_type)
