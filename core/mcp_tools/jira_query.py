"""面向 MCP 的 Jira 只读查询及活动队列提醒。"""

from __future__ import annotations

import json
from typing import Any

from core import jira_client
from core.operations import get_services
from core.runtime_logger import redact


def _error(prefix: str, exc: Exception) -> str:
    return f"{prefix}: {redact(type(exc).__name__ + ': ' + str(exc))}"


def _reminder() -> str:
    count = get_services().query.total_active()
    return (
        ""
        if count == 0
        else f"\n\n⚠️ 当前有 {count} 条活动 operation；需要审核时请调用 jira_show_queue。"
    )


def _serialize_issue(issue: Any, fields: list[str] | None) -> dict[str, Any]:
    source = issue.fields
    standard = {
        "summary": getattr(source, "summary", None),
        "status": getattr(getattr(source, "status", None), "name", None),
        "issuetype": getattr(getattr(source, "issuetype", None), "name", None),
        "priority": getattr(getattr(source, "priority", None), "name", None),
        "assignee": getattr(getattr(source, "assignee", None), "displayName", None),
        "reporter": getattr(getattr(source, "reporter", None), "displayName", None),
        "project": getattr(getattr(source, "project", None), "key", None),
        "labels": getattr(source, "labels", None),
        "created": getattr(source, "created", None),
        "updated": getattr(source, "updated", None),
        "description": getattr(source, "description", None),
    }
    result: dict[str, Any] = {"key": issue.key}
    if fields:
        for name in fields:
            result[name] = (
                standard[name] if name in standard else getattr(source, name, None)
            )
    else:
        result.update(
            {key: value for key, value in standard.items() if value is not None}
        )
    return result


def jira_query_issue(issue_key: str, fields: list[str] | None = None) -> str:
    """立即查询单个 Jira 事务；这是只读工具，不会修改 Jira。

    参数：
        issue_key: Jira 事务 Key，例如 ``PROJ-123``。
        fields: 可选的返回字段名列表；省略时返回常用标准字段。

    返回：
        JSON 格式的事务字段数据；查询失败时返回经过脱敏的错误信息。

    副作用：
        无 Jira 写入；只会立即读取 Jira 数据。
    """
    try:
        return (
            json.dumps(
                _serialize_issue(jira_client.get_client().issue(issue_key), fields),
                ensure_ascii=False,
                indent=2,
            )
            + _reminder()
        )
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
        return json.dumps(values, ensure_ascii=False, indent=2) + _reminder()
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
        return json.dumps(values, ensure_ascii=False, indent=2) + _reminder()
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
        return json.dumps(values, ensure_ascii=False, indent=2) + _reminder()
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
        values = jira_client.get_client().search_issues(
            jql, startAt=start_at, maxResults=max_results, fields=fields
        )
        result = {
            "total": getattr(values, "total", len(values)),
            "start_at": start_at,
            "count": len(values),
            "issues": [_serialize_issue(item, fields) for item in values],
        }
        return json.dumps(result, ensure_ascii=False, indent=2) + _reminder()
    except Exception as exc:
        return _error("JQL 搜索失败", exc)
