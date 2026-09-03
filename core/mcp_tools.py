"""面向 MCP 的 Jira 只读查询与不可变操作入队函数。"""

from __future__ import annotations

import json
import os
from typing import Any

from core import jira_client
from core.runtime_logger import redact
from core.operations import get_services


def _error(prefix: str, exc: Exception) -> str:
    return f"{prefix}: {redact(type(exc).__name__ + ': ' + str(exc))}"


def _reminder() -> str:
    count = get_services().query.total_active()
    return (
        ""
        if count == 0
        else f"\n\n⚠️ 当前有 {count} 条活动 operation；需要审核时请调用 jira_show_queue。"
    )


def _response(operation: Any) -> str:
    result = (
        f"Operation ID: {operation.operation_id}\n"
        f"基础风险: {operation.risk_level}\n"
        "该 operation 已进入本地审核队列，尚未执行。"
    )
    if operation.operation_type in ("create_issue", "clone_issue"):
        result += (
            "\n在真实事务尚未生成时，可将此 Operation ID 直接作为后续修改 "
            "Tool 的 issue_key/target_issue_key。"
        )
    if operation.operation_type == "add_attachment":
        result += (
            "\n可在同一事务的后续 description、comment 或 worklog 中使用 "
            "[^{{attachment:"
            + operation.operation_id
            + "}}] 或 !{{attachment:"
            + operation.operation_id
            + "}}! 引用本次上传结果。"
        )
    return result


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


def _enqueue(
    operation_type: str,
    issue_key: str | None,
    payload: dict[str, Any],
    description: str,
    source: str,
    target_issue_key: str | None = None,
) -> str:
    try:
        return _response(
            get_services().queue.enqueue(
                operation_type,
                issue_key,
                payload,
                description,
                source,
                target_issue_key,
            )
        )
    except Exception as exc:
        return _error("入队失败", exc)


def jira_issue_create_append(
    project_key: str,
    issue_type: str,
    summary: str,
    description: str | None = None,
    description_file: str | None = None,
    priority: str | None = None,
    assignee: str | None = None,
    extra_fields: dict | None = None,
) -> str:
    """将新建 Jira 事务操作加入本地审核队列；仅入队，不会直接写入 Jira。

    参数：
        project_key: Jira 项目 Key。
        issue_type: 事务类型名称，例如 ``Bug``、``Task`` 或 ``Story``。
        summary: 事务标题。
        description: 可选的内联事务描述；未提供文件时优先于
            ``extra_fields.description``。
        description_file: 可选的本地 ``.txt`` 文件绝对路径；其内容拥有最高
            优先级，并在入队时固化为文本快照。
        priority: 可选的 Jira 优先级名称。
        assignee: 可选的 Jira Server/Data Center 经办人用户名。
        extra_fields: 可选的其他 Jira 字段映射；其中的 ``description`` 仅在
            上述两个来源均未提供有效内容时采用，并会归一化为专用字段。

        初始 description 不接受 Jira 本地附件引用。需要引用附件时，应先创建
        事务，再上传附件，最后通过字段更新 Tool 设置 description。

    返回：
        已入队的 operation ID、基础风险等级及“尚未执行”提示。只有用户在
        本地 Web 审核页明确提交后，后台 worker 才可以写入 Jira；operation
        ID 可立即作为后续修改 Tool 的待创建事务引用。

    副作用：
        仅在本地 SQLite 中创建待审核 operation；本次调用不写入 Jira。
    """
    payload = {
        "project_key": project_key,
        "issue_type": issue_type,
        "summary": summary,
        "description": description,
        "description_file": description_file,
        "priority": priority,
        "assignee": assignee,
        "extra_fields": extra_fields or {},
    }
    return _enqueue(
        "create_issue",
        None,
        payload,
        f"新建 {issue_type}「{summary}」于项目 {project_key}",
        "jira_issue_create_append",
    )


def jira_issue_clone_append(
    issue_key: str,
    summary: str | None = None,
    clone_subtasks: bool = False,
    clone_attachments: bool = False,
) -> str:
    """将克隆 Jira 事务操作加入本地审核队列；仅入队，不会直接写入 Jira。

    参数：
        issue_key: 源 Jira 事务的真实 Key；克隆来源不接受待创建 operation ID。
        summary: 可选的新事务标题。
        clone_subtasks: 是否在经审核的操作中克隆子任务，默认为 ``False``。
        clone_attachments: 是否在经审核的操作中克隆附件，默认为 ``False``。

    返回：
        已入队的 operation ID、基础风险等级及“尚未执行”提示；执行前必须由
        用户在本地 Web 审核页明确提交。该 operation ID 可立即作为后续修改
        Tool 的待创建事务引用。

    副作用：
        仅在本地 SQLite 中创建待审核 operation；本次调用不写入 Jira。
    """
    payload = {
        "summary": summary,
        "clone_subtasks": clone_subtasks,
        "clone_attachments": clone_attachments,
    }
    return _enqueue(
        "clone_issue",
        issue_key,
        payload,
        f"克隆事务 {issue_key}" + (f" → 标题「{summary}」" if summary else ""),
        "jira_issue_clone_append",
    )


