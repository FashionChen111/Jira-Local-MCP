"""Pure rules for attachment operation references embedded in Jira text."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from core.operations.errors import DependencyError
from core.operations.models import AttachmentResult, DependencyState, OperationRecord
from core.operations.rules.identifiers import is_jira_issue_key, is_operation_id
from core.operations.rules.issue_dependencies import ISSUE_PRODUCER_OPERATION_TYPES


ATTACHMENT_PRODUCER_OPERATION_TYPES = frozenset({"add_attachment"})
_REFERENCE_PATTERN = re.compile(
    r"\[\^\{\{attachment:(?P<link>op-[0-9a-f]{32})\}\}\]"
    r"|!\{\{attachment:(?P<image>op-[0-9a-f]{32})\}\}!"
)
_ANY_PLACEHOLDER = re.compile(r"\{\{attachment:[^{}]*\}\}")
_NATIVE_LINK = re.compile(r"\[\^[^\]\r\n]+\]")
_NATIVE_IMAGE = re.compile(r"!([^!\r\n]+)!")


@dataclass(frozen=True)
class AttachmentReference:
    """One attachment producer reference at a supported Jira text path."""

    operation_id: str
    path: str
    render_kind: Literal["link", "image"]


def _content_items(operation: OperationRecord) -> tuple[tuple[str, object], ...]:
    payload = operation.payload
    items: list[tuple[str, object]] = []

    def nested(container_name: str, field_name: str, path: str) -> None:
        container = payload.get(container_name)
        if isinstance(container, dict) and field_name in container:
            items.append((path, container[field_name]))

    if operation.operation_type == "create_issue":
        if "description" in payload:
            items.append(("description", payload["description"]))
        nested("extra_fields", "description", "extra_fields.description")
    elif operation.operation_type == "update_field":
        nested("fields", "description", "fields.description")
    elif operation.operation_type == "change_type":
        nested(
            "fields_mapping", "description", "fields_mapping.description"
        )
    elif operation.operation_type == "transition_issue":
        nested("fields", "description", "fields.description")
        if "comment" in payload:
            items.append(("comment", payload["comment"]))
    elif operation.operation_type == "add_comment":
        if "body" in payload:
            items.append(("body", payload["body"]))
    elif operation.operation_type == "add_worklog":
        if "comment" in payload:
            items.append(("comment", payload["comment"]))
    return tuple(items)


def _parsed_references(path: str, value: object) -> tuple[AttachmentReference, ...]:
    if not isinstance(value, str):
        return ()
    result: list[AttachmentReference] = []
    for match in _REFERENCE_PATTERN.finditer(value):
        link_id = match.group("link")
        image_id = match.group("image")
        operation_id = link_id or image_id
        if not is_operation_id(operation_id):
            raise DependencyError(f"附件引用 operation ID 格式无效：{path}")
        result.append(
            AttachmentReference(
                operation_id=operation_id,
                path=path,
                render_kind="link" if link_id else "image",
            )
        )
    remaining = _REFERENCE_PATTERN.sub("", value)
    if _ANY_PLACEHOLDER.search(remaining):
        raise DependencyError(
            f"附件引用必须使用 [^{{{{attachment:op-…}}}}] 或 "
            f"!{{{{attachment:op-…}}}}!：{path}"
        )
    return tuple(result)


def references(operation: OperationRecord) -> tuple[AttachmentReference, ...]:
    """Return attachment references in stable content-path and text order."""

    return tuple(
        reference
        for path, value in _content_items(operation)
        for reference in _parsed_references(path, value)
    )


def _contains_local_attachment_syntax(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if _NATIVE_LINK.search(value):
        return True
    return any(
        not match.group(1).strip().lower().startswith(("http://", "https://"))
        for match in _NATIVE_IMAGE.finditer(value)
    )


def _resolved_issue_target(
    value: str | None, states: Mapping[str, DependencyState]
) -> str | None:
    if is_jira_issue_key(value):
        return value
    if is_operation_id(value):
        state = states.get(value)
        if (
            state is not None
            and state.operation_type in ISSUE_PRODUCER_OPERATION_TYPES
            and is_jira_issue_key(state.new_issue_key)
        ):
            return state.new_issue_key
    return value


def _same_issue(
    operation: OperationRecord,
    state: DependencyState,
    states: Mapping[str, DependencyState],
) -> bool:
    consumer_target = _resolved_issue_target(operation.issue_key, states)
    producer_target = _resolved_issue_target(state.issue_key, states)
    return (
        consumer_target is not None
        and producer_target is not None
        and consumer_target == producer_target
    )


def validate_references(
    operation: OperationRecord,
    states: Mapping[str, DependencyState],
) -> dict[str, AttachmentResult]:
    """Validate attachment producers and return available upload results."""

    content_items = _content_items(operation)
    if operation.operation_type == "create_issue" and any(
        _contains_local_attachment_syntax(value) for _, value in content_items
    ):
        raise DependencyError(
            "create_issue 初始 description 不得引用附件；请先创建事务、"
            "上传附件，再通过 update_field 更新 description"
        )

    resolved: dict[str, AttachmentResult] = {}
    for reference in references(operation):
        producer_id = reference.operation_id
        state = states.get(producer_id)
        if state is None or state.state == "unknown":
            raise DependencyError(f"引用的附件 operation 不存在：{producer_id}")
        if state.operation_type not in ATTACHMENT_PRODUCER_OPERATION_TYPES:
            raise DependencyError(
                f"引用的 operation 不是 add_attachment：{producer_id}"
            )
        if not _same_issue(operation, state, states):
            raise DependencyError(
                f"附件引用与正文 operation 不属于同一 Jira 事务：{producer_id}"
            )
        if state.state == "success":
            if state.attachment_result is None:
                raise DependencyError(
                    f"引用的附件生产者缺少上传结果：{producer_id}"
                )
            resolved[producer_id] = state.attachment_result
            continue
        if not state.active:
            raise DependencyError(
                f"引用的附件生产者已处于不可用状态 {state.state}：{producer_id}"
            )
    return resolved


def _replace_content(
    operation: OperationRecord, path: str, value: str
) -> OperationRecord:
    data = operation.model_dump(mode="python")
    payload = data["payload"]
    if not isinstance(payload, dict):
        raise DependencyError("operation payload 必须是对象")
    if "." in path:
        container_name, field_name = path.split(".", 1)
        container = payload.get(container_name)
        if not isinstance(container, dict):
            raise DependencyError(f"附件引用路径不存在：{path}")
        copied = dict(container)
        copied[field_name] = value
        payload[container_name] = copied
    else:
        payload[path] = value
    data["payload"] = payload
    return OperationRecord.model_validate(data)


def materialize_references(
    operation: OperationRecord,
    resolved_attachments: Mapping[str, AttachmentResult],
) -> OperationRecord:
    """Replace attachment placeholders with Jira-native filename syntax."""

    result = operation
    for path, value in _content_items(operation):
        if not isinstance(value, str):
            continue

        def replacement(match: re.Match[str]) -> str:
            operation_id = match.group("link") or match.group("image")
            attachment = resolved_attachments.get(operation_id)
            if attachment is None:
                return match.group(0)
            if match.group("link"):
                return f"[^{attachment.filename}]"
            return f"!{attachment.filename}!"

        materialized = _REFERENCE_PATTERN.sub(replacement, value)
        if materialized != value:
            result = _replace_content(result, path, materialized)
    return result


def block_reason(
    operation: OperationRecord,
    states: Mapping[str, DependencyState],
) -> str | None:
    """Describe the first attachment dependency preventing submission."""

    try:
        refs = references(operation)
    except DependencyError as exc:
        return str(exc)
    for reference in refs:
        operation_id = reference.operation_id
        state = states.get(operation_id)
        if state is None or state.state == "unknown":
            return f"依赖的附件 operation 不存在：{operation_id}"
        if state.operation_type not in ATTACHMENT_PRODUCER_OPERATION_TYPES:
            return f"依赖的 operation 不是附件上传：{operation_id}"
        if not _same_issue(operation, state, states):
            return f"依赖的附件与正文不属于同一 Jira 事务：{operation_id}"
        if state.state in {"failed", "dependency_failed", "abandoned"}:
            return f"依赖的附件上传已失败：{operation_id}"
        if state.state == "indeterminate":
            return f"依赖的附件上传结果待人工处置：{operation_id}"
        if state.state == "success" and state.attachment_result is None:
            return f"依赖的附件上传缺少结果：{operation_id}"
    return None
