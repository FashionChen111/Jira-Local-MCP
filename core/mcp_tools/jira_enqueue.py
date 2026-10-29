"""面向 MCP 的 Jira 不可变操作入队函数；执行须经本地 Web 审核提交。"""

from __future__ import annotations

import os
from typing import Any, Literal

from core.operations import OperationRecord, get_services
from core.runtime_logger import redact


def _error(prefix: str, exc: Exception) -> str:
    return f"{prefix}: {redact(type(exc).__name__ + ': ' + str(exc))}"


def _response(operation: OperationRecord) -> str:
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
    issue_type_id: str,
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
        issue_type_id: 项目创建元数据返回的事务类型 ID。
        summary: 事务标题。
        description: 可选的内联事务描述；未提供文件时使用该值。
        description_file: 可选的本地 ``.txt`` 文件绝对路径；其内容拥有最高
            优先级，并在入队时固化为文本快照。
        priority: 可选的 Jira 优先级名称。
        assignee: 可选的 Jira Server/Data Center 经办人用户名。
        extra_fields: 可选的其他 Jira 字段映射。不得包含已有专用参数负责的
            ``project``、``issuetype``、``summary``、``description``、
            ``priority`` 或 ``assignee``；这些字段必须使用对应顶层参数。

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
        "issue_type_id": issue_type_id,
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
        f"新建 {issue_type_id}「{summary}」于项目 {project_key}",
        "jira_issue_create_append",
    )


def jira_issue_clone_append(
    issue_key: str,
    summary: str | None = None,
    clone_subtasks: bool = False,
    clone_attachments: bool = False,
    field_overrides: dict | None = None,
) -> str:
    """将克隆 Jira 事务操作加入本地审核队列；仅入队，不会直接写入 Jira。

    参数：
        issue_key: 源 Jira 事务的真实 Key；克隆来源不接受待创建 operation ID。
        summary: 可选的新事务标题。
        clone_subtasks: 必须为 False；递归克隆子任务不支持，True 立即拒绝且不入队。
        clone_attachments: 是否在经审核的操作中克隆附件，默认为 ``False``。
        field_overrides: 补充或覆盖创建字段，禁止 project、issuetype、summary；description
            支持文本文件快照，禁止待上传附件引用；单个子任务默认保留原 parent。

    返回：
        已入队的 operation ID、基础风险等级及“尚未执行”提示；执行前必须由
        用户在本地 Web 审核页明确提交。该 operation ID 可立即作为后续修改
        Tool 的待创建事务引用。新事务已创建而附件处理异常时，执行日志记录
        warning 和附件结果列表，仍解析新事务依赖；不得自动重放整个克隆。

    副作用：
        仅在本地 SQLite 中创建待审核 operation；本次调用不写入 Jira。
    """
    payload = {
        "summary": summary,
        "clone_subtasks": clone_subtasks,
        "clone_attachments": clone_attachments,
        "field_overrides": field_overrides or {},
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
    """说明移动事务能力限制，立即拒绝且不入队。

    参数：
        issue_key: 源事务 Key。
        target_project_key: 目标项目 Key。
        target_issue_type: 可选的目标事务类型；保留公开签名。

    返回：
        不支持且未入队的明确提示；请在 Jira 页面人工移动。

    副作用：
        不修改 Jira，不创建本地记录。其他 append 工具仅入队、尚未执行，
        需要本地 Web 人工审核提交。
    """
    return "不支持且未入队：Jira Server/Data Center REST API 不支持移动事务，请在 Jira 页面人工处理。"


def jira_issue_type_change_append(
    issue_key: str, target_issue_type_id: str, fields_mapping: dict | None = None
) -> str:
    """将 Jira 事务类型变更加入本地审核队列；仅入队，不会直接写入 Jira。

    参数：
        issue_key: Jira 事务 Key，或创建/克隆 operation ID。
        target_issue_type_id: 项目元数据返回的目标事务类型 ID。
        fields_mapping: 可选的字段映射，用于适配不同事务类型的字段差异。
            其中 description 支持入队时文本文件快照及同事务附件引用语法。

    返回：
        已入队的 operation ID、基础风险等级及“尚未执行”提示；执行前必须由
        用户在本地 Web 审核页明确提交。

    副作用：
        仅在本地 SQLite 中创建待审核 operation；本次调用不写入 Jira。
    """
    payload = {
        "target_issue_type_id": target_issue_type_id,
        "fields_mapping": fields_mapping or {},
    }
    return _enqueue(
        "change_type",
        issue_key,
        payload,
        f"变更 {issue_key} 事务类型 → {target_issue_type_id}",
        "jira_issue_type_change_append",
    )


def jira_issue_transition_append(
    issue_key: str,
    transition_id: str,
    comment: str | None = None,
    fields: dict | None = None,
) -> str:
    """将 Jira 工作流状态流转加入本地审核队列；仅入队，不会直接写入 Jira。

    参数：
        issue_key: Jira 事务 Key，或创建/克隆 operation ID。
        transition_id: 该真实事务当前可用的工作流流转 ID；执行前重新校验。
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
        "transition_id": transition_id,
        "comment": comment,
        "fields": fields or {},
    }
    return _enqueue(
        "transition_issue",
        issue_key,
        payload,
        f"{issue_key} 状态流转「{transition_id}」",
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
            文件名必须为非空白文本，不得包含 NUL，且不超过 1024 个字符。

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
    adjust_estimate: Literal["auto", "leave", "new", "manual"] = "auto",
    new_estimate: str | None = None,
    reduce_by: str | None = None,
) -> str:
    """将 Jira 工作日志添加操作加入本地审核队列；仅入队，不会直接写入 Jira。

    参数：
        issue_key: Jira 事务 Key，或创建/克隆 operation ID。
        time_spent: Jira 耗时表达式，例如 ``2h`` 或 ``1d 4h``。
        comment: 可选的工作说明。
            可使用 ``[^{{attachment:op-…}}]`` 或 ``!{{attachment:op-…}}!``
            引用同一事务的附件上传 operation。
        started: 带明确时区的合法 ISO 开始时间；省略时由 SDK 使用当前时间。
        adjust_estimate: 剩余估算模式：auto 自动减少、leave 保持、new 重设、manual 指定减少量。
        new_estimate: 仅 new 模式必须提供的剩余估算，例如 2h。
        reduce_by: 仅 manual 模式必须提供的减少量，例如 1h；与 new_estimate 互斥。

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
        "adjust_estimate": adjust_estimate,
        "new_estimate": new_estimate,
        "reduce_by": reduce_by,
    }
    return _enqueue(
        "add_worklog",
        issue_key,
        payload,
        f"为 {issue_key} 添加工作日志 {time_spent}；估算模式 {adjust_estimate}",
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
