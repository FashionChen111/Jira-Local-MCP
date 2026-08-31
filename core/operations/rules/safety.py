"""Allowlist, deletion defence, risk policy, and final Jira target gates."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from pydantic import JsonValue

from core.operations.models import OperationDraft, OperationRecord, RiskLevel
from core.operations.rules.dependencies import DependencyError, validate_target_syntax


ALLOWED_OPERATION_TYPES = frozenset(
    {
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
    }
)
_LOW_RISK = frozenset({"add_comment", "add_worklog", "add_issuelink"})
_MEDIUM_RISK = frozenset(
    {
        "create_issue",
        "clone_issue",
        "update_field",
        "update_custom_field",
        "add_attachment",
    }
)
_CLOSE_KEYWORDS = ("关闭", "解决", "完成", "close", "resolve", "done", "cancel")
_DELETE_PATTERN = re.compile(r"delete|remove|purge|删除|移除", re.IGNORECASE)


class OperationSafetyError(ValueError):
    """An operation was rejected by a stable operations safety policy."""


def classify_base_risk(
    operation_type: str, payload: Mapping[str, JsonValue]
) -> tuple[RiskLevel, str]:
    """Classify the operation before selection-level escalation."""

    if operation_type in _LOW_RISK:
        return "low", "低风险：仅新增内容，不改动既有数据"
    if operation_type == "transition_issue":
        name = str(payload.get("transition_name", ""))
        if any(keyword in name.lower() for keyword in _CLOSE_KEYWORDS):
            return "high", f"高风险：状态流转“{name}”可能关闭或解决事务"
        return "high", f"高风险：状态流转“{name}”会改变工作流状态"
    if operation_type == "move_issue":
        return "high", "高风险：移动事务会改变项目归属"
    if operation_type == "change_type":
        return "high", "高风险：变更事务类型可能影响字段和层级"
    if operation_type in _MEDIUM_RISK:
        return "medium", "中风险：创建或修改事务数据"
    return "high", "未知操作类型，按最高风险处理"


def _contains_delete_semantics(value: object) -> bool:
    if isinstance(value, str):
        return bool(_DELETE_PATTERN.search(value))
    if isinstance(value, dict):
        return any(
            _DELETE_PATTERN.search(key) or _contains_delete_semantics(item)
            for key, item in value.items()
            if isinstance(key, str)
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_delete_semantics(item) for item in value)
    return False


def assert_allowed_and_non_delete(
    operation_type: str, payload: Mapping[str, JsonValue]
) -> None:
    """Enforce the immutable operation allowlist and deletion-semantic defence."""

    if operation_type not in ALLOWED_OPERATION_TYPES:
        raise OperationSafetyError(
            f"操作类型“{operation_type}”不在允许白名单内"
        )
    if _DELETE_PATTERN.search(operation_type) or _contains_delete_semantics(payload):
        raise OperationSafetyError("检测到删除语义，拒绝创建或执行 operation")
    duplicate_targets = {"issue_key", "target_issue_key"} & payload.keys()
    if duplicate_targets:
        names = "、".join(sorted(duplicate_targets))
        raise OperationSafetyError(f"payload 不得重复保存顶层目标字段：{names}")


def validate_for_enqueue(draft: OperationDraft) -> tuple[RiskLevel, str]:
    """Validate one immutable queue snapshot without querying Jira."""

    assert_allowed_and_non_delete(draft.operation_type, draft.payload)
    provisional = OperationRecord(
        operation_id="op-00000000000000000000000000000000",
        operation_type=draft.operation_type,
        issue_key=draft.issue_key,
        target_issue_key=draft.target_issue_key,
        risk_level="low",
        payload=draft.payload,
        description=draft.description,
        source=draft.source,
        created_at=0,
        attempt_no=1,
    )
    try:
        validate_target_syntax(provisional, allow_operation_ids=True)
    except DependencyError as exc:
        raise OperationSafetyError(str(exc)) from exc
    return classify_base_risk(draft.operation_type, draft.payload)


def assert_safe_for_submit(operation: OperationRecord) -> None:
    """Recheck immutable safety rules while direct dependencies may remain."""

    assert_allowed_and_non_delete(operation.operation_type, operation.payload)
    try:
        validate_target_syntax(operation, allow_operation_ids=True)
    except DependencyError as exc:
        raise OperationSafetyError(str(exc)) from exc


def classify_submission_risks(
    operations: Sequence[OperationRecord],
) -> dict[str, RiskLevel]:
    """Escalate dense field edits to one Jira issue while retaining base risks."""

    risks: dict[str, RiskLevel] = {
        operation.operation_id: operation.risk_level for operation in operations
    }
    counts: dict[str, int] = {}
    for operation in operations:
        if operation.operation_type in {"update_field", "update_custom_field"}:
            key = operation.issue_key
            if key is not None:
                counts[key] = counts.get(key, 0) + 1
    hot_keys = {key for key, count in counts.items() if count >= 5}
    for operation in operations:
        if (
            operation.operation_type in {"update_field", "update_custom_field"}
            and operation.issue_key in hot_keys
        ):
            risks[operation.operation_id] = "high"
    return risks


def assert_safe_to_execute(operation: OperationRecord) -> None:
    """Final pure gate: Jira execution accepts only concrete target keys."""

    assert_allowed_and_non_delete(operation.operation_type, operation.payload)
    try:
        validate_target_syntax(operation, allow_operation_ids=False)
    except DependencyError as exc:
        raise OperationSafetyError(str(exc)) from exc
