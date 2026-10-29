"""Jira operation handlers; only the execution worker calls this module."""
from __future__ import annotations

import os
from io import BytesIO
from typing import Any, BinaryIO

from jira.exceptions import JIRAError

from core import jira_client
from core.operations import (
    AttachmentIdentity,
    AttachmentResult,
    OperationExecutionResult,
    assert_safe_to_execute,
    parse_aware_datetime,
)
from core.runtime_logger import redact


def _create(client: Any, task: Any) -> OperationExecutionResult:
    payload = task.payload
    fields: dict[str, Any] = {
        "project": {"key": payload["project_key"]},
        "issuetype": {"id": payload["issue_type_id"]},
        "summary": payload["summary"],
    }
    description = payload.get("description")
    if description is not None:
        fields["description"] = description
    if payload.get("priority"):
        fields["priority"] = {"name": payload["priority"]}
    if payload.get("assignee"):
        fields["assignee"] = {"name": payload["assignee"]}
    fields.update(payload.get("extra_fields") or {})
    issue = client.create_issue(fields=fields, prefetch=False)
    return OperationExecutionResult(
        summary=f"已创建事务 {issue.key}", new_issue_key=issue.key
    )


def _clone(client: Any, task: Any) -> OperationExecutionResult:
    payload = task.payload
    source = client.issue(task.issue_key)
    fields: dict[str, Any] = {
        "project": {"key": source.fields.project.key},
        "issuetype": {"id": source.fields.issuetype.id},
        "summary": payload.get("summary") or f"[克隆] {source.fields.summary}",
    }
    if getattr(source.fields, "description", None) is not None:
        fields["description"] = source.fields.description
    if getattr(source.fields, "priority", None):
        fields["priority"] = {"id": source.fields.priority.id}
    if getattr(source.fields.issuetype, "subtask", False):
        fields["parent"] = {"key": source.fields.parent.key}
    overrides = payload.get("field_overrides") or {}
    fields.update(overrides)
    if "parent" in overrides:
        parent = client.issue(overrides["parent"]["key"], fields="project,issuetype")
        if (parent.fields.project.key != source.fields.project.key
                or getattr(parent.fields.issuetype, "subtask", False)
                or not getattr(source.fields.issuetype, "subtask", False)):
            raise ValueError("parent 必须是同项目中的非子任务，且仅能用于克隆子任务")
    attachments = (
        list(getattr(source.fields, "attachment", []) or [])
        if payload.get("clone_attachments") else []
    )
    copies = [
        AttachmentResult(
            source_attachment_id=str(item.id), requested_filename=item.filename,
            status="unattempted",
        )
        for item in attachments
    ]
    issue = client.create_issue(fields=fields, prefetch=False)
    for index, attachment in enumerate(attachments):
        try:
            content = attachment.get()
            if not isinstance(content, bytes):
                raise TypeError("附件下载结果必须为 bytes")
            with BytesIO(content) as handle:
                copies[index] = _upload_attachment(
                    client, issue.key, handle, attachment.filename, str(attachment.id)
                )
        except Exception as exc:
            copies[index] = AttachmentResult(
                source_attachment_id=str(attachment.id),
                requested_filename=attachment.filename,
                status="failed", error=redact(f"{type(exc).__name__}: {exc}"),
            )
        if copies[index].status != "success":
            return OperationExecutionResult(
                outcome="warning", new_issue_key=issue.key,
                summary=f"已创建克隆事务 {issue.key}，附件复制未完成；禁止自动重放克隆",
                attachment_results=tuple(copies),
            )
    return OperationExecutionResult(
        summary=f"已克隆 {task.issue_key} → {issue.key}", new_issue_key=issue.key,
        attachment_results=tuple(copies),
    )


def _change_type(client: Any, task: Any) -> OperationExecutionResult:
    payload = task.payload
    fields = {"issuetype": {"id": payload["target_issue_type_id"]}}
    fields.update(payload.get("fields_mapping") or {})
    client.issue(task.issue_key).update(fields=fields)
    return OperationExecutionResult(
        summary=f"{task.issue_key} 类型已变更为 {payload['target_issue_type_id']}"
    )


def _transition(client: Any, task: Any) -> OperationExecutionResult:
    payload = task.payload
    issue = client.issue(task.issue_key)
    transition_id = payload["transition_id"]
    if not any(str(item["id"]) == transition_id for item in client.transitions(issue)):
        raise ValueError(f"流转 ID {transition_id} 当前不可用")
    kwargs: dict[str, Any] = {}
    if payload.get("fields"):
        kwargs["fields"] = dict(payload["fields"])
    if payload.get("comment"):
        kwargs["comment"] = payload["comment"]
    client.transition_issue(issue, transition_id, **kwargs)
    return OperationExecutionResult(
        summary=f"{task.issue_key} 状态已流转：{payload['transition_id']}"
    )


def _update_field(client: Any, task: Any) -> OperationExecutionResult:
    payload = task.payload
    fields = dict(payload["fields"])
    client.issue(task.issue_key).update(fields=fields)
    return OperationExecutionResult(
        summary=f"{task.issue_key} 已更新字段：{', '.join(fields)}"
    )


def _update_custom_field(client: Any, task: Any) -> OperationExecutionResult:
    payload = task.payload
    fields = dict(payload["custom_fields"])
    client.issue(task.issue_key).update(fields=fields)
    return OperationExecutionResult(
        summary=f"{task.issue_key} 已更新自定义字段：{', '.join(fields)}"
    )


