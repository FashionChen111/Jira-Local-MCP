"""Pure rules for explicit operation-to-operation Jira issue references."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from core.operations.errors import DependencyError
from core.operations.models import (
    DependencyState,
    IssueDependencyResolution,
    OperationRecord,
    OperationType,
)
from core.operations.rules.validation import is_jira_issue_key, is_operation_id


_PRODUCER_OPERATION_TYPES = frozenset({"create_issue", "clone_issue"})


@dataclass(frozen=True)
class IssueReference:
    """One explicit Jira issue target backed by another operation."""

    referenced_operation_id: str
    path: Literal["issue_key", "target_issue_key"]


def is_producer(operation_type: OperationType | None) -> bool:
    """Return whether an operation type produces a new Jira issue key."""

    return operation_type in _PRODUCER_OPERATION_TYPES


def collect_references(operation: OperationRecord) -> tuple[IssueReference, ...]:
    """Collect valid operation IDs from supported top-level issue targets."""

    result: list[IssueReference] = []
    if is_operation_id(operation.issue_key):
        result.append(IssueReference(operation.issue_key, "issue_key"))
    if (
        operation.operation_type == "add_issuelink"
        and is_operation_id(operation.target_issue_key)
    ):
        result.append(
            IssueReference(operation.target_issue_key, "target_issue_key")
        )
    return tuple(result)


def validate_reference_syntax(
    operation: OperationRecord, *, allow_unresolved: bool
) -> None:
    """Assert target applicability and lifecycle-specific Jira key syntax."""

    def valid_target(value: str | None) -> bool:
        return is_jira_issue_key(value) or (
            allow_unresolved and is_operation_id(value)
        )

    if operation.operation_type == "create_issue":
        if operation.issue_key is not None:
            raise DependencyError("create_issue 不接受 issue_key")
    elif operation.operation_type == "clone_issue":
        if not is_jira_issue_key(operation.issue_key):
            raise DependencyError("clone_issue.issue_key 必须是真实 Jira Key")
    elif not valid_target(operation.issue_key):
        expected = (
            "真实 Jira Key 或待创建 operation ID"
            if allow_unresolved
            else "真实 Jira Key"
        )
        raise DependencyError(
            f"{operation.operation_type}.issue_key 必须是{expected}"
        )

    if operation.operation_type == "add_issuelink":
        if not valid_target(operation.target_issue_key):
            expected = (
                "真实 Jira Key 或待创建 operation ID"
                if allow_unresolved
                else "真实 Jira Key"
            )
            raise DependencyError(
                f"add_issuelink.target_issue_key 必须是{expected}"
            )
    elif operation.target_issue_key is not None:
        raise DependencyError(
            f"{operation.operation_type} 不接受 target_issue_key"
        )


def reference_problem(
    references: tuple[IssueReference, ...],
    states: Mapping[str, DependencyState],
    *,
    allow_available_resolution: bool,
) -> str | None:
    """Return the first issue-reference state problem for one lifecycle."""

    for reference in references:
        referenced_operation_id = reference.referenced_operation_id
        state = states.get(referenced_operation_id)
        if state is None or state.state == "unknown":
            return f"引用的 operation 不存在：{referenced_operation_id}"
        if not is_producer(state.operation_type):
            return (
                "引用的 operation 不是 create_issue/clone_issue："
                f"{referenced_operation_id}"
            )
        if state.state in {"success", "warning"}:
            if not is_jira_issue_key(state.new_issue_key):
                return (
                    "引用的事务生产者缺少有效 Jira Key："
                    f"{referenced_operation_id}"
                )
            if not allow_available_resolution:
                return (
                    "依赖的创建/克隆事务已成功但引用尚未物化，数据不一致："
                    f"{referenced_operation_id}"
                )
        elif not state.active:
            return (
                "引用的事务生产者已处于不可用状态 "
                f"{state.state}：{referenced_operation_id}"
            )
    return None


def validate_reference_contract(
    references: tuple[IssueReference, ...],
    states: Mapping[str, DependencyState],
) -> None:
    """Assert producer existence, capability, lifecycle, and result integrity."""

    problem = reference_problem(
        references,
        states,
        allow_available_resolution=True,
    )
    if problem is not None:
        raise DependencyError(problem)


def materialize_references(
    operation: OperationRecord,
    issue_keys_by_producer: Mapping[str, str],
) -> tuple[OperationRecord, tuple[tuple[str, tuple[str, ...]], ...]]:
    """Replace available issue targets and report actual producer fields."""

    issue_key = operation.issue_key
    target_issue_key = operation.target_issue_key
    fields_by_producer: dict[str, list[str]] = {}

    replace_issue_key = (
        isinstance(issue_key, str) and issue_key in issue_keys_by_producer
    )
    if replace_issue_key and issue_key is not None:
        fields_by_producer.setdefault(issue_key, []).append("issue_key")
        issue_key = issue_keys_by_producer[issue_key]

    replace_target_issue_key = (
        isinstance(target_issue_key, str)
        and target_issue_key in issue_keys_by_producer
    )
    if replace_target_issue_key and target_issue_key is not None:
        fields_by_producer.setdefault(target_issue_key, []).append(
            "target_issue_key"
        )
        target_issue_key = issue_keys_by_producer[target_issue_key]

    if not replace_issue_key and not replace_target_issue_key:
        return operation, ()

    materialized = operation.with_targets(
        issue_key=issue_key,
        target_issue_key=target_issue_key,
        replace_issue_key=replace_issue_key,
        replace_target_issue_key=replace_target_issue_key,
    )
    changes = tuple(
        (producer_operation_id, tuple(fields_by_producer[producer_operation_id]))
        for producer_operation_id in issue_keys_by_producer
        if producer_operation_id in fields_by_producer
    )
    return materialized, changes


def comparable_issue_target(
    value: str | None, states: Mapping[str, DependencyState]
) -> str | None:
    """Return a concrete key when available, otherwise preserve the target ID."""

    if is_jira_issue_key(value):
        return value
    if is_operation_id(value):
        state = states.get(value)
        if (
            state is not None
            and is_producer(state.operation_type)
            and is_jira_issue_key(state.new_issue_key)
        ):
            return state.new_issue_key
    return value


def resolution_from_state(
    state: DependencyState,
) -> IssueDependencyResolution | None:
    """Project a valid successful state to its typed issue resolution."""

    if (
        is_producer(state.operation_type)
        and state.state in {"success", "warning"}
        and is_jira_issue_key(state.new_issue_key)
    ):
        return IssueDependencyResolution(
            producer_operation_id=state.operation_id,
            issue_key=state.new_issue_key,
        )
    return None


def pending_issue_group_id(operation: OperationRecord) -> str | None:
    """Return the pending-issue group ID implied by one operation."""

    if is_producer(operation.operation_type):
        return operation.operation_id
    if is_operation_id(operation.issue_key):
        return operation.issue_key
    return None
