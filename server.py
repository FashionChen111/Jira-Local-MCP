"""jira-safe-mcp —— JIRA 安全操作 MCP 服务入口。

层次：
  - 读工具：直连 JIRA，返回结果（无副作用）。
  - `*_append` 工具：仅入队，绝不执行。
  - jira_show_pending_queue：拉起 Web 确认页，立即返回。
  - jira_get_commit_log：查历史执行结果。

安全不变量：提交执行入口只在 Web /api/commit，绝不作为 MCP 工具暴露；
凭证与 nonce 绝不经 MCP 通道返回。

STDIO 规范：绝不 print 到 stdout；所有日志走 stderr / 文件。
"""
from __future__ import annotations

import json
import os
import webbrowser
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from config import SERVICE_VERSION, get_config
from core import jira_client, jira_executor
from core.audit_logger import get_logger, redact
from core.queue_manager import get_queue
from core.risk_engine import RiskError
from knowledge.prompts import PROMPTS

_KNOWLEDGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")

mcp = FastMCP("jira-safe-mcp")

JIRA_QUERY_LIMIT_MIN = 1

JiraQueryLimit = Annotated[
    int,
    Field(strict=True, ge=JIRA_QUERY_LIMIT_MIN),
]
CommitLogLimit = Annotated[
    int,
    Field(strict=True, ge=jira_executor.COMMIT_LOG_LIMIT_MIN),
]


# ============================================================
# 通用辅助
# ============================================================
def _pending_reminder() -> str:
    """若有待确认任务，返回提醒文本（供查询工具追加）。"""
    n = get_queue().total_pending()
    if n > 0:
        return (
            f"\n\n⚠️ 提醒：当前有 {n} 条待确认变更未提交，"
            f"如需执行请调用 jira_show_pending_queue 打开确认页面。"
        )
    return ""


def _validate_jira_query_limit(limit: int) -> int:
    """校验 Jira 列表查询数量，请求超量时返回现有全部数据。"""
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < JIRA_QUERY_LIMIT_MIN
    ):
        raise ValueError(f"limit 必须是大于等于 {JIRA_QUERY_LIMIT_MIN} 的整数")
    return limit


def _format_mcp_error(prefix: str, exc: Exception) -> str:
    """保留异常诊断信息，并脱敏配置中声明的敏感值。"""
    detail = redact(f"{type(exc).__name__}: {exc}")
    return f"{prefix}: {detail}"


def _enqueue_response(op_label: str, task: Any, extra: str = "") -> str:
    """入队成功的统一响应（不含 nonce）。"""
    return (
        f"✅ 已入队【{op_label}】\n"
        f"批次: {task.batch_id}\n"
        f"任务ID: {task.task_id}\n"
        f"风险等级: {task.risk_level}\n"
        f"描述: {task.description}"
        f"{extra}\n\n"
        f"该操作尚未执行。追加完全部变更后，请调用 jira_show_pending_queue 让用户确认。"
    )


def _serialize_issue(issue: Any, fields: list[str] | None) -> dict[str, Any]:
    f = issue.fields
    data: dict[str, Any] = {"key": issue.key}
    std = {
        "summary": getattr(f, "summary", None),
        "status": getattr(getattr(f, "status", None), "name", None),
        "issuetype": getattr(getattr(f, "issuetype", None), "name", None),
        "priority": getattr(getattr(f, "priority", None), "name", None),
        "assignee": getattr(getattr(f, "assignee", None), "displayName", None),
        "reporter": getattr(getattr(f, "reporter", None), "displayName", None),
        "project": getattr(getattr(f, "project", None), "key", None),
        "labels": getattr(f, "labels", None),
        "created": getattr(f, "created", None),
        "updated": getattr(f, "updated", None),
        "description": getattr(f, "description", None),
    }
    if fields:
        for name in fields:
            if name in std:
                data[name] = std[name]
            else:
                data[name] = getattr(f, name, None)
    else:
        data.update({k: v for k, v in std.items() if v is not None})
    return data


