"""风险识别引擎 + 两阶段校验 + 删除语义拦截。

三项职责：
  1. classify_risk() —— 按 spec §6.3 给操作分级 low/medium/high。
  2. check_two_phase() —— 入队修改类任务时拒绝"同批次针对本批次新建事务"的操作。
  3. assert_not_delete() —— 防御式：白名单外操作类型、含删除语义的 payload 一律拒绝。
"""
from __future__ import annotations

import re
from typing import Any

# —— 允许的操作类型白名单（永不含任何删除语义）——
CREATE_OPS = {"create_issue", "clone_issue"}
MODIFY_OPS = {
    "move_issue",
    "change_type",
    "transition_issue",
    "update_field",
    "update_custom_field",
    "add_attachment",
    "add_comment",
    "add_worklog",
    "add_issuelink",
}
ALLOWED_OPS = CREATE_OPS | MODIFY_OPS

# —— 静态风险映射（spec §6.3）——
LOW_OPS = {"add_comment", "add_worklog", "add_issuelink"}
MEDIUM_OPS = {
    "create_issue",
    "clone_issue",
    "update_field",
    "update_custom_field",
    "add_attachment",
}
HIGH_OPS = {"move_issue", "change_type", "transition_issue"}

# 关闭/解决类流转关键词（高危附加说明）
_CLOSE_KEYWORDS = ["关闭", "解决", "完成", "close", "resolve", "done", "cancel"]

# 批量字段修改升级为高危的阈值：同批次对同一 issue 的字段修改任务数
_BULK_FIELD_THRESHOLD = 5

# 删除语义词（payload 深度扫描）
_DELETE_PATTERN = re.compile(r"delete|remove|purge|删除|移除", re.IGNORECASE)

# 合法 issue key 形态
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


class RiskError(Exception):
    """风险 / 安全校验拒绝异常，消息面向 LLM 引导。"""


def classify_risk(operation_type: str, payload: dict[str, Any]) -> tuple[str, str]:
    """返回 (risk_level, reason)。"""
    if operation_type in LOW_OPS:
        return "low", "低风险：仅新增内容，不改动既有数据"

    if operation_type == "transition_issue":
        name = str(payload.get("transition_name", ""))
        if any(k in name.lower() for k in _CLOSE_KEYWORDS):
            return "high", f"高风险：状态流转（关闭/解决类）「{name}」不可轻易撤销"
        return "high", f"高风险：状态流转「{name}」会改变工作流状态"

    if operation_type == "move_issue":
        return "high", "高风险：移动事务会改变项目归属"

    if operation_type == "change_type":
        return "high", "高风险：变更事务类型可能导致字段丢失或层级变化"

    if operation_type in MEDIUM_OPS:
        return "medium", "中风险：创建或修改事务数据"

    # 未知类型（理论上已被 assert_not_delete 拦截）
    return "high", "未知操作类型，按最高风险处理"


def maybe_escalate_bulk_fields(tasks: list[Any]) -> None:
    """对同批次同一 issue 的字段修改任务数超阈值者，升级为高危（批量字段修改）。

    tasks 为 PendingTask 列表（需具备 operation_type / issue_key / risk_level 属性）。
    就地修改 risk_level。
    """
    counter: dict[str, int] = {}
    for t in tasks:
        if t.operation_type in ("update_field", "update_custom_field") and t.issue_key:
            counter[t.issue_key] = counter.get(t.issue_key, 0) + 1
    hot = {k for k, c in counter.items() if c >= _BULK_FIELD_THRESHOLD}
    for t in tasks:
        if (
            t.operation_type in ("update_field", "update_custom_field")
            and t.issue_key in hot
        ):
            t.risk_level = "high"


def check_two_phase(
    operation_type: str, issue_key: str | None, batch_tasks: list[Any]
) -> None:
    """两阶段校验：禁止同批次内出现针对"本批次新建事务"的修改。

    规则：若同批次已存在 create/clone 任务，则本批次任何修改类任务的
    issue_key 必须是一个合法且真实存在的 key（形态校验）；不得为空、
    不得含占位符（$ / {）。因为新建事务在确认前没有真实 issue_key。

    Raises:
        RiskError: 违反两阶段约束。
    """
    if operation_type not in MODIFY_OPS:
        return  # 只约束修改类
    has_create = any(t.operation_type in CREATE_OPS for t in batch_tasks)
    if not has_create:
        return

    guide = (
        "❌ 违反两阶段工作流：本批次已包含「新建/克隆事务」任务，"
        "不能在同一批次内追加针对新事务的修改操作。\n"
        "正确流程：\n"
        "  1) 先只提交创建批次，等用户在页面确认执行；\n"
        "  2) 调用 jira_get_commit_log 取回真实 issue_key；\n"
        "  3) 用真实 issue_key 在新批次追加修改，再次确认。\n"
        "提示：能在创建时一并设置的字段，请直接写在创建调用里，不要拆到第二阶段。"
    )

    if not issue_key:
        raise RiskError(guide)
    if "$" in issue_key or "{" in issue_key or "}" in issue_key:
        raise RiskError(guide)
    if not _ISSUE_KEY_RE.match(issue_key):
        raise RiskError(
            f"issue_key「{issue_key}」形态非法（应形如 PROJ-123）。\n" + guide
        )


def _scan_delete(value: Any) -> bool:
    """深度扫描 payload 是否含删除语义。"""
    if isinstance(value, str):
        return bool(_DELETE_PATTERN.search(value))
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and _DELETE_PATTERN.search(k):
                return True
            if _scan_delete(v):
                return True
        return False
    if isinstance(value, (list, tuple, set)):
        return any(_scan_delete(v) for v in value)
    return False


def assert_not_delete(operation_type: str, payload: dict[str, Any]) -> None:
    """防御式删除拦截，供 executor 在真实执行前调用。

    Raises:
        RiskError: 操作类型不在白名单，或 payload 含删除语义。
    """
    if operation_type not in ALLOWED_OPS:
        raise RiskError(
            f"操作类型「{operation_type}」不在允许白名单内，拒绝执行（删除类操作永久禁止）。"
        )
    # 操作类型键名扫描
    if _DELETE_PATTERN.search(operation_type):
        raise RiskError(f"检测到删除语义操作「{operation_type}」，拒绝执行。")
    if _scan_delete(payload):
        raise RiskError("检测到 payload 中含删除语义字段，出于安全拒绝执行。")