def jira_issue_move_append(
    issue_key: str, target_project_key: str, target_issue_type: str | None = None
) -> str:
    """将 Jira 事务移动方案加入本地审核队列；仅入队，不会直接写入 Jira。

    Jira Server/Data Center REST API 不支持移动事务，因此即使用户授权，
    该操作也会在执行阶段明确失败，不会被伪装成成功。

    参数：
        issue_key: 源 Jira 事务 Key，或创建/克隆 operation ID。
        target_project_key: 目标 Jira 项目 Key。
        target_issue_type: 可选的目标事务类型。

    返回：
        已入队的 operation ID、基础风险等级及“尚未执行”提示。

    副作用：
        仅在本地 SQLite 中创建待审核 operation；本次调用不写入 Jira。
    """
    payload = {
        "target_project_key": target_project_key,
        "target_issue_type": target_issue_type,
    }
    return _enqueue(
        "move_issue",
        issue_key,
        payload,
        f"移动事务 {issue_key} → 项目 {target_project_key}",
        "jira_issue_move_append",
    )


def jira_issue_type_change_append(
    issue_key: str, target_issue_type: str, fields_mapping: dict | None = None
) -> str:
    """将 Jira 事务类型变更加入本地审核队列；仅入队，不会直接写入 Jira。

    参数：
        issue_key: Jira 事务 Key，或创建/克隆 operation ID。
        target_issue_type: 目标事务类型名称。
        fields_mapping: 可选的字段映射，用于适配不同事务类型的字段差异。
            其中 description 支持入队时文本文件快照及同事务附件引用语法。

    返回：
        已入队的 operation ID、基础风险等级及“尚未执行”提示；执行前必须由
        用户在本地 Web 审核页明确提交。

    副作用：
        仅在本地 SQLite 中创建待审核 operation；本次调用不写入 Jira。
    """
    payload = {
        "target_issue_type": target_issue_type,
        "fields_mapping": fields_mapping or {},
    }
    return _enqueue(
        "change_type",
        issue_key,
        payload,
        f"变更 {issue_key} 事务类型 → {target_issue_type}",
        "jira_issue_type_change_append",
    )


def jira_issue_transition_append(
    issue_key: str,
    transition_name: str,
    comment: str | None = None,
    fields: dict | None = None,
) -> str:
    """将 Jira 工作流状态流转加入本地审核队列；仅入队，不会直接写入 Jira。

    参数：
        issue_key: Jira 事务 Key，或创建/克隆 operation ID。
        transition_name: Jira 工作流流转名称。
        comment: 可选的流转评论，支持同事务附件引用语法。
        fields: 可选的流转字段映射，其中 description 支持入队时文本文件
            快照及同事务附件引用语法。

    返回：
        已入队的 operation ID、基础风险等级及“尚未执行”提示；执行前必须由
        用户在本地 Web 审核页明确提交。

    副作用：
        仅在本地 SQLite 中创建待审核 operation；本次调用不写入 Jira。
    """
    payload = {
        "transition_name": transition_name,
        "comment": comment,
        "fields": fields or {},
    }
    return _enqueue(
        "transition_issue",
        issue_key,
        payload,
        f"{issue_key} 状态流转「{transition_name}」",
        "jira_issue_transition_append",
    )


def jira_field_update_append(issue_key: str, fields: dict) -> str:
    """将 Jira 标准字段变更加入本地审核队列；仅入队，不会直接写入 Jira。

    参数：
        issue_key: Jira 事务 Key，或创建/克隆 operation ID。
        fields: 非空的标准字段映射。描述文件可写为
            ``{"description": {"file": "C:\\absolute\\path.txt"}}``。
            description 可使用 ``[^{{attachment:op-…}}]`` 或
            ``!{{attachment:op-…}}!`` 引用同一事务的附件上传 operation。

    返回：
        已入队的 operation ID、基础风险等级及“尚未执行”提示；执行前必须由
        用户在本地 Web 审核页明确提交。

    副作用：
        仅在本地 SQLite 中创建待审核 operation；本次调用不写入 Jira。
    """
    payload = {"fields": fields}
    return _enqueue(
        "update_field",
        issue_key,
        payload,
        f"修改 {issue_key} 字段: {', '.join(fields)}",
        "jira_field_update_append",
    )


def jira_customfield_update_append(issue_key: str, custom_fields: dict) -> str:
    """将 Jira 自定义字段变更加入本地审核队列；仅入队，不会直接写入 Jira。

    参数：
        issue_key: Jira 事务 Key，或创建/克隆 operation ID。
        custom_fields: 非空的自定义字段映射；键必须是 Jira 自定义字段 ID，
            例如 ``customfield_10001``。

    返回：
        已入队的 operation ID、基础风险等级及“尚未执行”提示；执行前必须由
        用户在本地 Web 审核页明确提交。

    副作用：
        仅在本地 SQLite 中创建待审核 operation；本次调用不写入 Jira。
    """
    payload = {"custom_fields": custom_fields}
    return _enqueue(
        "update_custom_field",
        issue_key,
        payload,
        f"修改 {issue_key} 自定义字段: {', '.join(custom_fields)}",
        "jira_customfield_update_append",
    )