# ============================================================
# 一、读工具（直连 JIRA）
# ============================================================
@mcp.tool()
def jira_query_issue(issue_key: str, fields: list[str] | None = None) -> str:
    """查询单个 JIRA 事务的字段信息。

    Args:
        issue_key: 事务 Key，如 PROJ-123
        fields: 可选，指定返回的字段列表，不传则返回常用标准字段
    Returns:
        事务字段详情（JSON 文本）
    """
    try:
        issue = jira_client.get_client().issue(issue_key)
        data = _serialize_issue(issue, fields)
        return json.dumps(data, ensure_ascii=False, indent=2) + _pending_reminder()
    except Exception as e:
        return _format_mcp_error("查询事务失败", e)


@mcp.tool()
def jira_query_comments(issue_key: str, limit: JiraQueryLimit = 20) -> str:
    """查询 JIRA 事务的评论列表。

    Args:
        issue_key: 事务 Key
        limit: 返回最近 N 条评论，最小为 1，默认 20
    Returns:
        评论列表（JSON 文本），含作者、时间、内容
    """
    try:
        limit = _validate_jira_query_limit(limit)
        comments = jira_client.get_client().comments(issue_key)
        items = [
            {
                "author": getattr(c.author, "displayName", None),
                "created": c.created,
                "body": c.body,
            }
            for c in comments[-limit:]
        ]
        return json.dumps(items, ensure_ascii=False, indent=2) + _pending_reminder()
    except Exception as e:
        return _format_mcp_error("查询评论失败", e)


@mcp.tool()
def jira_query_worklogs(issue_key: str, limit: JiraQueryLimit = 20) -> str:
    """查询 JIRA 事务的工作日志列表。

    Args:
        issue_key: 事务 Key
        limit: 返回最近 N 条工作日志，最小为 1，默认 20
    Returns:
        工作日志列表（JSON 文本），含作者、时间、耗时、描述
    """
    try:
        limit = _validate_jira_query_limit(limit)
        worklogs = jira_client.get_client().worklogs(issue_key)
        items = [
            {
                "author": getattr(w.author, "displayName", None),
                "started": getattr(w, "started", None),
                "timeSpent": getattr(w, "timeSpent", None),
                "comment": getattr(w, "comment", None),
            }
            for w in worklogs[-limit:]
        ]
        return json.dumps(items, ensure_ascii=False, indent=2) + _pending_reminder()
    except Exception as e:
        return _format_mcp_error("查询工作日志失败", e)


@mcp.tool()
def jira_query_attachments(issue_key: str) -> str:
    """查询 JIRA 事务的附件列表及元信息。

    Args:
        issue_key: 事务 Key
    Returns:
        附件列表（JSON 文本），含文件名、大小、上传人、上传时间、下载链接
    """
    try:
        issue = jira_client.get_client().issue(issue_key)
        items = [
            {
                "filename": a.filename,
                "size": getattr(a, "size", None),
                "author": getattr(getattr(a, "author", None), "displayName", None),
                "created": getattr(a, "created", None),
                "content": getattr(a, "content", None),
            }
            for a in getattr(issue.fields, "attachment", []) or []
        ]
        return json.dumps(items, ensure_ascii=False, indent=2) + _pending_reminder()
    except Exception as e:
        return _format_mcp_error("查询附件失败", e)


@mcp.tool()
def jira_jql_search(
    jql: str,
    fields: list[str] | None = None,
    start_at: int = 0,
    max_results: int = 50,
) -> str:
    """通过 JQL 语句批量搜索 JIRA 事务。

    Args:
        jql: JQL 查询语句
        fields: 可选，指定返回字段
        start_at: 分页起始位置
        max_results: 每页最大返回数，默认 50，上限 100
    Returns:
        搜索结果列表（JSON 文本），含总数与事务摘要
    """
    try:
        max_results = min(max(1, max_results), 100)  # 上限 100，防大结果集
        client = jira_client.get_client()
        res = client.search_issues(
            jql, startAt=start_at, maxResults=max_results, fields=fields
        )
        items = [_serialize_issue(i, fields) for i in res]
        out = {
            "total": getattr(res, "total", len(items)),
            "start_at": start_at,
            "count": len(items),
            "issues": items,
        }
        return json.dumps(out, ensure_ascii=False, indent=2) + _pending_reminder()
    except Exception as e:
        return _format_mcp_error("JQL 搜索失败", e)


