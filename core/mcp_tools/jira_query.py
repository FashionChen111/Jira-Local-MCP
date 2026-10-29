"""面向 MCP 的 Jira 只读查询及活动队列提醒。"""

from __future__ import annotations

import json

from core import jira_client
from core.jira_metadata import IssueContext, error_response, object_value, project_value, selected
from core.operations import get_services


DEFAULT_FIELDS = ["summary", "project", "issuetype", "status", "priority", "assignee", "reporter", "labels", "created", "updated", "description"]


def _error(prefix: str, exc: Exception) -> str:
    return json.dumps(error_response(exc), ensure_ascii=False)


def _response(value: object) -> str:
    result = object_value(value) if isinstance(value, dict) else {"items": project_value(value)}
    count = get_services().query.total_active()
    if count:
        result["queue_notice"] = f"当前有 {count} 条活动 operation；需要审核时请调用 jira_show_queue。"
    return json.dumps(result, ensure_ascii=False, indent=2)


def _field_names(fields: list[str] | None) -> list[str]:
    if fields is None:
        return DEFAULT_FIELDS.copy()
    if not fields or any(not isinstance(name, str) or not name.strip() for name in fields):
        raise ValueError("fields 必须是非空字段列表；省略时使用常用摘要字段")
    if any(any(char in name for char in (",", "*", "-", " ")) for name in fields):
        raise ValueError("fields 仅接受明确字段 ID，不接受通配符或组合选择表达式")
    return list(dict.fromkeys(fields))


def _serialize_issue(issue: object, fields: list[str]) -> dict[str, object]:
    raw = object_value(issue)
    values = object_value(raw.get("fields", {}))
    result = {}
    for name in fields:
        if name not in values:
            continue
        value = values[name]
        if isinstance(value, dict) and name in {"project", "issuetype", "status", "priority", "assignee", "reporter"}:
            value = selected(value, ("id", "key", "name", "displayName", "active", "subtask"))
        if name == "issuelinks" and isinstance(value, list):
            links = []
            for link in value:
                raw_link = object_value(link)
                projected = selected(raw_link, ("id", "type"))
                for direction in ("inwardIssue", "outwardIssue"):
                    related = raw_link.get(direction)
                    if isinstance(related, dict):
                        projected[direction] = selected(related, ("id", "key"))
                links.append(projected)
            value = links
        result[name] = project_value(value)
    return {"key": raw.get("key"), "fields": result}


def jira_query_issue(issue_key: str, fields: list[str] | None = None) -> str:
    """立即查询单个 Jira 事务；这是只读工具，不会修改 Jira。

    参数：
        issue_key: Jira 事务 Key，例如 ``PROJ-123``。
        fields: 可选的非空字段 ID 列表；省略时查询常用摘要字段，返回 key 与 fields，用户/类型保留准确身份。

    返回：
        JSON 格式的事务字段数据；查询失败时返回经过脱敏的错误信息。

    副作用：
        无 Jira 写入；只会立即读取 Jira 数据。
    """
    try:
        IssueContext(kind="issue", issue_key=issue_key)
        names = _field_names(fields)
        issue = jira_client.get_client().issue(issue_key, fields=",".join(names))
        return _response(_serialize_issue(issue, names))
    except Exception as exc:
        return _error("查询事务失败", exc)


def jira_query_comments(issue_key: str, limit: int = 20) -> str:
    """立即查询 Jira 事务的最新评论；这是只读工具，不会修改 Jira。

    参数：
        issue_key: Jira 事务 Key，例如 ``PROJ-123``。
        limit: 最多返回的最新评论数，必须是正整数，默认为 20。

    返回：
        JSON 格式的评论列表；查询失败时返回经过脱敏的错误信息。

    副作用：
        无 Jira 写入；只会立即读取 Jira 数据。
    """
    try:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit 必须是大于等于 1 的整数")
        comments = jira_client.get_client().comments(issue_key)
        values = [
            {
                "author": getattr(item.author, "displayName", None),
                "created": item.created,
                "body": item.body,
            }
            for item in comments[-limit:]
        ]
        return _response(values)
    except Exception as exc:
        return _error("查询评论失败", exc)


def jira_query_worklogs(issue_key: str, limit: int = 20) -> str:
    """立即查询 Jira 事务的最近工作日志；这是只读工具，不会修改 Jira。

    参数：
        issue_key: Jira 事务 Key，例如 ``PROJ-123``。
        limit: 最多返回的最近工作日志数，必须是正整数，默认为 20。

    返回：
        JSON 格式的工作日志列表；查询失败时返回经过脱敏的错误信息。

    副作用：
        无 Jira 写入；只会立即读取 Jira 数据。
    """
    try:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit 必须是大于等于 1 的整数")
        values = [
            {
                "author": getattr(item.author, "displayName", None),
                "started": getattr(item, "started", None),
                "timeSpent": getattr(item, "timeSpent", None),
                "comment": getattr(item, "comment", None),
            }
            for item in jira_client.get_client().worklogs(issue_key)[-limit:]
        ]
        return _response(values)
    except Exception as exc:
        return _error("查询工作日志失败", exc)


def jira_query_attachments(issue_key: str) -> str:
    """查询 Jira 事务的附件元数据；不会下载附件，也不会修改 Jira 或本地文件。

    参数：
        issue_key: Jira 事务 Key，例如 ``PROJ-123``。

    返回：
        JSON 格式的附件元数据；查询失败时返回经过脱敏的错误信息。

    副作用：
        无 Jira 写入或本地文件修改；只会立即读取 Jira 附件元数据。
    """
    try:
        issue = jira_client.get_client().issue(issue_key)
        values = [
            {
                "id": (
                    str(item.id) if getattr(item, "id", None) is not None else None
                ),
                "filename": item.filename,
                "size": getattr(item, "size", None),
                "author": getattr(getattr(item, "author", None), "displayName", None),
                "created": getattr(item, "created", None),
                "content": getattr(item, "content", None),
            }
            for item in getattr(issue.fields, "attachment", []) or []
        ]
        return _response(values)
    except Exception as exc:
        return _error("查询附件失败", exc)


def jira_jql_search(
    jql: str, fields: list[str] | None = None, start_at: int = 0, max_results: int = 50
) -> str:
    """立即执行分页 JQL 查询；这是只读工具，不会修改 Jira。

    参数：
        jql: JQL 查询表达式。
        fields: 每个事务需要返回的可选字段名列表。
        start_at: 从零开始的结果偏移量，默认为 0。
        max_results: 请求的分页大小，实际限制在 1 到 100，默认为 50。

    返回：
        JSON 格式的分页搜索结果；查询失败时返回经过脱敏的错误信息。

    副作用：
        无 Jira 写入；只会立即执行 Jira 查询。
    """
    try:
        max_results = min(max(1, max_results), 100)
        names = _field_names(fields)
        values = jira_client.get_client().search_issues(
            jql, startAt=start_at, maxResults=max_results, fields=names
        )
        result = {
            "total": getattr(values, "total", len(values)),
            "start_at": start_at,
            "count": len(values),
            "issues": [_serialize_issue(item, names) for item in values],
        }
        return _response(result)
    except Exception as exc:
        return _error("JQL 搜索失败", exc)