def jira_attachment_add_append(
    issue_key: str, file_path: str, file_name: str | None = None
) -> str:
    """将 Jira 附件上传加入本地审核队列；仅入队，不会直接写入 Jira。

    参数：
        issue_key: Jira 事务 Key，或创建/克隆 operation ID。
        file_path: 已存在的本地文件绝对路径。
        file_name: 可选的附件显示名称；省略时使用本地文件名。

    返回：
        已入队的 operation ID、基础风险等级及“尚未执行”提示。

    副作用：
        校验本地文件后，仅在 SQLite 中创建待审核 operation；本次调用既不
        上传附件，也不写入 Jira。

    注意：
        文件目前不会被暂存或进行哈希绑定，审核内容并非不可变；从入队到执行
        之间，本地文件内容可能发生变化。
    """
    if not os.path.isabs(file_path):
        return f"附件路径必须为绝对路径: {file_path}"
    if not os.path.isfile(file_path):
        return f"附件文件不存在: {file_path}"
    payload = {"file_path": file_path, "file_name": file_name}
    return _enqueue(
        "add_attachment",
        issue_key,
        payload,
        f"为 {issue_key} 添加附件 {file_name or os.path.basename(file_path)}",
        "jira_attachment_add_append",
    )


def jira_comment_add_append(issue_key: str, body: str) -> str:
    """将 Jira 评论添加操作加入本地审核队列；仅入队，不会直接写入 Jira。

    参数：
        issue_key: Jira 事务 Key，或创建/克隆 operation ID。
        body: 评论正文。
            可使用 ``[^{{attachment:op-…}}]`` 或 ``!{{attachment:op-…}}!``
            引用同一事务的附件上传 operation。

    返回：
        已入队的 operation ID、基础风险等级及“尚未执行”提示；执行前必须由
        用户在本地 Web 审核页明确提交。

    副作用：
        仅在本地 SQLite 中创建待审核 operation；本次调用不写入 Jira。
    """
    preview = body[:30] + ("…" if len(body) > 30 else "")
    return _enqueue(
        "add_comment",
        issue_key,
        {"body": body},
        f"为 {issue_key} 添加评论：{preview}",
        "jira_comment_add_append",
    )


def jira_worklog_add_append(
    issue_key: str,
    time_spent: str,
    comment: str | None = None,
    started: str | None = None,
) -> str:
    """将 Jira 工作日志添加操作加入本地审核队列；仅入队，不会直接写入 Jira。

    参数：
        issue_key: Jira 事务 Key，或创建/克隆 operation ID。
        time_spent: Jira 耗时表达式，例如 ``2h`` 或 ``1d 4h``。
        comment: 可选的工作说明。
            可使用 ``[^{{attachment:op-…}}]`` 或 ``!{{attachment:op-…}}!``
            引用同一事务的附件上传 operation。
        started: 可选的 ISO 格式开始时间；省略时由 Jira 使用当前时间。

    返回：
        已入队的 operation ID、基础风险等级及“尚未执行”提示；执行前必须由
        用户在本地 Web 审核页明确提交。

    副作用：
        仅在本地 SQLite 中创建待审核 operation；本次调用不写入 Jira。
    """
    payload = {
        "time_spent": time_spent,
        "comment": comment,
        "started": started,
    }
    return _enqueue(
        "add_worklog",
        issue_key,
        payload,
        f"为 {issue_key} 添加工作日志 {time_spent}",
        "jira_worklog_add_append",
    )


def jira_issuelink_add_append(
    issue_key: str, target_issue_key: str, link_type: str
) -> str:
    """将 Jira 事务链接添加操作加入本地审核队列；仅入队，不会直接写入 Jira。

    参数：
        issue_key: 源 Jira 事务 Key，或创建/克隆 operation ID。
        target_issue_key: 目标 Jira 事务 Key，或另一个创建/克隆 operation ID。
        link_type: Jira 链接类型，例如 ``Blocks`` 或 ``Relates``。

    返回：
        已入队的 operation ID、基础风险等级及“尚未执行”提示；执行前必须由
        用户在本地 Web 审核页明确提交。

    副作用：
        仅在本地 SQLite 中创建待审核 operation；本次调用不写入 Jira。
    """
    payload = {
        "link_type": link_type,
    }
    return _enqueue(
        "add_issuelink",
        issue_key,
        payload,
        f"链接 {issue_key} --[{link_type}]--> {target_issue_key}",
        "jira_issuelink_add_append",
        target_issue_key=target_issue_key,
    )


TOOL_NAMES = [
    "jira_query_issue",
    "jira_query_comments",
    "jira_query_worklogs",
    "jira_query_attachments",
    "jira_jql_search",
    "jira_issue_create_append",
    "jira_issue_clone_append",
    "jira_issue_move_append",
    "jira_issue_type_change_append",
    "jira_issue_transition_append",
    "jira_field_update_append",
    "jira_customfield_update_append",
    "jira_attachment_add_append",
    "jira_comment_add_append",
    "jira_worklog_add_append",
    "jira_issuelink_add_append",
]