# ============================================================
# 二、事务级操作入队工具
# ============================================================
@mcp.tool()
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
    """新建 JIRA 事务操作追加到待执行队列，不会真实创建。

    Args:
        project_key: 项目 Key
        issue_type: 事务类型，如 Bug、Task、Story
        summary: 事务标题
        description: 可选，事务描述
        description_file: 可选，本地 txt 文件绝对路径，优先级高于 description
        priority: 可选，优先级
        assignee: 可选，经办人用户名（Server/DC 用 name）
        extra_fields: 可选，额外字段字典
    Returns:
        入队结果与批次信息
    """
    try:
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
        desc = f"新建 {issue_type}「{summary}」于项目 {project_key}"
        task = get_queue().enqueue("create_issue", None, payload, desc)
        return _enqueue_response("新建事务", task)
    except RiskError as e:
        return str(e)
    except Exception as e:
        return f"入队失败: {type(e).__name__}: {e}"


@mcp.tool()
def jira_issue_clone_append(
    issue_key: str,
    summary: str | None = None,
    clone_subtasks: bool = False,
    clone_attachments: bool = False,
) -> str:
    """克隆 JIRA 事务操作追加到待执行队列。

    Args:
        issue_key: 源事务 Key
        summary: 可选，新事务标题，不传则自动加"[克隆]"前缀
        clone_subtasks: 是否克隆子任务，默认否（需人工核对）
        clone_attachments: 是否克隆附件，默认否
    Returns:
        入队结果与批次信息
    """
    try:
        payload = {
            "issue_key": issue_key,
            "summary": summary,
            "clone_subtasks": clone_subtasks,
            "clone_attachments": clone_attachments,
        }
        desc = f"克隆事务 {issue_key}" + (f" → 标题「{summary}」" if summary else "")
        task = get_queue().enqueue("clone_issue", issue_key, payload, desc)
        return _enqueue_response("克隆事务", task)
    except RiskError as e:
        return str(e)
    except Exception as e:
        return f"入队失败: {type(e).__name__}: {e}"


@mcp.tool()
def jira_issue_move_append(
    issue_key: str,
    target_project_key: str,
    target_issue_type: str | None = None,
) -> str:
    """移动 JIRA 事务到其他项目操作追加到待执行队列。

    注意：JIRA Server / Data Center 的 REST API 不支持移动事务，
    该任务可入队展示，但执行阶段会失败，请在 JIRA 网页端手动操作。

    Args:
        issue_key: 源事务 Key
        target_project_key: 目标项目 Key
        target_issue_type: 可选，目标事务类型，不传则保持原类型
    Returns:
        入队结果与批次信息（含 Server/DC 不支持警告）
    """
    try:
        payload = {
            "issue_key": issue_key,
            "target_project_key": target_project_key,
            "target_issue_type": target_issue_type,
        }
        desc = f"移动事务 {issue_key} → 项目 {target_project_key}"
        task = get_queue().enqueue("move_issue", issue_key, payload, desc)
        warn = (
            "\n\n⚠️ 警告：JIRA Server/DC 的 REST 不支持移动事务，"
            "此任务执行阶段会失败，建议在网页端手动操作。"
        )
        return _enqueue_response("移动事务", task, extra=warn)
    except RiskError as e:
        return str(e)
    except Exception as e:
        return f"入队失败: {type(e).__name__}: {e}"


