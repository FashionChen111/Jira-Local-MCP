"""Allowlist, deletion defence, risk policy, and final Jira target gates."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from core.operations.dependencies import validate_dependency_syntax
from core.operations.models import OperationDraft, OperationRecord, RiskLevel
from core.operations.errors import DependencyError


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
_CUSTOM_FIELD_PATTERN = re.compile(r"customfield_\d+\Z")

_PayloadValueKind = Literal[
    "string",
    "nullable_string",
    "boolean",
    "mapping",
    "nonempty_mapping",
]


@dataclass(frozen=True)
class _PayloadShape:
    """Minimal handler-facing shape for one persisted payload."""

    required: Mapping[str, _PayloadValueKind]
    optional: Mapping[str, _PayloadValueKind]


_PAYLOAD_SHAPES: Mapping[str, _PayloadShape] = {
    "create_issue": _PayloadShape(
        required={
            "project_key": "string",
            "issue_type": "string",
            "summary": "string",
        },
        optional={
            "description": "nullable_string",
            "priority": "nullable_string",
            "assignee": "nullable_string",
            "extra_fields": "mapping",
        },
    ),
    "clone_issue": _PayloadShape(
        required={},
        optional={
            "summary": "nullable_string",
            "clone_subtasks": "boolean",
            "clone_attachments": "boolean",
        },
    ),
    "move_issue": _PayloadShape(
        required={"target_project_key": "string"},
        optional={"target_issue_type": "nullable_string"},
    ),
    "change_type": _PayloadShape(
        required={"target_issue_type": "string"},
        optional={"fields_mapping": "mapping"},
    ),
    "transition_issue": _PayloadShape(
        required={"transition_name": "string"},
        optional={
            "comment": "nullable_string",
            "fields": "mapping",
        },
    ),
    "update_field": _PayloadShape(
        required={"fields": "nonempty_mapping"},
        optional={},
    ),
    "update_custom_field": _PayloadShape(
        required={"custom_fields": "nonempty_mapping"},
        optional={},
    ),
    "add_attachment": _PayloadShape(
        required={"file_path": "string"},
        optional={"file_name": "nullable_string"},
    ),
    "add_comment": _PayloadShape(
        required={"body": "string"},
        optional={},
    ),
    "add_worklog": _PayloadShape(
        required={"time_spent": "string"},
        optional={
            "comment": "nullable_string",
            "started": "nullable_string",
        },
    ),
    "add_issuelink": _PayloadShape(
        required={"link_type": "string"},
        optional={},
    ),
}

_PAYLOAD_KIND_LABELS: Mapping[_PayloadValueKind, str] = {
    "string": "字符串",
    "nullable_string": "字符串或 null",
    "boolean": "布尔值",
    "mapping": "对象",
    "nonempty_mapping": "非空对象",
}


class OperationSafetyError(ValueError):
    """An operation was rejected by a stable operations safety policy."""


def _matches_payload_kind(value: JsonValue, kind: _PayloadValueKind) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "nullable_string":
        return value is None or isinstance(value, str)
    if kind == "boolean":
        return type(value) is bool
    if kind == "mapping":
        return isinstance(value, dict)
    return isinstance(value, dict) and bool(value)


def assert_valid_payload_shape(
    operation_type: str, payload: Mapping[str, JsonValue]
) -> None:
    """Validate only the stable local shape consumed by Jira handlers."""

    shape = _PAYLOAD_SHAPES.get(operation_type)
    if shape is None:
        raise OperationSafetyError(
            f"操作类型“{operation_type}”没有已定义的 payload 形态"
        )

    expected = shape.required.keys() | shape.optional.keys()
    unexpected = sorted(payload.keys() - expected)
    if unexpected:
        raise OperationSafetyError(
            f"{operation_type}.payload 包含未定义字段：{'、'.join(unexpected)}"
        )
    missing = sorted(shape.required.keys() - payload.keys())
    if missing:
        raise OperationSafetyError(
            f"{operation_type}.payload 缺少必需字段：{'、'.join(missing)}"
        )

    for fields in (shape.required, shape.optional):
        for name, kind in fields.items():
            if name not in payload:
                continue
            if not _matches_payload_kind(payload[name], kind):
                raise OperationSafetyError(
                    f"{operation_type}.payload.{name} 必须是"
                    f"{_PAYLOAD_KIND_LABELS[kind]}"
                )

    if operation_type == "update_custom_field":
        custom_fields = payload["custom_fields"]
        if not isinstance(custom_fields, dict):
            return
        invalid = sorted(
            name
            for name in custom_fields
            if not _CUSTOM_FIELD_PATTERN.fullmatch(name)
        )
        if invalid:
            raise OperationSafetyError(
                "update_custom_field.payload.custom_fields 包含无效字段 ID："
                + "、".join(invalid)
            )


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
    assert_valid_payload_shape(draft.operation_type, draft.payload)
    return classify_base_risk(draft.operation_type, draft.payload)


def assert_safe_for_submit(operation: OperationRecord) -> None:
    """Recheck immutable safety rules while direct dependencies may remain."""

    assert_allowed_and_non_delete(operation.operation_type, operation.payload)
    try:
        validate_dependency_syntax(operation, allow_unresolved=True)
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
    assert_valid_payload_shape(operation.operation_type, operation.payload)
    try:
        validate_dependency_syntax(operation, allow_unresolved=False)
    except DependencyError as exc:
        raise OperationSafetyError(str(exc)) from exc
