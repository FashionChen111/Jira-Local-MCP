"""Pure, explicit rules for direct operation-to-operation Jira key references."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TypeGuard

from core.operations.models import OperationRecord, ProducerState


PRODUCER_OPERATION_TYPES = frozenset({"create_issue", "clone_issue"})
_OPERATION_ID = re.compile(r"^op-[0-9a-f]{32}$")
_JIRA_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$")
_ACTIVE_PRODUCER_STATES = frozenset(
    {"queued", "staged", "executing", "held", "retry_pending"}
)


class DependencyError(ValueError):
    """A direct issue-key dependency violates the operations contract."""


def is_operation_id(value: object) -> TypeGuard[str]:
    """Return whether *value* is a server-generated operation identifier."""

    return isinstance(value, str) and bool(_OPERATION_ID.fullmatch(value))


def is_jira_issue_key(value: object) -> TypeGuard[str]:
    """Return whether *value* has the accepted Jira issue-key shape."""

    return isinstance(value, str) and bool(_JIRA_ISSUE_KEY.fullmatch(value))


def dependency_fields(operation: OperationRecord) -> tuple[tuple[str, str], ...]:
    """Return explicit target fields that currently contain producer IDs."""

    fields: list[tuple[str, str]] = []
    if is_operation_id(operation.issue_key):
        fields.append(("issue_key", operation.issue_key))
    if (
        operation.operation_type == "add_issuelink"
        and is_operation_id(operation.target_issue_key)
    ):
        fields.append(("target_issue_key", operation.target_issue_key))
    return tuple(fields)


def dependency_operation_ids(operation: OperationRecord) -> tuple[str, ...]:
    """Return distinct producer IDs in stable field order."""

    return tuple(dict.fromkeys(value for _, value in dependency_fields(operation)))


def validate_target_syntax(
    operation: OperationRecord, *, allow_operation_ids: bool
) -> None:
    """Validate the two explicit targets for the operation type and lifecycle."""

    def valid_target(value: str | None) -> bool:
        return is_jira_issue_key(value) or (
            allow_operation_ids and is_operation_id(value)
        )

    if operation.operation_type == "create_issue":
        if operation.issue_key is not None:
            raise DependencyError("create_issue 不接受 issue_key")
    elif operation.operation_type == "clone_issue":
        if not is_jira_issue_key(operation.issue_key):
            raise DependencyError("clone_issue.issue_key 必须是真实 Jira Key")
    elif not valid_target(operation.issue_key):
        expected = "真实 Jira Key 或待创建 operation ID" if allow_operation_ids else "真实 Jira Key"
        raise DependencyError(f"{operation.operation_type}.issue_key 必须是{expected}")

    if operation.operation_type == "add_issuelink":
        if not valid_target(operation.target_issue_key):
            expected = "真实 Jira Key 或待创建 operation ID" if allow_operation_ids else "真实 Jira Key"
            raise DependencyError(f"add_issuelink.target_issue_key 必须是{expected}")
    elif operation.target_issue_key is not None:
        raise DependencyError(
            f"{operation.operation_type} 不接受 target_issue_key"
        )


def validate_referenced_producers(
    operation: OperationRecord,
    states: Mapping[str, ProducerState],
) -> dict[str, str]:
    """Validate referenced producers and return already available Jira keys."""

    resolved: dict[str, str] = {}
    for producer_id in dependency_operation_ids(operation):
        state = states.get(producer_id)
        if state is None or state.state == "unknown":
            raise DependencyError(f"引用的 operation 不存在：{producer_id}")
        if state.operation_type not in PRODUCER_OPERATION_TYPES:
            raise DependencyError(
                f"引用的 operation 不是 create_issue/clone_issue：{producer_id}"
            )
        if state.state == "success":
            if not is_jira_issue_key(state.new_issue_key):
                raise DependencyError(
                    f"引用的生产者缺少有效 Jira Key：{producer_id}"
                )
            resolved[producer_id] = state.new_issue_key
            continue
        if state.state not in _ACTIVE_PRODUCER_STATES:
            raise DependencyError(
                f"引用的生产者已处于不可用状态 {state.state}：{producer_id}"
            )
    return resolved


def materialize_targets(
    operation: OperationRecord, resolved_keys: Mapping[str, str]
) -> OperationRecord:
    """Replace either explicit target when its producer result is available."""

    issue_key = operation.issue_key
    target_issue_key = operation.target_issue_key
    replace_issue = bool(issue_key in resolved_keys)
    replace_target = bool(target_issue_key in resolved_keys)
    if replace_issue:
        issue_key = resolved_keys[operation.issue_key]  # type: ignore[index]
    if replace_target:
        target_issue_key = resolved_keys[operation.target_issue_key]  # type: ignore[index]
    return operation.with_targets(
        issue_key=issue_key,
        target_issue_key=target_issue_key,
        replace_issue_key=replace_issue,
        replace_target_issue_key=replace_target,
    )


def dependency_state_map(
    operation: OperationRecord,
    states: Mapping[str, ProducerState],
) -> dict[str, str]:
    """Return public lifecycle labels for this operation's producer IDs."""

    return {
        operation_id: states.get(
            operation_id,
            ProducerState(
                operation_id=operation_id,
                operation_type=None,
                state="unknown",
            ),
        ).state
        for operation_id in dependency_operation_ids(operation)
    }


def submission_block_reason(
    operation: OperationRecord,
    states: Mapping[str, ProducerState],
) -> str | None:
    """Describe the first dependency state that prevents submission."""

    for operation_id in dependency_operation_ids(operation):
        state = states.get(operation_id)
        if state is None or state.state == "unknown":
            return f"依赖的 operation 不存在：{operation_id}"
        if state.operation_type not in PRODUCER_OPERATION_TYPES:
            return f"依赖的 operation 不是创建/克隆事务：{operation_id}"
        if state.state in {"failed", "dependency_failed", "abandoned"}:
            return f"依赖的创建/克隆事务已失败：{operation_id}"
        if state.state == "indeterminate":
            return f"依赖的创建/克隆事务结果待人工处置：{operation_id}"
        if state.state == "success" and not is_jira_issue_key(state.new_issue_key):
            return f"依赖的创建/克隆事务缺少 Jira Key：{operation_id}"
    return None