@mcp.tool()
def jira_issue_type_change_append(
    issue_key: str,
    target_issue_type: str,
    fields_mapping: dict | None = None,
) -> str:
    """变更 JIRA 事务类型操作追加到待执行队列。

    Args:
        issue_key: 事务 Key
        target_issue_type: 目标事务类型名称，如 "Task"、"Bug"
        fields_mapping: 可选，字段映射字典，适配类型转换的字段差异
    Returns:
        入队结果与批次信息
    """
    try:
        payload = {
            "issue_key": issue_key,
            "target_issue_type": target_issue_type,
            "fields_mapping": fields_mapping or {},
        }
        desc = f"变更 {issue_key} 事务类型 → {target_issue_type}"
        task = get_queue().enqueue("change_type", issue_key, payload, desc)
        return _enqueue_response("变更事务类型", task)
    except RiskError as e:
        return str(e)
    except Exception as e:
        return f"入队失败: {type(e).__name__}: {e}"


@mcp.tool()
def jira_issue_transition_append(
    issue_key: str,
    transition_name: str,
    comment: str | None = None,
    fields: dict | None = None,
) -> str:
    """转换 JIRA 事务状态（工作流流转）操作追加到待执行队列。

    Args:
        issue_key: 事务 Key
        transition_name: 流转名称，如"开始处理"、"解决"、"关闭"
        comment: 可选，流转时添加的备注评论
        fields: 可选，流转时同步更新的字段
    Returns:
        入队结果与批次信息
    """
    try:
        payload = {
            "issue_key": issue_key,
            "transition_name": transition_name,
            "comment": comment,
            "fields": fields or {},
        }
        desc = f"{issue_key} 状态流转「{transition_name}」"
        task = get_queue().enqueue("transition_issue", issue_key, payload, desc)
        return _enqueue_response("状态流转", task)
    except RiskError as e:
        return str(e)
    except Exception as e:
        return f"入队失败: {type(e).__name__}: {e}"


# ============================================================
# 三、事务内操作入队工具
# ============================================================
@mcp.tool()
def jira_field_update_append(issue_key: str, fields: dict) -> str:
    """修改 JIRA 事务官方字段操作追加到待执行队列。

    Args:
        issue_key: 事务 Key
        fields: 字段键值对，如 {"summary": "新标题", "priority": {"name": "High"}}。
                description 支持文件传参：{"description": {"file": "C:\\abs\\path.txt"}}，file 优先。
    Returns:
        入队结果与批次信息
    """
    try:
        payload = {"issue_key": issue_key, "fields": fields}
        keys = ", ".join(fields.keys())
        desc = f"修改 {issue_key} 字段: {keys}"
        task = get_queue().enqueue("update_field", issue_key, payload, desc)
        return _enqueue_response("修改官方字段", task)
    except RiskError as e:
        return str(e)
    except Exception as e:
        return f"入队失败: {type(e).__name__}: {e}"


@mcp.tool()
def jira_customfield_update_append(issue_key: str, custom_fields: dict) -> str:
    """修改 JIRA 事务自定义字段操作追加到待执行队列。

    Args:
        issue_key: 事务 Key
        custom_fields: 自定义字段键值对，key 为字段 ID（如 customfield_10001）
    Returns:
        入队结果与批次信息
    """
    try:
        payload = {"issue_key": issue_key, "custom_fields": custom_fields}
        keys = ", ".join(custom_fields.keys())
        desc = f"修改 {issue_key} 自定义字段: {keys}"
        task = get_queue().enqueue("update_custom_field", issue_key, payload, desc)
        return _enqueue_response("修改自定义字段", task)
    except RiskError as e:
        return str(e)
    except Exception as e:
        return f"入队失败: {type(e).__name__}: {e}"


