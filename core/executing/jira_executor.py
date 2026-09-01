"""Jira operation handlers; only the execution worker calls this module."""
from __future__ import annotations

import os
from typing import Any

from core import jira_client
from core.operations import (
    AttachmentResult,
    OperationExecutionResult,
    assert_safe_to_execute,
)

MOVE_UNSUPPORTED_MSG = (
    "Jira Server / Data Center REST API 不支持移动事务，请在 Jira 页面人工处理。"
)


def _create(client: Any, task: Any) -> OperationExecutionResult:
    payload = task.payload
    fields: dict[str, Any] = {
        "project": {"key": payload["project_key"]},
        "issuetype": {"name": payload["issue_type"]},
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
    issue = client.create_issue(fields=fields)
    return OperationExecutionResult(
        summary=f"已创建事务 {issue.key}", new_issue_key=issue.key
    )


def _clone(client: Any, task: Any) -> OperationExecutionResult:
    payload = task.payload
    source = client.issue(task.issue_key)
    fields: dict[str, Any] = {
        "project": {"key": source.fields.project.key},
        "issuetype": {"name": source.fields.issuetype.name},
        "summary": payload.get("summary") or f"[克隆] {source.fields.summary}",
    }
    if getattr(source.fields, "description", None):
        fields["description"] = source.fields.description
    if getattr(source.fields, "priority", None):
        fields["priority"] = {"name": source.fields.priority.name}
    issue = client.create_issue(fields=fields)
    notes = []
    if payload.get("clone_attachments"):
        try:
            for attachment in getattr(source.fields, "attachment", []) or []:
                client.add_attachment(issue=issue.key, attachment=attachment.get(), filename=attachment.filename)
            notes.append("附件已复制")
        except Exception as exc:
            notes.append(f"附件复制失败({type(exc).__name__})")
    if payload.get("clone_subtasks"):
        notes.append("子任务克隆需人工核对（未自动执行）")
    suffix = "；" + "，".join(notes) if notes else ""
    return OperationExecutionResult(
        summary=f"已克隆 {task.issue_key} → {issue.key}{suffix}",
        new_issue_key=issue.key,
    )


def _move(_client: Any, _task: Any) -> OperationExecutionResult:
    raise RuntimeError(MOVE_UNSUPPORTED_MSG)


def _change_type(client: Any, task: Any) -> OperationExecutionResult:
    payload = task.payload
    fields = {"issuetype": {"name": payload["target_issue_type"]}}
    fields.update(payload.get("fields_mapping") or {})
    try:
        client.issue(task.issue_key).update(fields=fields)
    except Exception as exc:
        raise RuntimeError(f"变更事务类型失败: {type(exc).__name__}") from exc
    return OperationExecutionResult(
        summary=f"{task.issue_key} 类型已变更为 {payload['target_issue_type']}"
    )


def _transition(client: Any, task: Any) -> OperationExecutionResult:
    payload = task.payload
    issue = client.issue(task.issue_key)
    wanted = str(payload["transition_name"]).strip().lower()
    transition_id = next((item["id"] for item in client.transitions(issue) if item["name"].strip().lower() == wanted), None)
    if transition_id is None:
        raise RuntimeError(f"未找到流转「{payload['transition_name']}」")
    kwargs: dict[str, Any] = {}
    if payload.get("fields"):
        kwargs["fields"] = dict(payload["fields"])
    if payload.get("comment"):
        kwargs["comment"] = payload["comment"]
    client.transition_issue(issue, transition_id, **kwargs)
    return OperationExecutionResult(
        summary=f"{task.issue_key} 状态已流转：{payload['transition_name']}"
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
    if not os.path.isabs(path) or not os.path.isfile(path):
        raise RuntimeError(f"附件文件不存在或非绝对路径: {path}")
    with open(path, "rb") as handle:
        uploaded = client.add_attachment(
            issue=task.issue_key,
            attachment=handle,
            filename=payload.get("file_name"),
        )
    attachment_id = _resource_value(uploaded, "id")
    if attachment_id is None or not str(attachment_id).strip():
        raise RuntimeError("Jira 附件上传响应缺少附件 ID")
    filename = _resource_value(uploaded, "filename")
    if filename is None or not str(filename).strip():
        confirmed = client.attachment(str(attachment_id))
        filename = _resource_value(confirmed, "filename")
    if filename is None or not str(filename).strip():
        raise RuntimeError("无法根据 Jira 附件 ID 确认最终文件名")
    attachment_result = AttachmentResult(
        id=str(attachment_id), filename=str(filename)
    )
    return OperationExecutionResult(
        summary=f"{task.issue_key} 已添加附件 {attachment_result.filename}",
        attachment_result=attachment_result,
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
        kwargs["started"] = payload["started"]
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
    "create_issue": _create, "clone_issue": _clone, "move_issue": _move,
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
