"""Dependency facade combining issue targets and attachment text references."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from core.operations.errors import DependencyError
from core.operations.models import AttachmentResult, DependencyState, OperationRecord
from core.operations.rules import attachment_dependencies, issue_dependencies
from core.operations.rules.attachment_dependencies import (
    ATTACHMENT_PRODUCER_OPERATION_TYPES,
)
from core.operations.rules.identifiers import is_jira_issue_key, is_operation_id
from core.operations.rules.issue_dependencies import ISSUE_PRODUCER_OPERATION_TYPES


PRODUCER_OPERATION_TYPES = (
    ISSUE_PRODUCER_OPERATION_TYPES | ATTACHMENT_PRODUCER_OPERATION_TYPES
)


@dataclass(frozen=True)
class DependencyResolutions:
    """Already available dependency results for one consumer operation."""

    issue_keys: dict[str, str]
    attachments: dict[str, AttachmentResult]


@dataclass(frozen=True)
class DependencyResolutionEvent:
    """One audit event produced by enqueue-time dependency materialization."""

    action_code: str
    producer_operation_id: str
    data: dict[str, object]


def dependency_operation_ids(operation: OperationRecord) -> tuple[str, ...]:
    """Return distinct issue and attachment producer IDs in stable order."""

    values = [
        reference.operation_id
        for reference in issue_dependencies.references(operation)
    ]
    values.extend(
        reference.operation_id
        for reference in attachment_dependencies.references(operation)
    )
    return tuple(dict.fromkeys(values))


def validate_references(
    operation: OperationRecord,
    states: Mapping[str, DependencyState],
) -> DependencyResolutions:
    """Validate both dependency domains and collect completed results."""

    return DependencyResolutions(
        issue_keys=issue_dependencies.validate_references(operation, states),
        attachments=attachment_dependencies.validate_references(operation, states),
    )


def materialize_references(
    operation: OperationRecord, resolutions: DependencyResolutions
) -> OperationRecord:
    """Materialize issue targets followed by embedded attachment references."""

    materialized = issue_dependencies.materialize_references(
        operation, resolutions.issue_keys
    )
    return attachment_dependencies.materialize_references(
        materialized, resolutions.attachments
    )


def resolution_events(
    operation: OperationRecord, resolutions: DependencyResolutions
) -> tuple[DependencyResolutionEvent, ...]:
    """Build stable enqueue-time audit events for resolved references."""

    events: list[DependencyResolutionEvent] = []
    issue_refs = issue_dependencies.references(operation)
    for producer_id, new_issue_key in resolutions.issue_keys.items():
        fields = [
            reference.path
            for reference in issue_refs
            if reference.operation_id == producer_id
        ]
        events.append(
            DependencyResolutionEvent(
                action_code="issue_key_resolved",
                producer_operation_id=producer_id,
                data={
                    "new_issue_key": new_issue_key,
                    "fields": fields,
                },
            )
        )
    attachment_refs = attachment_dependencies.references(operation)
    for producer_id, attachment_result in resolutions.attachments.items():
        paths = list(
            dict.fromkeys(
                reference.path
                for reference in attachment_refs
                if reference.operation_id == producer_id
            )
        )
        events.append(
            DependencyResolutionEvent(
                action_code="attachment_resolved",
                producer_operation_id=producer_id,
                data={
                    "attachment_result": attachment_result.model_dump(mode="json"),
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
        for operation_id in dependency_operation_ids(operation)
    }


def submission_block_reason(
    operation: OperationRecord,
    states: Mapping[str, DependencyState],
) -> str | None:
    """Return the first issue or attachment dependency block reason."""

    return issue_dependencies.block_reason(
        operation, states
    ) or attachment_dependencies.block_reason(operation, states)


def has_usable_success_result(state: DependencyState) -> bool:
    """Return whether a successful producer exposes its required typed result."""

    if state.operation_type in ISSUE_PRODUCER_OPERATION_TYPES:
        return is_jira_issue_key(state.new_issue_key)
    if state.operation_type in ATTACHMENT_PRODUCER_OPERATION_TYPES:
        return state.attachment_result is not None
    return False


# Compatibility wrappers for existing internal callers while the facade remains stable.
def dependency_fields(operation: OperationRecord) -> tuple[tuple[str, str], ...]:
    """Return the former issue-only ``(field, operation_id)`` projection."""

    return tuple(
        (reference.path, reference.operation_id)
        for reference in issue_dependencies.references(operation)
    )


materialize_targets = issue_dependencies.materialize_references
validate_referenced_producers = issue_dependencies.validate_references
validate_target_syntax = issue_dependencies.validate_target_syntax