@mcp.tool()
def jira_attachment_add_append(
    issue_key: str,
    file_path: str,
    file_name: str | None = None,
) -> str:
    """为 JIRA 事务添加附件操作追加到待执行队列。

    Args:
        issue_key: 事务 Key
        file_path: 本地文件绝对路径
        file_name: 可选，附件显示名称，不传则用原文件名
    Returns:
        入队结果与批次信息
    """
    try:
        if not os.path.isabs(file_path):
            return f"附件路径必须为绝对路径: {file_path}"
        if not os.path.isfile(file_path):
            return f"附件文件不存在: {file_path}"
        payload = {
            "issue_key": issue_key,
            "file_path": file_path,
            "file_name": file_name,
        }
        desc = f"为 {issue_key} 添加附件 {file_name or os.path.basename(file_path)}"
        task = get_queue().enqueue("add_attachment", issue_key, payload, desc)
        return _enqueue_response("添加附件", task)
    except RiskError as e:
        return str(e)
    except Exception as e:
        return f"入队失败: {type(e).__name__}: {e}"


@mcp.tool()
def jira_comment_add_append(issue_key: str, body: str) -> str:
    """为 JIRA 事务添加评论操作追加到待执行队列。

    Args:
        issue_key: 事务 Key
        body: 评论内容
    Returns:
        入队结果与批次信息
    """
    try:
        payload = {"issue_key": issue_key, "body": body}
        preview = body[:30] + ("…" if len(body) > 30 else "")
        desc = f"为 {issue_key} 添加评论：{preview}"
        task = get_queue().enqueue("add_comment", issue_key, payload, desc)
        return _enqueue_response("添加评论", task)
    except RiskError as e:
        return str(e)
    except Exception as e:
        return f"入队失败: {type(e).__name__}: {e}"


@mcp.tool()
def jira_worklog_add_append(
    issue_key: str,
    time_spent: str,
    comment: str | None = None,
    started: str | None = None,
) -> str:
    """为 JIRA 事务添加工作日志操作追加到待执行队列。

    Args:
        issue_key: 事务 Key
        time_spent: 耗时，如 "2h"、"30m"、"1d 4h"
        comment: 可选，工作描述
        started: 可选，开始时间，ISO 格式，不传则默认当前时间
    Returns:
        入队结果与批次信息
    """
    try:
        payload = {
            "issue_key": issue_key,
            "time_spent": time_spent,
            "comment": comment,
            "started": started,
        }
        desc = f"为 {issue_key} 添加工作日志 {time_spent}"
        task = get_queue().enqueue("add_worklog", issue_key, payload, desc)
        return _enqueue_response("添加工作日志", task)
    except RiskError as e:
        return str(e)
    except Exception as e:
        return f"入队失败: {type(e).__name__}: {e}"


@mcp.tool()
def jira_issuelink_add_append(
    issue_key: str,
    target_issue_key: str,
    link_type: str,
) -> str:
    """为 JIRA 事务添加关联链接操作追加到待执行队列。

    Args:
        issue_key: 源事务 Key
        target_issue_key: 目标事务 Key
        link_type: 链接类型，如 "Blocks"、"Relates"、"Duplicate"
    Returns:
        入队结果与批次信息
    """
    try:
        payload = {
            "issue_key": issue_key,
            "target_issue_key": target_issue_key,
            "link_type": link_type,
        }
        desc = f"链接 {issue_key} --[{link_type}]--> {target_issue_key}"
        task = get_queue().enqueue("add_issuelink", issue_key, payload, desc)
        return _enqueue_response("添加事务链接", task)
    except RiskError as e:
        return str(e)
    except Exception as e:
        return f"入队失败: {type(e).__name__}: {e}"


