"""Allowlist, deletion defence, risk policy, and final Jira target gates."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue, ValidationError

from core.operations.dependencies import orchestration as dependency_orchestration
from core.operations.errors import DependencyError
from core.operations.models import (
    AttachmentResult,
    DependencyState,
    OperationDraft,
    OperationRecord,
    RiskLevel,
)
from core.operations.rules.validation import is_jira_issue_key, parse_aware_datetime


ALLOWED_OPERATION_TYPES = frozenset(
    {
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
_DELETE_PATTERN = re.compile(r"delete|remove|purge|删除|移除", re.IGNORECASE)
_CUSTOM_FIELD_PATTERN = re.compile(r"customfield_\d+\Z")
_CREATE_ISSUE_FIELD_OWNERS: Mapping[str, str] = {
    "project": "project_key",
    "issuetype": "issue_type_id",
    "summary": "summary",
    "description": "description 或 description_file",
    "priority": "priority",
    "assignee": "assignee",
}

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
            "issue_type_id": "string",
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
            "field_overrides": "mapping",
        },
    ),
    "change_type": _PayloadShape(
        required={"target_issue_type_id": "string"},
        optional={"fields_mapping": "mapping"},
    ),
    "transition_issue": _PayloadShape(
        required={"transition_id": "string"},
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
            "adjust_estimate": "string",
            "new_estimate": "nullable_string",
            "reduce_by": "nullable_string",
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
    """Validate the stable payload shape and local operation constraints."""

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

    for key in ("issue_type_id", "target_issue_type_id", "transition_id"):
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
            raise OperationSafetyError(f"{key} 必须是十进制数字 ID 字符串")

    if operation_type == "change_type":
        fields_mapping = payload.get("fields_mapping")
        if isinstance(fields_mapping, dict) and "issuetype" in fields_mapping:
            raise OperationSafetyError(
                "fields_mapping 不得覆盖 issuetype，请使用 target_issue_type_id"
            )
    elif operation_type == "clone_issue":
        if payload.get("clone_subtasks"):
            raise OperationSafetyError("尚不支持子任务递归克隆，未入队")
        overrides = payload.get("field_overrides")
        if not isinstance(overrides, dict):
            return
        if overrides.keys() & {"project", "issuetype", "summary"}:
            raise OperationSafetyError(
                "field_overrides 不得覆盖 project、issuetype、summary"
            )
        if "parent" in overrides:
            parent = overrides["parent"]
            if (
                not isinstance(parent, dict)
                or set(parent) != {"key"}
                or not is_jira_issue_key(parent["key"])
            ):
                raise OperationSafetyError("parent 必须为仅包含真实 Jira Key 的对象")
        description = overrides.get("description")
        if isinstance(description, str) and "{{attachment:" in description:
            raise OperationSafetyError("克隆初始 description 不接受待上传附件引用")
    elif operation_type == "add_attachment":
        filename = payload.get("file_name")
        path = payload["file_path"]
        if filename is None and isinstance(path, str):
            filename = os.path.basename(path)
        try:
            AttachmentResult.model_validate({
                "requested_filename": filename, "status": "unattempted",
            })
        except ValidationError as exc:
            raise OperationSafetyError("附件请求文件名无效") from exc
    elif operation_type == "add_worklog":
        started = payload.get("started")
        if isinstance(started, str):
            try:
                parse_aware_datetime(started)
            except ValueError as exc:
                raise OperationSafetyError(f"started {exc}") from exc
        estimate_mode = payload.get("adjust_estimate", "auto")
        if estimate_mode not in ("auto", "leave", "new", "manual"):
            raise OperationSafetyError("adjust_estimate 必须为 auto、leave、new 或 manual")
        new_estimate = payload.get("new_estimate")
        reduce_by = payload.get("reduce_by")
        if estimate_mode == "new":
            if (
                not isinstance(new_estimate, str)
                or not new_estimate.strip()
                or reduce_by is not None
            ):
                raise OperationSafetyError("new 必须提供 new_estimate，且不能提供 reduce_by")
        elif estimate_mode == "manual":
            if (
                not isinstance(reduce_by, str)
                or not reduce_by.strip()
                or new_estimate is not None
            ):
                raise OperationSafetyError(
                    "manual 必须提供 reduce_by，且不能提供 new_estimate"
                )
        elif new_estimate is not None or reduce_by is not None:
            raise OperationSafetyError("auto/leave 不接受 new_estimate 或 reduce_by")
    elif operation_type == "update_custom_field":
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
    elif operation_type == "create_issue":
        extra_fields = payload.get("extra_fields")
        if not isinstance(extra_fields, dict):
            return
        conflicts = sorted(extra_fields.keys() & _CREATE_ISSUE_FIELD_OWNERS.keys())
        if conflicts:
            guidance = "、".join(
                f"{name}（请使用顶层 {_CREATE_ISSUE_FIELD_OWNERS[name]}）"
                for name in conflicts
            )
            raise OperationSafetyError(
                "create_issue.payload.extra_fields 不得包含专用字段："
                f"{guidance}"
            )


def classify_base_risk(
    operation_type: str, payload: Mapping[str, JsonValue]
) -> tuple[RiskLevel, str]:
    """Classify the operation before selection-level escalation."""

    if operation_type in _LOW_RISK:
        return "low", "低风险：新增内容，影响可控"
    if operation_type == "transition_issue":
        return "high", f"高风险：流转 ID {payload.get('transition_id', '')} 会改变工作流状态"
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


def submission_problem(
    operation: OperationRecord,
    inspection: dependency_orchestration.DependencyInspection,
    dependency_states: Mapping[str, DependencyState],
) -> str | None:
    """Return the first common or dependency safety problem for submission."""

    try:
        assert_allowed_and_non_delete(operation.operation_type, operation.payload)
        assert_valid_payload_shape(operation.operation_type, operation.payload)
        dependency_orchestration.validate_reference_syntax(
            operation,
            inspection,
            allow_unresolved=True,
        )
    except (DependencyError, OperationSafetyError) as exc:
        return str(exc)
    return dependency_orchestration.submission_reference_problem(
        operation,
        inspection,
        dependency_states,
    )


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
        inspection = dependency_orchestration.inspect_dependencies(operation)
        dependency_orchestration.validate_reference_syntax(
            operation,
            inspection,
            allow_unresolved=False,
        )
    except DependencyError as exc:
        raise OperationSafetyError(str(exc)) from exc
