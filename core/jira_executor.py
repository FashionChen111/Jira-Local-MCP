"""JIRA 操作执行引擎 —— 唯一持有真实写路径。

仅在用户通过 Web 页面确认后由 /api/commit 调用。执行前：
  1. nonce 校验（整批级）；
  2. JIRA 探活；
  3. 按用户指定顺序逐条执行，每条先过删除语义拦截，单条独立失败。

执行结果写入 commit_log.json（环形保留最近 N 条）+ 审计日志。
已提交任务从队列移除。失败任务不可重试（由 Agent 分析后重新入队）。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import threading
import time
import traceback
from typing import Any, Iterator

import portalocker

from config import get_config
from core import commit_guard, jira_client, risk_engine
from core.audit_logger import audit, get_logger, redact
from core.queue_manager import PendingTask, ReconciliationResult, get_queue
from core.textfile_loader import TextFileError, read_text_file

MOVE_UNSUPPORTED_MSG = (
    "JIRA Server / Data Center 的 REST API 不支持移动事务（move），"
    "请在 JIRA 网页端手动操作，或改用克隆到目标项目再关闭原事务的方式。"
)


class CommitError(Exception):
    """提交级错误（如 nonce 错误、探活失败），面向 Web 层。"""


class CommitBusyError(CommitError):
    """另一个批次正在执行，调用方应稍后重试。"""


# ---------- description 解析（file 优先）----------
def _resolve_description(payload: dict[str, Any]) -> str | None:
    """解析 create / field_update 中的 description，file 优先于 inline。"""
    # create_issue 风格：description_file / description
    description_file = payload.get("description_file")
    if description_file:
        if not isinstance(description_file, str):
            raise TextFileError("description_file 必须是文件路径字符串")
        return read_text_file(description_file)
    if "description" in payload:
        desc = payload["description"]
        if isinstance(desc, dict):
            file_path = desc.get("file")
            if not isinstance(file_path, str) or not file_path:
                raise TextFileError("description 字典必须包含非空的 file 路径")
            return read_text_file(file_path)
        if isinstance(desc, str) or desc is None:
            return desc
        raise TextFileError("description 必须是字符串、文件参数或 None")
    return None


def _resolve_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """处理 fields 字典中的 description 文件传参。"""
    out = dict(fields)
    if "description" in out:
        desc = out["description"]
        if isinstance(desc, dict) and desc.get("file"):
            out["description"] = read_text_file(desc["file"])
    return out


# ---------- 各操作处理器：返回 (成功摘要, 生成的 issue_key 或 None) ----------
def _op_create_issue(client: Any, task: PendingTask) -> tuple[str, str | None]:
    p = task.payload
    fields: dict[str, Any] = {
        "project": {"key": p["project_key"]},
        "issuetype": {"name": p["issue_type"]},
        "summary": p["summary"],
    }
    desc = _resolve_description(p)
    if desc is not None:
        fields["description"] = desc
    if p.get("priority"):
        fields["priority"] = {"name": p["priority"]}
    if p.get("assignee"):
        fields["assignee"] = {"name": p["assignee"]}
    if p.get("extra_fields"):
        fields.update(p["extra_fields"])
    issue = client.create_issue(fields=fields)
    return f"已创建事务 {issue.key}", issue.key


def _op_clone_issue(client: Any, task: PendingTask) -> tuple[str, str | None]:
    p = task.payload
    src = client.issue(p["issue_key"])
    sf = src.fields
    new_summary = p.get("summary") or f"[克隆] {sf.summary}"
    fields: dict[str, Any] = {
        "project": {"key": sf.project.key},
        "issuetype": {"name": sf.issuetype.name},
        "summary": new_summary,
    }
    if getattr(sf, "description", None):
        fields["description"] = sf.description
    if getattr(sf, "priority", None):
        fields["priority"] = {"name": sf.priority.name}
    new_issue = client.create_issue(fields=fields)

    extras = []
    if p.get("clone_attachments"):
        try:
            for att in getattr(sf, "attachment", []) or []:
                data = att.get()
                client.add_attachment(
                    issue=new_issue.key, attachment=data, filename=att.filename
                )
            extras.append("附件已复制")
        except Exception as e:
            extras.append(f"附件复制失败({type(e).__name__})")
    # 子任务克隆需要目标父子关系，Server 差异较大，此处仅在需要时提示
    if p.get("clone_subtasks"):
        extras.append("子任务克隆需人工核对（未自动执行）")

    suffix = ("；" + "，".join(extras)) if extras else ""
    return f"已克隆 {p['issue_key']} → {new_issue.key}{suffix}", new_issue.key


def _op_move_issue(client: Any, task: PendingTask) -> tuple[str, str | None]:
    # Server / DC REST 不支持，明确失败
    raise RuntimeError(MOVE_UNSUPPORTED_MSG)


def _op_change_type(client: Any, task: PendingTask) -> tuple[str, str | None]:
    p = task.payload
    issue = client.issue(p["issue_key"])
    fields: dict[str, Any] = {"issuetype": {"name": p["target_issue_type"]}}
    if p.get("fields_mapping"):
        fields.update(p["fields_mapping"])
    try:
        issue.update(fields=fields)
    except Exception as e:
        raise RuntimeError(
            f"变更事务类型失败（子任务↔标准任务的跨层级转换 REST 不支持）: "
            f"{type(e).__name__}"
        ) from e
    return f"{p['issue_key']} 类型已变更为 {p['target_issue_type']}", None


def _op_transition_issue(client: Any, task: PendingTask) -> tuple[str, str | None]:
    p = task.payload
    issue = client.issue(p["issue_key"])
    # 用名称匹配可用流转
    transitions = client.transitions(issue)
    target = None
    want = str(p["transition_name"]).strip().lower()
    for t in transitions:
        if t["name"].strip().lower() == want:
            target = t["id"]
            break
    if target is None:
        avail = ", ".join(t["name"] for t in transitions) or "（无可用流转）"
        raise RuntimeError(
            f"未找到流转「{p['transition_name']}」，当前可用流转：{avail}"
        )
    kwargs: dict[str, Any] = {}
    if p.get("fields"):
        kwargs["fields"] = _resolve_fields(p["fields"])
    if p.get("comment"):
        kwargs["comment"] = p["comment"]
    client.transition_issue(issue, target, **kwargs)
    return f"{p['issue_key']} 状态已流转：{p['transition_name']}", None


def _op_update_field(client: Any, task: PendingTask) -> tuple[str, str | None]:
    p = task.payload
    issue = client.issue(p["issue_key"])
    fields = _resolve_fields(p["fields"])
    issue.update(fields=fields)
    keys = ", ".join(fields.keys())
    return f"{p['issue_key']} 已更新字段：{keys}", None


def _op_update_custom_field(client: Any, task: PendingTask) -> tuple[str, str | None]:
    p = task.payload
    issue = client.issue(p["issue_key"])
    fields = _resolve_fields(p["custom_fields"])
    issue.update(fields=fields)
    keys = ", ".join(fields.keys())
    return f"{p['issue_key']} 已更新自定义字段：{keys}", None


def _op_add_attachment(client: Any, task: PendingTask) -> tuple[str, str | None]:
    p = task.payload
    fp = p["file_path"]
    if not os.path.isabs(fp) or not os.path.isfile(fp):
        raise RuntimeError(f"附件文件不存在或非绝对路径: {fp}")
    with open(fp, "rb") as fh:
        client.add_attachment(
            issue=p["issue_key"], attachment=fh, filename=p.get("file_name")
        )
    return f"{p['issue_key']} 已添加附件 {p.get('file_name') or os.path.basename(fp)}", None


def _op_add_comment(client: Any, task: PendingTask) -> tuple[str, str | None]:
    p = task.payload
    client.add_comment(p["issue_key"], p["body"])
    return f"{p['issue_key']} 已添加评论", None


def _op_add_worklog(client: Any, task: PendingTask) -> tuple[str, str | None]:
    p = task.payload
    kwargs: dict[str, Any] = {"timeSpent": p["time_spent"]}
    if p.get("comment"):
        kwargs["comment"] = p["comment"]
    if p.get("started"):
        # jira 库需要 datetime；这里透传字符串由库解析失败则报错
        kwargs["started"] = p["started"]
    client.add_worklog(issue=p["issue_key"], **kwargs)
    return f"{p['issue_key']} 已添加工作日志 {p['time_spent']}", None


def _op_add_issuelink(client: Any, task: PendingTask) -> tuple[str, str | None]:
    p = task.payload
    client.create_issue_link(
        type=p["link_type"],
        inwardIssue=p["issue_key"],
        outwardIssue=p["target_issue_key"],
    )
    return (
        f"已链接 {p['issue_key']} --[{p['link_type']}]--> {p['target_issue_key']}",
        None,
    )


_HANDLERS = {
    "create_issue": _op_create_issue,
    "clone_issue": _op_clone_issue,
    "move_issue": _op_move_issue,
    "change_type": _op_change_type,
    "transition_issue": _op_transition_issue,
    "update_field": _op_update_field,
    "update_custom_field": _op_update_custom_field,
    "add_attachment": _op_add_attachment,
    "add_comment": _op_add_comment,
    "add_worklog": _op_add_worklog,
    "add_issuelink": _op_add_issuelink,
}


# ---------- commit_log 环形持久化 ----------
COMMIT_LOG_LIMIT_MIN = 1
_COMMIT_LOG_LOCK = threading.Lock()


@contextmanager
def _commit_log_file_lock(path: str) -> Iterator[None]:
    """串行化当前进程及多个进程间的提交日志访问。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with _COMMIT_LOG_LOCK:
        with portalocker.Lock(path + ".lock", mode="a", timeout=10):
            yield