# ============================================================
# 四、确认与日志工具
# ============================================================
@mcp.tool()
def jira_show_pending_queue() -> str:
    """打开本地 Web 页面，展示全部待执行任务供用户人工确认。

    调用后立即返回，不会等待用户操作，确认提交在页面独立完成。

    Returns:
        页面已打开的提示信息，不包含确认地址或会话凭据
    """
    try:
        from web import app as web_app

        queue = get_queue()
        if queue.total_pending() == 0:
            return "当前没有待确认的变更任务，无需打开确认页面。"

        queue.present_pending_batches()

        base = web_app.ensure_started()
        confirmation_url = web_app.create_confirmation_url(base)
        try:
            opened = webbrowser.open(confirmation_url)
        except Exception:
            opened = False
        if opened is False:
            return "打开确认页面失败：默认浏览器未接受打开请求，请重试。"
        n = queue.total_pending()
        session_minutes = web_app.confirmation_session_ttl_minutes()
        return (
            f"已在默认浏览器打开确认页面（共 {n} 条待确认变更）。\n"
            f"确认会话在页面打开后 {session_minutes} 分钟内有效；"
            f"过期后请重新调用此工具。\n"
            f"请到浏览器核对清单、输入页面上的验证码并确认。"
            f"我在此等待，不会替你提交或执行。"
        )
    except Exception as e:
        return f"打开确认页面失败: {type(e).__name__}: {e}"


@mcp.tool()
def jira_get_commit_log(limit: CommitLogLimit = 10) -> str:
    """查询历史 Jira 执行及人工对账日志。

    Args:
        limit: 返回最近 N 条记录，最小为 1，默认 10
    Returns:
        历史记录列表（JSON 文本），包含正常执行记录及 record_type 为
        reconciliation 的人工对账记录
    """
    try:
        logs = jira_executor.read_commit_log(limit)
        if not logs:
            return "暂无历史提交记录。"
        return redact(json.dumps(logs, ensure_ascii=False, indent=2))
    except Exception as e:
        return _format_mcp_error("查询提交日志失败", e)


# ============================================================
# 五、Resource 只读资源
# ============================================================
def _read_knowledge(name: str) -> str:
    path = os.path.join(_KNOWLEDGE_DIR, name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"（知识库文件加载失败: {e}）"


@mcp.resource("skill://jira-operation-rule")
def res_operation_rule() -> str:
    """JIRA 操作强制安全规范：读写分离、禁止删除、批量流程、高危约束。"""
    return _read_knowledge("jira-operation-rule.md")


@mcp.resource("skill://jira-field-guide")
def res_field_guide() -> str:
    """JIRA 字段手册：标准字段、自定义字段格式、字段 ID 对应关系。"""
    return _read_knowledge("jira-field-guide.md")


@mcp.resource("skill://risk-classification")
def res_risk_classification() -> str:
    """JIRA 操作风险分级标准：低/中/高三级划分与处理策略。"""
    return _read_knowledge("risk-classification.md")


@mcp.resource("config://service-info")
def res_service_info() -> str:
    """服务元信息（不含任何凭证）。"""
    cfg = get_config()
    info = {
        "service": "jira-safe-mcp",
        "version": SERVICE_VERSION,
        "web_port_default": cfg.web_port,
        "queue_expire_minutes": cfg.expire_minutes,
        "max_txt_bytes": cfg.max_txt_bytes,
        "delete_operations": "permanently disabled",
        "note": "JIRA 地址与令牌仅存于本地配置，绝不通过任何接口暴露。",
    }
    return json.dumps(info, ensure_ascii=False, indent=2)


# ============================================================
# 六、Prompt 预设模板
# ============================================================
@mcp.prompt()
def batch_modify_workflow() -> str:
    """标准批量 JIRA 修改完整流程。"""
    return PROMPTS["batch-modify-workflow"]


@mcp.prompt()
def create_then_modify_workflow() -> str:
    """创建后修改的两阶段异步工作流。"""
    return PROMPTS["create-then-modify-workflow"]


@mcp.prompt()
def safe_query_guide() -> str:
    """安全查询规范：JQL 分页、大结果集处理。"""
    return PROMPTS["safe-query-guide"]


@mcp.prompt()
def no_delete_checklist() -> str:
    """执行修改前的自检清单。"""
    return PROMPTS["no-delete-checklist"]


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    get_logger().info(f"jira-safe-mcp v{SERVICE_VERSION} 启动（STDIO）")
    mcp.run(transport="stdio")