def _resource_value(resource: Any, name: str) -> object:
    value = getattr(resource, name, None)
    if value is not None:
        return value
    raw = getattr(resource, "raw", None)
    return raw.get(name) if isinstance(raw, dict) else None


def _attachment(client: Any, task: Any) -> OperationExecutionResult:
    payload = task.payload
    path = payload["file_path"]
    requested_filename = payload.get("file_name") or os.path.basename(path)
    try:
        if not os.path.isabs(path) or not os.path.isfile(path):
            raise RuntimeError(f"附件文件不存在或非绝对路径: {path}")
        with open(path, "rb") as handle:
            result = _upload_attachment(client, task.issue_key, handle, requested_filename)
    except Exception as exc:
        result = AttachmentResult(
            requested_filename=requested_filename, status="failed",
            error=redact(f"{type(exc).__name__}: {exc}"),
        )
    if result.attachment is None:
        return OperationExecutionResult(
            outcome="failed", summary=f"{task.issue_key} 附件上传未确认成功",
            error=result.error, attachment_results=(result,),
        )
    return OperationExecutionResult(
        summary=f"{task.issue_key} 已添加附件 {result.attachment.filename}",
        attachment_results=(result,),
    )


def _upload_attachment(
    client: Any, issue_key: str, handle: BinaryIO, requested_filename: str,
    source_attachment_id: str | None = None,
) -> AttachmentResult:
    """Upload once and distinguish rejection from an unconfirmed write."""
    try:
        uploaded = client.add_attachment(
            issue=issue_key, attachment=handle, filename=requested_filename,
        )
    except Exception as exc:
        rejected = (
            isinstance(exc, JIRAError)
            and isinstance(exc.status_code, int)
            and 400 <= exc.status_code < 500
        )
        return AttachmentResult(
            requested_filename=requested_filename,
            source_attachment_id=source_attachment_id,
            status="failed" if rejected else "unknown",
            error=redact(f"{type(exc).__name__}: {exc}"),
        )
    try:
        attachment = _confirmed_attachment(client, uploaded)
    except Exception as exc:
        return AttachmentResult(
            requested_filename=requested_filename,
            source_attachment_id=source_attachment_id,
            status="unknown", error=redact(f"{type(exc).__name__}: {exc}"),
        )
    return AttachmentResult(
        requested_filename=requested_filename,
        source_attachment_id=source_attachment_id,
        status="success", attachment=attachment,
    )


def _confirmed_attachment(client: Any, uploaded: Any) -> AttachmentIdentity:
    attachment_id = _resource_value(uploaded, "id")
    if attachment_id is None or not str(attachment_id).strip():
        raise RuntimeError("Jira 附件上传响应缺少附件 ID")
    filename = _resource_value(uploaded, "filename")
    if filename is None or not str(filename).strip():
        confirmed = client.attachment(str(attachment_id))
        filename = _resource_value(confirmed, "filename")
    if filename is None or not str(filename).strip():
        raise RuntimeError("无法根据 Jira 附件 ID 确认最终文件名")
    return AttachmentIdentity(
        id=str(attachment_id), filename=str(filename)
    )


def _comment(client: Any, task: Any) -> OperationExecutionResult:
    client.add_comment(task.issue_key, task.payload["body"])
    return OperationExecutionResult(summary=f"{task.issue_key} 已添加评论")


def _worklog(client: Any, task: Any) -> OperationExecutionResult:
    payload = task.payload
    kwargs: dict[str, Any] = {"timeSpent": payload["time_spent"]}
    if payload.get("comment"):
        kwargs["comment"] = payload["comment"]
    if payload.get("started"):
        kwargs["started"] = parse_aware_datetime(payload["started"])
    kwargs["adjustEstimate"] = payload.get("adjust_estimate", "auto")
    if payload.get("new_estimate") is not None:
        kwargs["newEstimate"] = payload["new_estimate"]
    if payload.get("reduce_by") is not None:
        kwargs["reduceBy"] = payload["reduce_by"]
    client.add_worklog(issue=task.issue_key, **kwargs)
    return OperationExecutionResult(
        summary=f"{task.issue_key} 已添加工作日志 {payload['time_spent']}"
    )


def _link(client: Any, task: Any) -> OperationExecutionResult:
    payload = task.payload
    client.create_issue_link(type=payload["link_type"], inwardIssue=task.issue_key, outwardIssue=task.target_issue_key)
    return OperationExecutionResult(
        summary=(
            f"已链接 {task.issue_key} --[{payload['link_type']}]--> "
            f"{task.target_issue_key}"
        )
    )


_HANDLERS = {
    "create_issue": _create, "clone_issue": _clone,
    "change_type": _change_type, "transition_issue": _transition,
    "update_field": _update_field, "update_custom_field": _update_custom_field,
    "add_attachment": _attachment, "add_comment": _comment, "add_worklog": _worklog,
    "add_issuelink": _link,
}


def execute_operation(task: Any) -> OperationExecutionResult:
    """Execute one authorized operation after the mandatory final safety gate."""
    assert_safe_to_execute(task)
    handler = _HANDLERS.get(task.operation_type)
    if handler is None:
        raise RuntimeError(f"不支持的操作类型: {task.operation_type}")
    return handler(jira_client.get_client(), task)
