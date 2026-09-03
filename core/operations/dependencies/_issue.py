"""Pure rules for explicit operation-to-operation Jira issue references."""

from __future__ import annotations

from collections.abc import Mapping

from core.operations.dependencies._types import IssueReference
from core.operations.errors import DependencyError
from core.operations.models import (
    DependencyState,
    IssueDependencyResolution,
    OperationRecord,
    OperationType,
)
from core.operations.rules.identifiers import is_jira_issue_key, is_operation_id


_PRODUCER_OPERATION_TYPES = frozenset({"create_issue", "clone_issue"})


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


def validate_reference_contract(
    references: tuple[IssueReference, ...],
    states: Mapping[str, DependencyState],
) -> None:
    """Assert existence, capability, lifecycle, and result integrity."""

    for reference in references:
        producer_id = reference.operation_id
        state = states.get(producer_id)
        if state is None or state.state == "unknown":
            raise DependencyError(f"引用的 operation 不存在：{producer_id}")
        if not is_producer(state.operation_type):
            raise DependencyError(
                f"引用的 operation 不是 create_issue/clone_issue：{producer_id}"
            )
        if state.state == "success" and not is_jira_issue_key(
            state.new_issue_key
        ):
            raise DependencyError(
                f"引用的事务生产者缺少有效 Jira Key：{producer_id}"
            )
        if state.state != "success" and not state.active:
            raise DependencyError(
                f"引用的事务生产者已处于不可用状态 {state.state}：{producer_id}"
            )


def collect_resolutions(
    references: tuple[IssueReference, ...],
    states: Mapping[str, DependencyState],
) -> dict[str, str]:
    """Collect available Jira keys after the reference contract is validated."""

    resolved: dict[str, str] = {}
    for reference in references:
        state = states.get(reference.operation_id)
        if (
            state is not None
            and state.state == "success"
            and is_jira_issue_key(state.new_issue_key)
        ):
            resolved[reference.operation_id] = state.new_issue_key
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


def submission_block_reason(
    references: tuple[IssueReference, ...],
    states: Mapping[str, DependencyState],
) -> str | None:
    """Describe the first issue dependency preventing submission."""

    for reference in references:
        operation_id = reference.operation_id
        state = states.get(operation_id)
        if state is None or state.state == "unknown":
            return f"依赖的 operation 不存在：{operation_id}"
        if not is_producer(state.operation_type):
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


def comparable_target(
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
        and state.state == "success"
        and is_jira_issue_key(state.new_issue_key)
    ):
        return IssueDependencyResolution(
            producer_operation_id=state.operation_id,
            issue_key=state.new_issue_key,
        )
    return None


def pending_group_id(operation: OperationRecord) -> str | None:
    """Return the pending-issue group ID implied by one operation."""

    if is_producer(operation.operation_type):
        return operation.operation_id
    if is_operation_id(operation.issue_key):
        return operation.issue_key
    return None
