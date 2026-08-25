"""Risk classification, target validation, and delete-semantic defence.

三项职责：
  1. classify_risk() —— 给不可编辑的基础 operation 快照分级。
  2. validate_operation_target() —— 只接受真实 issue key 或服务端创建/克隆 operation ID。
  3. assert_not_delete() —— 防御式拒绝白名单外类型和含删除语义的 payload。
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

# 删除语义词（payload 深度扫描）
_DELETE_PATTERN = re.compile(r"delete|remove|purge|删除|移除", re.IGNORECASE)

_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
_OPERATION_ID_RE = re.compile(r"^op-[0-9a-f]{32}$")

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


def validate_operation_target(operation_type: str, issue_key: str | None) -> None:
    """Allow real Jira keys or server-issued create/clone operation IDs."""
    if operation_type not in MODIFY_OPS:
        return

    guide = (
        "修改目标必须使用真实 Jira issue key，或本服务返回的 create_issue/"
        "clone_issue operation ID；不接受自定义占位符。"
    )

    if not issue_key:
        raise RiskError(guide)
    if not _ISSUE_KEY_RE.fullmatch(issue_key) and not _OPERATION_ID_RE.fullmatch(
        issue_key
    ):
        raise RiskError(
            f"issue_key「{issue_key}」形态非法。\n" + guide
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
