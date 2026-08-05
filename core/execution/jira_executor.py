"""Jira operation handlers; only the execution worker calls this module."""
from __future__ import annotations

import os
from typing import Any

from core import jira_client, risk_engine
from core.textfile_loader import TextFileError, read_text_file

MOVE_UNSUPPORTED_MSG = (
    "Jira Server / Data Center REST API 不支持移动事务，请在 Jira 页面人工处理。"
)


def _resolve_description(payload: dict[str, Any]) -> str | None:
    file_path = payload.get("description_file")
    if file_path:
        if not isinstance(file_path, str):
            raise TextFileError("description_file 必须是文件路径字符串")
        return read_text_file(file_path)
    if "description" not in payload:
        return None
    value = payload["description"]
    if isinstance(value, dict):
        path = value.get("file")
        if not isinstance(path, str) or not path:
            raise TextFileError("description 字典必须包含非空 file 路径")
        return read_text_file(path)
    if isinstance(value, str) or value is None:
        return value
    raise TextFileError("description 必须是字符串、文件参数或 None")


def _resolve_fields(fields: dict[str, Any]) -> dict[str, Any]:
    result = dict(fields)
    value = result.get("description")
    if isinstance(value, dict) and value.get("file"):
        result["description"] = read_text_file(value["file"])
    return result


def _create(client: Any, task: Any) -> tuple[str, str | None]:
    payload = task.payload
    fields: dict[str, Any] = {
        "project": {"key": payload["project_key"]},
        "issuetype": {"name": payload["issue_type"]},
        "summary": payload["summary"],
    }
    description = _resolve_description(payload)
    if description is not None:
        fields["description"] = description
    if payload.get("priority"):
        fields["priority"] = {"name": payload["priority"]}
    if payload.get("assignee"):
        fields["assignee"] = {"name": payload["assignee"]}
    fields.update(payload.get("extra_fields") or {})
    issue = client.create_issue(fields=fields)
    return f"已创建事务 {issue.key}", issue.key


def _clone(client: Any, task: Any) -> tuple[str, str | None]:
    payload = task.payload
    source = client.issue(payload["issue_key"])
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
    return f"已克隆 {payload['issue_key']} → {issue.key}{suffix}", issue.key


def _move(_client: Any, _task: Any) -> tuple[str, str | None]:
    raise RuntimeError(MOVE_UNSUPPORTED_MSG)


def _change_type(client: Any, task: Any) -> tuple[str, str | None]:
    payload = task.payload
    fields = {"issuetype": {"name": payload["target_issue_type"]}}
    fields.update(payload.get("fields_mapping") or {})
    try:
        client.issue(payload["issue_key"]).update(fields=fields)
    except Exception as exc:
        raise RuntimeError(f"变更事务类型失败: {type(exc).__name__}") from exc
    return f"{payload['issue_key']} 类型已变更为 {payload['target_issue_type']}", None


def _transition(client: Any, task: Any) -> tuple[str, str | None]:
    payload = task.payload
    issue = client.issue(payload["issue_key"])
    wanted = str(payload["transition_name"]).strip().lower()
    transition_id = next((item["id"] for item in client.transitions(issue) if item["name"].strip().lower() == wanted), None)
    if transition_id is None:
        raise RuntimeError(f"未找到流转「{payload['transition_name']}」")
    kwargs: dict[str, Any] = {}
    if payload.get("fields"):
        kwargs["fields"] = _resolve_fields(payload["fields"])
    if payload.get("comment"):
        kwargs["comment"] = payload["comment"]
    client.transition_issue(issue, transition_id, **kwargs)
    return f"{payload['issue_key']} 状态已流转：{payload['transition_name']}", None


def _update_field(client: Any, task: Any) -> tuple[str, str | None]:
    payload = task.payload
    fields = _resolve_fields(payload["fields"])
    client.issue(payload["issue_key"]).update(fields=fields)
    return f"{payload['issue_key']} 已更新字段：{', '.join(fields)}", None


def _update_custom_field(client: Any, task: Any) -> tuple[str, str | None]:
    payload = task.payload
    fields = _resolve_fields(payload["custom_fields"])
    client.issue(payload["issue_key"]).update(fields=fields)
    return f"{payload['issue_key']} 已更新自定义字段：{', '.join(fields)}", None


def _attachment(client: Any, task: Any) -> tuple[str, str | None]:
    payload = task.payload
    path = payload["file_path"]
    if not os.path.isabs(path) or not os.path.isfile(path):
        raise RuntimeError(f"附件文件不存在或非绝对路径: {path}")
    with open(path, "rb") as handle:
        client.add_attachment(issue=payload["issue_key"], attachment=handle, filename=payload.get("file_name"))
    return f"{payload['issue_key']} 已添加附件 {payload.get('file_name') or os.path.basename(path)}", None


def _comment(client: Any, task: Any) -> tuple[str, str | None]:
    client.add_comment(task.payload["issue_key"], task.payload["body"])
    return f"{task.payload['issue_key']} 已添加评论", None


def _worklog(client: Any, task: Any) -> tuple[str, str | None]:
    payload = task.payload
    kwargs: dict[str, Any] = {"timeSpent": payload["time_spent"]}
    if payload.get("comment"):
        kwargs["comment"] = payload["comment"]
    if payload.get("started"):
        kwargs["started"] = payload["started"]
    client.add_worklog(issue=payload["issue_key"], **kwargs)
    return f"{payload['issue_key']} 已添加工作日志 {payload['time_spent']}", None


def _link(client: Any, task: Any) -> tuple[str, str | None]:
    payload = task.payload
    client.create_issue_link(type=payload["link_type"], inwardIssue=payload["issue_key"], outwardIssue=payload["target_issue_key"])
    return f"已链接 {payload['issue_key']} --[{payload['link_type']}]--> {payload['target_issue_key']}", None


_HANDLERS = {
    "create_issue": _create, "clone_issue": _clone, "move_issue": _move,
    "change_type": _change_type, "transition_issue": _transition,
    "update_field": _update_field, "update_custom_field": _update_custom_field,
    "add_attachment": _attachment, "add_comment": _comment, "add_worklog": _worklog,
    "add_issuelink": _link,
}


def execute_operation(task: Any) -> tuple[str, str | None]:
    """Execute one authorized operation after the mandatory second delete check."""
    risk_engine.assert_not_delete(task.operation_type, task.payload)
    handler = _HANDLERS.get(task.operation_type)
    if handler is None:
        raise RuntimeError(f"不支持的操作类型: {task.operation_type}")
    return handler(jira_client.get_client(), task)
