"""Pure rules for attachment references embedded in Jira text fields."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from core.operations.dependencies import _issue
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
    "create_issue": ("description",),
    "change_type": ("fields_mapping.description",),
    "transition_issue": ("fields.description", "comment"),
    "update_field": ("fields.description",),
    "add_comment": ("body",),
    "add_worklog": ("comment",),
}


@dataclass(frozen=True)
class AttachmentReference:
    """One attachment producer reference embedded in a Jira text field."""

    referenced_operation_id: str
    path: str
    render_kind: Literal["link", "image"]


@dataclass(frozen=True)
class AttachmentReferenceInspection:
    """One scan result containing references and syntax diagnostics."""

    references: tuple[AttachmentReference, ...]
    invalid_placeholder_paths: tuple[str, ...]
    jira_attachment_markup_paths: tuple[str, ...]


def is_producer(operation_type: OperationType | None) -> bool:
    """Return whether an operation type produces a Jira attachment result."""

    return operation_type in _PRODUCER_OPERATION_TYPES


def supported_reference_text_paths(operation_type: str) -> tuple[str, ...]:
    """Return the supported Jira text paths for an operation type."""

    return _REFERENCE_TEXT_PATHS.get(operation_type, ())


def attachment_reference_description_paths(
    operation_type: str,
) -> tuple[str, ...]:
    """Return supported description paths from the shared text-path rules."""

    return tuple(
        path
        for path in supported_reference_text_paths(operation_type)
        if path == "description" or path.endswith(".description")
    )


def _reference_text_values(
    operation: OperationRecord,
) -> tuple[tuple[str, object], ...]:
    payload = operation.payload
    values: list[tuple[str, object]] = []
    for path in supported_reference_text_paths(operation.operation_type):
        if "." not in path:
            if path in payload:
                values.append((path, payload[path]))
            continue
        container_name, field_name = path.split(".", 1)
        container = payload.get(container_name)
        if isinstance(container, dict) and field_name in container:
            values.append((path, container[field_name]))
    return tuple(values)


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
    for path, value in _reference_text_values(operation):
        if not isinstance(value, str):
            continue
        for match in _REFERENCE_PATTERN.finditer(value):
            link_id = match.group("link")
            image_id = match.group("image")
            references.append(
                AttachmentReference(
                    referenced_operation_id=link_id or image_id,
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


def _shares_issue_context(
    operation: OperationRecord,
    state: DependencyState,
    states: Mapping[str, DependencyState],
) -> bool:
    consumer_target = _issue.comparable_issue_target(operation.issue_key, states)
    producer_target = _issue.comparable_issue_target(
        state.operation_issue_key, states
    )
    return (
        consumer_target is not None
        and producer_target is not None
        and consumer_target == producer_target
    )


def reference_problem(
    operation: OperationRecord,
    references: tuple[AttachmentReference, ...],
    states: Mapping[str, DependencyState],
    *,
    allow_available_resolution: bool,
) -> str | None:
    """Return the first attachment-reference problem for one lifecycle."""

    for reference in references:
        referenced_operation_id = reference.referenced_operation_id
        state = states.get(referenced_operation_id)
        if state is None or state.state == "unknown":
            return (
                "引用的附件 operation 不存在："
                f"{referenced_operation_id}"
            )
        if not is_producer(state.operation_type):
            return (
                "引用的 operation 不是 add_attachment："
                f"{referenced_operation_id}"
            )
        if not _shares_issue_context(operation, state, states):
            return (
                "附件引用与正文 operation 不属于同一 Jira 事务："
                f"{referenced_operation_id}"
            )
        if state.state == "success":
            if state.attachment_result is None:
                return (
                    "引用的附件生产者缺少上传结果："
                    f"{referenced_operation_id}"
                )
            if not allow_available_resolution:
                return (
                    "依赖的附件上传已成功但引用尚未物化，数据不一致："
                    f"{referenced_operation_id}"
                )
        elif not state.active:
            return (
                "引用的附件生产者已处于不可用状态 "
                f"{state.state}：{referenced_operation_id}"
            )
    return None


def validate_reference_contract(
    operation: OperationRecord,
    references: tuple[AttachmentReference, ...],
    states: Mapping[str, DependencyState],
) -> None:
    """Assert producer existence, capability, issue context, and lifecycle."""

    problem = reference_problem(
        operation,
        references,
        states,
        allow_available_resolution=True,
    )
    if problem is not None:
        raise DependencyError(problem)


def _replace_payload_texts(
    operation: OperationRecord,
    replacements: Mapping[str, str],
) -> OperationRecord:
    data = operation.model_dump(
        mode="python", include=set(OperationRecord.model_fields)
    )
    payload = data["payload"]
    if not isinstance(payload, dict):
        raise DependencyError("operation payload 必须是对象")
    for path, value in replacements.items():
        if "." in path:
            container_name, field_name = path.split(".", 1)
            container = payload.get(container_name)
            if not isinstance(container, dict):
                raise DependencyError(f"附件引用路径不存在：{path}")
            copied_container = dict(container)
            copied_container[field_name] = value
            payload[container_name] = copied_container
        else:
            payload[path] = value
    data["payload"] = payload
    return OperationRecord.model_validate(data)


def materialize_references(
    operation: OperationRecord,
    attachment_results_by_producer: Mapping[str, AttachmentResult],
) -> tuple[OperationRecord, tuple[tuple[str, tuple[str, ...]], ...]]:
    """Replace attachment placeholders and report actual producer paths."""

    if not attachment_results_by_producer:
        return operation, ()

    replacements: dict[str, str] = {}
    paths_by_producer: dict[str, list[str]] = {}
    for path, value in _reference_text_values(operation):
        if not isinstance(value, str):
            continue

        def replacement(match: re.Match[str]) -> str:
            referenced_operation_id = match.group("link") or match.group("image")
            attachment_result = attachment_results_by_producer.get(
                referenced_operation_id
            )
            if attachment_result is None:
                return match.group(0)
            producer_paths = paths_by_producer.setdefault(
                referenced_operation_id, []
            )
            if path not in producer_paths:
                producer_paths.append(path)
            if match.group("link"):
                return f"[^{attachment_result.filename}]"
            return f"!{attachment_result.filename}!"

        materialized = _REFERENCE_PATTERN.sub(replacement, value)
        if materialized != value:
            replacements[path] = materialized

    if not replacements:
        return operation, ()
    materialized_operation = _replace_payload_texts(operation, replacements)
    changes = tuple(
        (
            producer_operation_id,
            tuple(paths_by_producer[producer_operation_id]),
        )
        for producer_operation_id in attachment_results_by_producer
        if producer_operation_id in paths_by_producer
    )
    return materialized_operation, changes


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
