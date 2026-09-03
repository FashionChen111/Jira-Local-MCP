"""Pure rules for attachment references embedded in Jira text fields."""

from __future__ import annotations

import re
from collections.abc import Mapping

from core.operations.dependencies import _issue
from core.operations.dependencies._types import (
    AttachmentReference,
    AttachmentReferenceInspection,
)
from core.operations.errors import DependencyError
from core.operations.models import (
    AttachmentDependencyResolution,
    AttachmentResult,
    DependencyState,
    OperationRecord,
    OperationType,
)


_PRODUCER_OPERATION_TYPES = frozenset({"add_attachment"})
_REFERENCE_PATTERN = re.compile(
    r"\[\^\{\{attachment:(?P<link>op-[0-9a-f]{32})\}\}\]"
    r"|!\{\{attachment:(?P<image>op-[0-9a-f]{32})\}\}!"
)
_ANY_PLACEHOLDER = re.compile(r"\{\{attachment:[^{}]*\}\}")
_JIRA_ATTACHMENT_LINK = re.compile(r"\[\^[^\]\r\n]+\]")
_JIRA_IMAGE = re.compile(r"!([^!\r\n]+)!")
_REFERENCE_TEXT_PATHS: Mapping[str, tuple[str, ...]] = {
    "create_issue": ("description", "extra_fields.description"),
    "change_type": ("fields_mapping.description",),
    "transition_issue": ("fields.description", "comment"),
    "update_field": ("fields.description",),
    "add_comment": ("body",),
    "add_worklog": ("comment",),
}


def is_producer(operation_type: OperationType | None) -> bool:
    """Return whether an operation type produces a Jira attachment result."""

    return operation_type in _PRODUCER_OPERATION_TYPES


def reference_text_paths(operation_type: str) -> tuple[str, ...]:
    """Return the supported Jira text paths for an operation type."""

    return _REFERENCE_TEXT_PATHS.get(operation_type, ())


def description_paths(operation_type: str) -> tuple[str, ...]:
    """Return supported description paths from the shared text-path rules."""

    return tuple(
        path
        for path in reference_text_paths(operation_type)
        if path == "description" or path.endswith(".description")
    )


def _reference_text_fields(
    operation: OperationRecord,
) -> tuple[tuple[str, object], ...]:
    payload = operation.payload
    items: list[tuple[str, object]] = []
    for path in reference_text_paths(operation.operation_type):
        if "." not in path:
            if path in payload:
                items.append((path, payload[path]))
            continue
        container_name, field_name = path.split(".", 1)
        container = payload.get(container_name)
        if isinstance(container, dict) and field_name in container:
            items.append((path, container[field_name]))
    return tuple(items)