def validate_commit_log_limit(limit: int) -> int:
    """校验提交日志查询数量，请求超量时返回现有全部记录。"""
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < COMMIT_LOG_LIMIT_MIN
    ):
        raise ValueError(f"limit 必须是大于等于 {COMMIT_LOG_LIMIT_MIN} 的整数")
    return limit


def _append_commit_log(record: dict[str, Any]) -> None:
    cfg = get_config()
    path = cfg.commit_log_file
    try:
        with _commit_log_file_lock(path):
            logs: list[dict[str, Any]] = []
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        logs = json.load(f)
                except Exception:
                    logs = []
            logs.append(record)
            logs = logs[-cfg.commit_log_keep :]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(logs, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
    except Exception as e:
        get_logger().error(f"写 commit_log 失败: {e}")


def append_reconciliation_log(
    batch_id: str, result: ReconciliationResult
) -> dict[str, Any]:
    """追加人工对账记录；该记录不代表 Jira API 的可观测执行结果。"""
    original = result.original_task
    recovery_task_id = (
        result.recovery_task.task_id if result.recovery_task is not None else None
    )
    summaries = {
        "succeeded": "人工确认该操作已在 Jira 生效，任务已归档",
        "not_applied": "人工确认该操作未在 Jira 生效，已生成恢复批次",
        "abandoned": "人工确认放弃该操作，任务已归档",
    }
    record = {
        "record_type": "reconciliation",
        "batch_id": batch_id,
        "committed_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "ts": time.time(),
        "results": [
            {
                "task_id": original.task_id,
                "operation_type": original.operation_type,
                "issue_key": original.issue_key,
                "status": f"reconciled_{result.resolution}",
                "resolution": result.resolution,
                "execution_id": original.execution_id,
                "recovery_task_id": recovery_task_id,
                "recovery_batch_id": result.recovery_batch_id,
                "summary": summaries[result.resolution],
            }
        ],
    }
    _append_commit_log(record)
    return record


def read_commit_log(limit: int = 10) -> list[dict[str, Any]]:
    limit = validate_commit_log_limit(limit)
    cfg = get_config()
    path = cfg.commit_log_file
    try:
        with _commit_log_file_lock(path):
            if not os.path.exists(path):
                return []
            with open(path, "r", encoding="utf-8") as f:
                logs = json.load(f)
    except Exception:
        return []
    return logs[-limit:][::-1]  # 最近的在前


# ---------- 主入口 ----------
def _execute_batch_locked(
    batch_id: str, ordered_task_ids: list[str], nonce: str
) -> dict[str, Any]:
    """执行一个批次中用户选中且排序后的任务。

    Returns:
        {"batch_id","committed_at","results":[{task_id,operation_type,issue_key,
         status,summary,error}], "ok_count","fail_count"}

    Raises:
        CommitError: nonce 错误 / 批次不存在 / 探活失败。
    """

    # 探活（spec §8.5）
    if not jira_client.ping():
        audit("execute_aborted", batch_id=batch_id, reason="jira_unreachable")
        raise CommitError("JIRA 连接不可用，本批次未执行")
    client = jira_client.get_client()

    queue = get_queue()
    try:
        ordered = queue.reserve_tasks(batch_id, ordered_task_ids, nonce)
    except PermissionError as exc:
        audit("confirm_rejected", batch_id=batch_id, reason="nonce_mismatch")
        raise CommitError(str(exc)) from exc
    except ValueError as exc:
        raise CommitError(str(exc)) from exc

    audit("confirm", batch_id=batch_id, task_ids=ordered_task_ids)

    results: list[dict[str, Any]] = []
    ok_count = 0

    for task in ordered:
        started = time.time()
        result: dict[str, Any] = {
            "task_id": task.task_id,
            "operation_type": task.operation_type,
            "issue_key": task.issue_key,
            "risk_level": task.risk_level,
        }
        try:
            # 每条执行前防御式删除拦截
            risk_engine.assert_not_delete(task.operation_type, task.payload)
            handler = _HANDLERS.get(task.operation_type)
            if handler is None:
                raise RuntimeError(f"不支持的操作类型: {task.operation_type}")
            summary, new_key = handler(client, task)
            result.update(
                status="success",
                summary=summary,
                new_issue_key=new_key,
                elapsed=round(time.time() - started, 3),
            )
            ok_count += 1
            audit(
                "result",
                batch_id=batch_id,
                task_id=task.task_id,
                operation_type=task.operation_type,
                status="success",
                new_issue_key=new_key,
                summary=summary,
            )
        except (risk_engine.RiskError, TextFileError) as e:
            msg = redact(str(e))
            result.update(status="failed", error=msg, elapsed=round(time.time() - started, 3))
            audit("error", batch_id=batch_id, task_id=task.task_id, error=msg)
        except Exception as e:
            msg = redact(f"{type(e).__name__}: {e}")
            get_logger().error(
                f"任务 {task.task_id} 执行失败: {msg}\n{redact(traceback.format_exc())}"
            )
            result.update(status="failed", error=msg, elapsed=round(time.time() - started, 3))
            audit("error", batch_id=batch_id, task_id=task.task_id, error=msg)
        results.append(result)

    # 从队列移除已提交任务（无论成败，失败不可重试）
    queue.remove_tasks(batch_id, [t.task_id for t in ordered])

    record = {
        "batch_id": batch_id,
        "committed_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "ts": time.time(),
        "results": results,
        "ok_count": ok_count,
        "fail_count": len(results) - ok_count,
    }
    _append_commit_log(record)
    audit(
        "execute",
        batch_id=batch_id,
        ok_count=ok_count,
        fail_count=len(results) - ok_count,
    )
    return record


def execute_batch(
    batch_id: str, ordered_task_ids: list[str], nonce: str
) -> dict[str, Any]:
    """在全局提交锁内执行批次，禁止同批次或多批次并发写 Jira。"""
    try:
        with commit_guard.execution_slot():
            return _execute_batch_locked(batch_id, ordered_task_ids, nonce)
    except commit_guard.ExecutionBusyError as exc:
        audit("confirm_rejected", batch_id=batch_id, reason="execution_busy")
        raise CommitBusyError(str(exc)) from exc
