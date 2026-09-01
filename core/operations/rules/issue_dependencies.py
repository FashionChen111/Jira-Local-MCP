"""Pure rules for direct operation-to-operation Jira issue references."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from core.operations.errors import DependencyError
from core.operations.models import DependencyState, OperationRecord
from core.operations.rules.identifiers import is_jira_issue_key, is_operation_id


ISSUE_PRODUCER_OPERATION_TYPES = frozenset({"create_issue", "clone_issue"})


@dataclass(frozen=True)
class IssueReference:
    """One explicit top-level target backed by an issue-producing operation."""

    operation_id: str
    path: Literal["issue_key", "target_issue_key"]


def references(operation: OperationRecord) -> tuple[IssueReference, ...]:
    """Return issue references in stable top-level field order."""

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


def validate_target_syntax(
    operation: OperationRecord, *, allow_operation_ids: bool
) -> None:
    """Validate the two explicit targets for the operation lifecycle."""

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
        expected = (
            "真实 Jira Key 或待创建 operation ID"
            if allow_operation_ids
            else "真实 Jira Key"
        )
        raise DependencyError(
            f"{operation.operation_type}.issue_key 必须是{expected}"
        )

    if operation.operation_type == "add_issuelink":
        if not valid_target(operation.target_issue_key):
            expected = (
                "真实 Jira Key 或待创建 operation ID"
                if allow_operation_ids
                else "真实 Jira Key"
            )
            raise DependencyError(
                f"add_issuelink.target_issue_key 必须是{expected}"
            )
    elif operation.target_issue_key is not None:
        raise DependencyError(
            f"{operation.operation_type} 不接受 target_issue_key"
        )


def validate_references(
    operation: OperationRecord,
    states: Mapping[str, DependencyState],
) -> dict[str, str]:
    """Validate referenced issue producers and return available Jira keys."""

    resolved: dict[str, str] = {}
    for reference in references(operation):
        producer_id = reference.operation_id
        state = states.get(producer_id)
        if state is None or state.state == "unknown":
            raise DependencyError(f"引用的 operation 不存在：{producer_id}")
        if state.operation_type not in ISSUE_PRODUCER_OPERATION_TYPES:
            raise DependencyError(
                f"引用的 operation 不是 create_issue/clone_issue：{producer_id}"
            )
        if state.state == "success":
            if not is_jira_issue_key(state.new_issue_key):
                raise DependencyError(
                    f"引用的事务生产者缺少有效 Jira Key：{producer_id}"
                )
            resolved[producer_id] = state.new_issue_key
            continue
        if not state.active:
            raise DependencyError(
                f"引用的事务生产者已处于不可用状态 {state.state}：{producer_id}"
            )
    return resolved


def materialize_references(
    operation: OperationRecord, resolved_keys: Mapping[str, str]
) -> OperationRecord:
    """Replace explicit targets whose producer results are available."""

    issue_key = operation.issue_key
    target_issue_key = operation.target_issue_key
    replace_issue = isinstance(issue_key, str) and issue_key in resolved_keys
    replace_target = (
        isinstance(target_issue_key, str)
        and target_issue_key in resolved_keys
    )
    if replace_issue and issue_key is not None:
        issue_key = resolved_keys[issue_key]
    if replace_target and target_issue_key is not None:
        target_issue_key = resolved_keys[target_issue_key]
    return operation.with_targets(
        issue_key=issue_key,
        target_issue_key=target_issue_key,
        replace_issue_key=replace_issue,
        replace_target_issue_key=replace_target,
    )


def block_reason(
    operation: OperationRecord,
    states: Mapping[str, DependencyState],
) -> str | None:
    """Describe the first issue dependency state preventing submission."""

    for reference in references(operation):
        operation_id = reference.operation_id
        state = states.get(operation_id)
        if state is None or state.state == "unknown":
            return f"依赖的 operation 不存在：{operation_id}"
        if state.operation_type not in ISSUE_PRODUCER_OPERATION_TYPES:
            return f"依赖的 operation 不是创建/克隆事务：{operation_id}"
        if state.state in {"failed", "dependency_failed", "abandoned"}:
            return f"依赖的创建/克隆事务已失败：{operation_id}"
        if state.state == "indeterminate":
            return f"依赖的创建/克隆事务结果待人工处置：{operation_id}"
        if state.state == "success" and not is_jira_issue_key(
            state.new_issue_key
        ):
            return f"依赖的创建/克隆事务缺少 Jira Key：{operation_id}"
    return None