def _contains_jira_attachment_markup(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if _JIRA_ATTACHMENT_LINK.search(value):
        return True
    return any(
        not match.group(1).strip().lower().startswith(("http://", "https://"))
        for match in _JIRA_IMAGE.finditer(value)
    )


def inspect_references(operation: OperationRecord) -> AttachmentReferenceInspection:
    """Scan each supported text field once without raising syntax errors."""

    references: list[AttachmentReference] = []
    invalid_paths: list[str] = []
    jira_markup_paths: list[str] = []
    for path, value in _reference_text_fields(operation):
        if not isinstance(value, str):
            continue
        for match in _REFERENCE_PATTERN.finditer(value):
            link_id = match.group("link")
            image_id = match.group("image")
            references.append(
                AttachmentReference(
                    operation_id=link_id or image_id,
                    path=path,
                    render_kind="link" if link_id else "image",
                )
            )
        remaining = _REFERENCE_PATTERN.sub("", value)
        if _ANY_PLACEHOLDER.search(remaining):
            invalid_paths.append(path)
        if _contains_jira_attachment_markup(value):
            jira_markup_paths.append(path)
    return AttachmentReferenceInspection(
        references=tuple(references),
        invalid_placeholder_paths=tuple(dict.fromkeys(invalid_paths)),
        jira_attachment_markup_paths=tuple(dict.fromkeys(jira_markup_paths)),
    )


def validate_reference_syntax(
    operation: OperationRecord,
    inspection: AttachmentReferenceInspection,
    *,
    allow_unresolved: bool,
) -> None:
    """Assert attachment syntax and lifecycle-specific materialization rules."""

    if inspection.invalid_placeholder_paths:
        path = inspection.invalid_placeholder_paths[0]
        raise DependencyError(
            f"附件引用必须使用 [^{{{{attachment:op-…}}}}] 或 "
            f"!{{{{attachment:op-…}}}}!：{path}"
        )
    if (
        operation.operation_type == "create_issue"
        and inspection.jira_attachment_markup_paths
    ):
        raise DependencyError(
            "create_issue 初始 description 不得引用附件；请先创建事务、"
            "上传附件，再通过 update_field 更新 description"
        )
    if not allow_unresolved and inspection.references:
        raise DependencyError(
            "执行前必须物化全部 attachment operation 引用："
            f"{inspection.references[0].path}"
        )


def _same_issue(
    operation: OperationRecord,
    state: DependencyState,
    states: Mapping[str, DependencyState],
) -> bool:
    consumer_target = _issue.comparable_target(operation.issue_key, states)
    producer_target = _issue.comparable_target(
        state.operation_issue_key, states
    )
    return (
        consumer_target is not None
        and producer_target is not None
        and consumer_target == producer_target
    )


def validate_reference_contract(
    operation: OperationRecord,
    references: tuple[AttachmentReference, ...],
    states: Mapping[str, DependencyState],
) -> None:
    """Assert producer existence, capability, issue context, and lifecycle."""

    for reference in references:
        producer_id = reference.operation_id
        state = states.get(producer_id)
        if state is None or state.state == "unknown":
            raise DependencyError(f"引用的附件 operation 不存在：{producer_id}")
        if not is_producer(state.operation_type):
            raise DependencyError(
                f"引用的 operation 不是 add_attachment：{producer_id}"
            )
        if not _same_issue(operation, state, states):
            raise DependencyError(
                f"附件引用与正文 operation 不属于同一 Jira 事务：{producer_id}"
            )
        if state.state == "success" and state.attachment_result is None:
            raise DependencyError(
                f"引用的附件生产者缺少上传结果：{producer_id}"
            )
        if state.state != "success" and not state.active:
            raise DependencyError(
                f"引用的附件生产者已处于不可用状态 {state.state}：{producer_id}"
            )


def collect_resolutions(
    references: tuple[AttachmentReference, ...],
    states: Mapping[str, DependencyState],
) -> dict[str, AttachmentResult]:
    """Collect available upload results after contract validation."""

    resolved: dict[str, AttachmentResult] = {}
    for reference in references:
        state = states.get(reference.operation_id)
        if (
            state is not None
            and state.state == "success"
            and state.attachment_result is not None
        ):
            resolved[reference.operation_id] = state.attachment_result
    return resolved


def _replace_payload_text(
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
    for path, value in _reference_text_fields(operation):
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
            result = _replace_payload_text(result, path, materialized)
    return result


def submission_block_reason(
    operation: OperationRecord,
    inspection: AttachmentReferenceInspection,
    states: Mapping[str, DependencyState],
) -> str | None:
    """Describe the first attachment dependency preventing submission."""

    try:
        validate_reference_syntax(
            operation, inspection, allow_unresolved=True
        )
    except DependencyError as exc:
        return str(exc)
    for reference in inspection.references:
        operation_id = reference.operation_id
        state = states.get(operation_id)
        if state is None or state.state == "unknown":
            return f"依赖的附件 operation 不存在：{operation_id}"
        if not is_producer(state.operation_type):
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


def resolution_from_state(
    state: DependencyState,
) -> AttachmentDependencyResolution | None:
    """Project a valid successful state to its typed attachment resolution."""

    if (
        is_producer(state.operation_type)
        and state.state == "success"
        and state.attachment_result is not None
    ):
        return AttachmentDependencyResolution(
            producer_operation_id=state.operation_id,
            attachment_result=state.attachment_result,
        )
    return None
