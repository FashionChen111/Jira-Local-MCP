"""面向 Agent 的 Jira 元数据只读接口。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from core import jira_metadata as meta

StartAt = Annotated[int, Field(strict=True, ge=0)]
MaxResults = Annotated[int, Field(strict=True, ge=1, le=100)]


def _respond(read: Callable[[], dict[str, JsonValue]]) -> dict[str, JsonValue]:
    try:
        return read()
    except Exception as exc:
        return meta.error_response(exc)


def jira_meta_server_settings(section: Literal['attachments', 'time_tracking', 'features']) -> dict[str, JsonValue]:
    """按主题查询附件、工时或功能开关。

    参数：
        section: attachments 返回附件开关/上限；time_tracking 返回工时配置；features 返回链接/子任务开关。

    返回：
        当前可见事实的 JSON 响应及必要分页信息；失败保留脱敏后的来源与错误。

    副作用：
        只读，不会修改 Jira，也不会创建或修改本地队列。
    """
    return _respond(lambda: meta.server_settings(section))


def jira_meta_user(username: str | None = None) -> dict[str, JsonValue]:
    """查询当前令牌身份或精确用户身份。

    参数：
        username: 可选的 Server/DC 用户名；省略时查询当前令牌用户，不展开组和角色。

    返回：
        当前可见事实的 JSON 响应及必要分页信息；失败保留脱敏后的来源与错误。

    副作用：
        只读，不会修改 Jira，也不会创建或修改本地队列。
    """
    return _respond(lambda: meta.user(username))


def jira_meta_search_users(query: str, scope: meta.UserSearchScope = meta.DirectoryScope(), start_at: StartAt = 0, max_results: MaxResults = 20) -> dict[str, JsonValue]:
    """按关键词查询用户身份、事务可浏览用户或用户字段候选。

    参数：
        query: 必填姓名或用户名关键词，不能无约束枚举。
        scope: directory 查身份；issue 必须提供真实 issue_key；field 必须提供写上下文和 field_id，使用固定 picker，返回 unverified 身份建议，不证明字段可选，不回退全局用户。
        start_at: 源分页起点，默认 0；字段 picker 只允许 0。
        max_results: 单页上限，默认 20，范围 1–100；无可靠总数时不伪造完整性。

    返回：
        当前可见事实的 JSON 响应及必要分页信息；失败保留脱敏后的来源与错误。

    副作用：
        只读，不会修改 Jira，也不会创建或修改本地队列。
    """
    return _respond(lambda: meta.search_users(query, scope, start_at, max_results))


def jira_meta_search_assignable_users(context: meta.ProjectOrIssueContext, query: str, start_at: StartAt = 0, max_results: MaxResults = 20) -> dict[str, JsonValue]:
    """查询指定项目或事务的可指派用户。

    参数：
        context: project 必须提供 project_key；issue 必须提供真实 issue_key。
        query: 必填的姓名或用户名关键词。
        start_at: 源分页起点，默认 0。
        max_results: 每页最多条数，默认 20，范围 1–100。

    返回：
        当前可见事实的 JSON 响应及必要分页信息；失败保留脱敏后的来源与错误。

    副作用：
        只读，不会修改 Jira，也不会创建或修改本地队列。
    """
    return _respond(lambda: meta.assignable_users(context, query, start_at, max_results))


def jira_meta_projects(query: str | None = None, start_at: StartAt = 0, max_results: MaxResults = 20) -> dict[str, JsonValue]:
    """发现当前用户可见的项目。

    参数：
        query: 可选项目名称或 Key 线索，省略时返回一页目录。
        start_at: 本地过滤后的分页起点，默认 0。
        max_results: 每页最多条数，默认 20，范围 1–100。

    返回：
        当前可见事实的 JSON 响应及必要分页信息；失败保留脱敏后的来源与错误。

    副作用：
        只读，不会修改 Jira，也不会创建或修改本地队列。
    """
    return _respond(lambda: meta.projects(query, start_at, max_results))


def jira_meta_issue_types(project_key: str, purpose: Literal['create', 'configured'] = 'create', start_at: StartAt = 0, max_results: MaxResults = 50) -> dict[str, JsonValue]:
    """查询一个项目的创建类型或配置类型。

    参数：
        project_key: 必填项目 Key，不能查询全局类型。
        purpose: create 查询当前凭证创建元数据；configured 仅查询项目配置，不代表可创建。
        start_at: 分页起点，默认 0。
        max_results: 每页最多条数，默认 50，范围 1–100。

    返回：
        当前可见事实的 JSON 响应及必要分页信息；失败保留脱敏后的来源与错误。

    副作用：
        只读，不会修改 Jira，也不会创建或修改本地队列。
    """
    return _respond(lambda: meta.issue_types(project_key, purpose, start_at, max_results))


def jira_meta_issue_transitions(issue_key: str, start_at: StartAt = 0, max_results: MaxResults = 50) -> dict[str, JsonValue]:
    """查询具体事务此刻可用的工作流动作。

    参数：
        issue_key: 必填真实 Jira Key，不能使用 operation ID。
        start_at: 本地分页起点，默认 0。
        max_results: 每页最多条数，默认 50，范围 1–100；返回动作 ID 而非仅状态 ID。

    返回：
        当前可见事实的 JSON 响应及必要分页信息；失败保留脱敏后的来源与错误。

    副作用：
        只读，不会修改 Jira，也不会创建或修改本地队列。
    """
    return _respond(lambda: meta.transitions(issue_key, start_at, max_results))


def jira_meta_fields(context: meta.WriteContext, query: str | None = None, required_only: bool = False, start_at: StartAt = 0, max_results: MaxResults = 50) -> dict[str, JsonValue]:
    """渐进查询创建、编辑或流转上下文中的字段摘要。

    参数：
        context: create 必须提供项目与类型 ID；edit 必须提供真实事务；transition 再提供动作 ID。
        query: 可选字段名称或 ID 线索，不展开候选值。
        required_only: 是否仅返回必填字段，默认 False；创建必填必须读完源分页。
        start_at: 源或本地分页起点，使用响应 next_start_at 续页，空页不代表结束。
        max_results: 每页最多条数，默认 50，范围 1–100。

    返回：
        当前可见事实的 JSON 响应及必要分页信息；失败保留脱敏后的来源与错误。

    副作用：
        只读，不会修改 Jira，也不会创建或修改本地队列。
    """
    return _respond(lambda: meta.fields(context, query, required_only, start_at, max_results))


def jira_meta_field(context: meta.FieldContext, field_id: str, options_query: str | None = None, options_start_at: StartAt = 0, options_max_results: MaxResults = 100) -> dict[str, JsonValue]:
    """读取一个字段的元描述与该上下文已有的候选值。

    参数：
        context: create/edit/transition 写上下文；catalog 仅查字段定义，不提供可写结论或选项过滤。
        field_id: 必填字段 ID。
        options_query: 可选候选线索，匹配级联子项时保留父路径。
        options_start_at: 按父子节点计数的选项分页起点，默认 0。
        options_max_results: 选项节点上限，默认 100，范围 1–100；小树一次完整返回。

    返回：
        当前可见事实的 JSON 响应及必要分页信息；失败保留脱敏后的来源与错误。

    副作用：
        只读，不会修改 Jira，也不会创建或修改本地队列。
    """
    return _respond(lambda: meta.field_detail(context, field_id, options_query, options_start_at, options_max_results))


def jira_meta_link_types(query: str | None = None, start_at: StartAt = 0, max_results: MaxResults = 50) -> dict[str, JsonValue]:
    """查询链接类型名称和双向关系描述。

    参数：
        query: 可选类型名称或 ID 线索。
        start_at: 本地分页起点，默认 0。
        max_results: 每页最多条数，默认 50，范围 1–100；链接类型本身是全局配置。

    返回：
        当前可见事实的 JSON 响应及必要分页信息；失败保留脱敏后的来源与错误。

    副作用：
        只读，不会修改 Jira，也不会创建或修改本地队列。
    """
    return _respond(lambda: meta.link_types(query, start_at, max_results))


def jira_meta_permissions(context: meta.ProjectOrIssueContext, permission_keys: list[str]) -> dict[str, JsonValue]:
    """读取当前用户在项目或事务范围内指定的权限事实。

    参数：
        context: project 提供 project_key；issue 提供真实 issue_key，不提供全局默认范围。
        permission_keys: 必填的 1–20 个非空且不重复的权限 key；未知项不推断为 false。

    返回：
        当前可见事实的 JSON 响应及必要分页信息；失败保留脱敏后的来源与错误。

    副作用：
        只读，不会修改 Jira，也不会创建或修改本地队列。
    """
    return _respond(lambda: meta.permissions(context, permission_keys))
