"""待执行任务队列管理器。

队列以服务端生成的批次为单位持久化。所有读写同时持有进程内锁和
跨进程文件锁，并在锁内重新加载磁盘状态，避免多个 MCP 进程互相覆盖。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

import portalocker

from config import get_config
from core import commit_guard, risk_engine
from core.audit_logger import audit, get_logger


@dataclass
class PendingTask:
    task_id: str
    batch_id: str
    operation_type: str
    risk_level: str
    issue_key: str | None
    payload: dict[str, Any]
    description: str
    created_at: float
    source: str = ""
    state: str = "pending"
    execution_id: str | None = None
    execution_reserved_at: float | None = None
    recovery_reason: str | None = None
    origin_task_id: str | None = None
    origin_batch_id: str | None = None
    previous_execution_ids: list[str] = field(default_factory=list)
    recovery_attempt: int = 0
    recovered_at: float | None = None

    def to_public_dict(self) -> dict[str, Any]:
        """返回确认页可见字段，不暴露内部执行状态。"""
        data = asdict(self)
        data.pop("state", None)
        return data


@dataclass
class Batch:
    batch_id: str
    nonce: str
    created_at: float
    state: str = "open"
    tasks: list[PendingTask] = field(default_factory=list)
    kind: str = "normal"
    recovery_from: str | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    """一次已落盘的人工对账结果及其原任务快照。"""

    original_task: PendingTask
    resolution: str
    resolved_state: str
    recovery_task: PendingTask | None = None
    recovery_batch_id: str | None = None


class QueueManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._batches: dict[str, Batch] = {}
        self._active_batch_id: str | None = None
        self._last_purge_count = 0
        with self._transaction():
            pass
        self.recover_interrupted_tasks()

    @property
    def _lock_file(self) -> str:
        return get_config().queue_file + ".lock"

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """锁定并刷新队列；退出时原子写回最新状态。"""
        cfg = get_config()
        os.makedirs(os.path.dirname(cfg.queue_file), exist_ok=True)
        with self._lock:
            with portalocker.Lock(self._lock_file, mode="a", timeout=10):
                self._reload_locked()
                self._last_purge_count = self._purge_expired_locked()
                yield
                self._flush_locked()

    def _reload_locked(self) -> None:
        path = get_config().queue_file
        self._batches = {}
        self._active_batch_id = None
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._active_batch_id = raw.get("active_batch_id")
            for item in raw.get("batches", []):
                batch = Batch(
                    batch_id=item["batch_id"],
                    nonce=item["nonce"],
                    created_at=item["created_at"],
                    state=item.get("state", "presented"),
                    tasks=[PendingTask(**task) for task in item.get("tasks", [])],
                    kind=item.get("kind", "normal"),
                    recovery_from=item.get("recovery_from"),
                )
                self._batches[batch.batch_id] = batch
        except Exception as exc:
            get_logger().error(f"加载队列持久化文件失败，忽略: {exc}")
            self._batches = {}
            self._active_batch_id = None

    def _flush_locked(self) -> None:
        cfg = get_config()
        data = {
            "active_batch_id": self._active_batch_id,
            "batches": [
                {
                    "batch_id": batch.batch_id,
                    "nonce": batch.nonce,
                    "created_at": batch.created_at,
                    "state": batch.state,
                    "tasks": [asdict(task) for task in batch.tasks],
                    "kind": batch.kind,
                    "recovery_from": batch.recovery_from,
                }
                for batch in self._batches.values()
            ],
        }
        tmp = f"{cfg.queue_file}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, cfg.queue_file)
        except Exception as exc:
            get_logger().error(f"写队列持久化文件失败: {exc}")
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _purge_expired_locked(self) -> int:
        ttl = get_config().expire_minutes * 60
        now = time.time()
        affected_batches = 0
        protected_states = {"executing", "indeterminate"}
        for batch_id, batch in list(self._batches.items()):
            if now - batch.created_at <= ttl:
                continue
            expired_tasks = [
                task for task in batch.tasks if task.state not in protected_states
            ]
            if not expired_tasks:
                continue
            expired_ids = {task.task_id for task in expired_tasks}
            batch.tasks = [
                task for task in batch.tasks if task.task_id not in expired_ids
            ]
            affected_batches += 1
            audit(
                "expire",
                batch_id=batch_id,
                task_count=len(expired_tasks),
                retained_indeterminate=sum(
                    task.state == "indeterminate" for task in batch.tasks
                ),
            )
            if not batch.tasks:
                self._batches.pop(batch_id, None)
                if self._active_batch_id == batch_id:
                    self._active_batch_id = None
        return affected_batches

    def purge_expired(self) -> int:
        with self._transaction():
            return self._last_purge_count

    def recover_interrupted_tasks(self) -> int:
        """在没有活跃提交时，把上次异常退出遗留的任务转为待对账状态。"""
        try:
            with commit_guard.execution_slot():
                with self._transaction():
                    recovered = 0
                    for batch in self._batches.values():
                        for task in batch.tasks:
                            if task.state != "executing":
                                continue
                            task.state = "indeterminate"
                            task.recovery_reason = "process_restarted_during_execution"
                            recovered += 1
                            audit(
                                "execution_recovery_required",
                                batch_id=batch.batch_id,
                                task_id=task.task_id,
                                execution_id=task.execution_id,
                                operation_type=task.operation_type,
                                issue_key=task.issue_key,
                            )
                    return recovered
        except commit_guard.ExecutionBusyError:
            return 0

    def _create_open_batch_locked(self) -> Batch:
        batch_id = "batch-" + uuid.uuid4().hex
        batch = Batch(
            batch_id=batch_id,
            nonce=secrets.token_hex(4).upper(),
            created_at=time.time(),
        )
        self._batches[batch_id] = batch
        self._active_batch_id = batch_id
        return batch

    def _get_or_create_recovery_batch_locked(
        self, failed_batch_id: str, now: float
    ) -> Batch:
        """返回失败批次的同层恢复批次；追加任务时延长整批 TTL。"""
        batch = next(
            (
                item
                for item in self._batches.values()
                if item.kind == "recovery"
                and item.recovery_from == failed_batch_id
                and item.state == "presented"
            ),
            None,
        )
        if batch is None:
            batch = Batch(
                batch_id="batch-" + uuid.uuid4().hex,
                nonce=secrets.token_hex(4).upper(),
                created_at=now,
                state="presented",
                kind="recovery",
                recovery_from=failed_batch_id,
            )
            self._batches[batch.batch_id] = batch
        else:
            batch.created_at = now
        return batch

    def enqueue(
        self,
        operation_type: str,
        issue_key: str | None,
        payload: dict[str, Any],
        description: str,
        source: str = "",
    ) -> PendingTask:
        """把任务追加到服务端维护的唯一开放批次。"""
        with self._transaction():
            risk_engine.assert_not_delete(operation_type, payload)
            batch = self._batches.get(self._active_batch_id or "")
            if (
                batch is None
                or batch.state != "open"
                or batch.kind != "normal"
            ):
                batch = self._create_open_batch_locked()

            risk_engine.check_two_phase(operation_type, issue_key, batch.tasks)
            risk_level, _reason = risk_engine.classify_risk(operation_type, payload)
            task = PendingTask(
                task_id="task-" + uuid.uuid4().hex,
                batch_id=batch.batch_id,
                operation_type=operation_type,
                risk_level=risk_level,
                issue_key=issue_key,
                payload=payload,
                description=description,
                created_at=time.time(),
                source=source,
            )
            batch.tasks.append(task)
            risk_engine.maybe_escalate_bulk_fields(batch.tasks)
            if len(batch.tasks) >= get_config().max_tasks_per_batch:
                batch.state = "sealed"
                self._active_batch_id = None
            audit(
                "enqueue",
                batch_id=batch.batch_id,
                task_id=task.task_id,
                operation_type=operation_type,
                risk_level=task.risk_level,
                issue_key=issue_key,
                description=description,
            )
            return task

    def present_pending_batches(self) -> list[Batch]:
        """原子封存开放批次，并把所有封存批次转为可确认状态。"""
        with self._transaction():
            active = self._batches.get(self._active_batch_id or "")
            if active is not None and active.tasks:
                active.state = "sealed"
                self._active_batch_id = None
            for batch in self._batches.values():
                if batch.state == "sealed":
                    batch.state = "presented"
            return sorted(
                [b for b in self._batches.values() if b.state == "presented"],
                key=lambda b: b.created_at,
            )

    def get_batch(self, batch_id: str) -> Batch | None:
        with self._transaction():
            return self._batches.get(batch_id)

    def list_batches(self) -> list[Batch]:
        with self._transaction():
            return sorted(self._batches.values(), key=lambda b: b.created_at)

    def list_presented_batches(self) -> list[Batch]:
        with self._transaction():
            return sorted(
                [
                    batch
                    for batch in self._batches.values()
                    if batch.state == "presented"
                    and any(task.state == "pending" for task in batch.tasks)
                ],
                key=lambda batch: batch.created_at,
            )

    def list_indeterminate_tasks(self) -> list[PendingTask]:
        with self._transaction():
            return sorted(
                [
                    task
                    for batch in self._batches.values()
                    for task in batch.tasks
                    if task.state == "indeterminate"
                ],
                key=lambda task: task.created_at,
            )

    def get_task(self, batch_id: str, task_id: str) -> PendingTask | None:
        with self._transaction():
            batch = self._batches.get(batch_id)
            if not batch:
                return None
            return next((task for task in batch.tasks if task.task_id == task_id), None)

    def total_pending(self) -> int:
        with self._transaction():
            active_states = {"pending", "executing", "indeterminate"}
            return sum(
                task.state in active_states
                for batch in self._batches.values()
                for task in batch.tasks
            )

    def has_pending(self) -> bool:
        return self.total_pending() > 0

    def reserve_tasks(
        self, batch_id: str, task_ids: list[str], nonce: str
    ) -> list[PendingTask]:
        """校验确认凭据并原子预留任务，阻止跨进程重复提交。"""
        with self._transaction():
            batch = self._batches.get(batch_id)
            if batch is None or batch.state != "presented":
                raise ValueError(f"批次 {batch_id} 不存在、未展示或已过期")
            if not secrets.compare_digest(nonce.strip().upper(), batch.nonce.upper()):
                raise PermissionError("验证码错误，拒绝提交")
            if len(task_ids) != len(set(task_ids)):
                raise ValueError("task_ids 不得重复")
            task_map = {task.task_id: task for task in batch.tasks}
            if any(task_id not in task_map for task_id in task_ids):
                raise ValueError("提交包含不存在的任务")
            selected = [task_map[task_id] for task_id in task_ids]
            if any(task.state != "pending" for task in selected):
                raise ValueError("任务已在执行或已经处理")
            for task in selected:
                task.state = "executing"
                task.execution_id = "exec-" + uuid.uuid4().hex
                task.execution_reserved_at = time.time()
                task.recovery_reason = None
            return selected

    def release_tasks(self, batch_id: str, task_ids: list[str]) -> None:
        with self._transaction():
            batch = self._batches.get(batch_id)
            if batch:
                selected = set(task_ids)
                for task in batch.tasks:
                    if task.task_id in selected and task.state == "executing":
                        task.state = "pending"
                        task.execution_id = None
                        task.execution_reserved_at = None
                        task.recovery_reason = None

    def resolve_indeterminate(
        self, batch_id: str, task_id: str, resolution: str
    ) -> ReconciliationResult:
        """记录人工对账结论；不会自动重试或执行任何 Jira 写操作。"""
        terminal_state = {
            "succeeded": "succeeded",
            "abandoned": "cancelled",
        }.get(resolution)
        if terminal_state is None and resolution != "not_applied":
            raise ValueError("不支持的人工处置结论")

        with self._transaction():
            batch = self._batches.get(batch_id)
            if batch is None:
                raise ValueError("批次不存在或已过期")
            task = next(
                (item for item in batch.tasks if item.task_id == task_id), None
            )
            if task is None:
                raise ValueError("任务不存在或已过期")
            if task.state != "indeterminate":
                raise ValueError("仅允许处置待人工对账的任务")

            original_task = deepcopy(task)
            now = time.time()
            recovery_task: PendingTask | None = None
            recovery_batch_id: str | None = None
            if resolution == "not_applied":
                recovery_batch = self._get_or_create_recovery_batch_locked(
                    batch_id, now
                )
                previous_execution_ids = list(task.previous_execution_ids)
                if task.execution_id:
                    previous_execution_ids.append(task.execution_id)
                recovery_task = PendingTask(
                    task_id="task-" + uuid.uuid4().hex,
                    batch_id=recovery_batch.batch_id,
                    operation_type=task.operation_type,
                    risk_level=task.risk_level,
                    issue_key=task.issue_key,
                    payload=deepcopy(task.payload),
                    description=task.description,
                    created_at=now,
                    source=task.source,
                    state="pending",
                    recovery_reason="human_confirmed_not_applied",
                    origin_task_id=task.origin_task_id or task.task_id,
                    origin_batch_id=task.origin_batch_id or batch_id,
                    previous_execution_ids=previous_execution_ids,
                    recovery_attempt=task.recovery_attempt + 1,
                    recovered_at=now,
                )
                recovery_batch.tasks.append(recovery_task)
                recovery_batch_id = recovery_batch.batch_id
                resolved_state = "pending"
            else:
                assert terminal_state is not None
                resolved_state = terminal_state

            batch.tasks = [item for item in batch.tasks if item.task_id != task_id]
            if not batch.tasks:
                self._batches.pop(batch_id, None)

            result = ReconciliationResult(
                original_task=original_task,
                resolution=resolution,
                resolved_state=resolved_state,
                recovery_task=recovery_task,
                recovery_batch_id=recovery_batch_id,
            )

        audit(
            "indeterminate_resolved",
            batch_id=batch_id,
            task_id=task_id,
            execution_id=result.original_task.execution_id,
            operation_type=result.original_task.operation_type,
            issue_key=result.original_task.issue_key,
            resolution=resolution,
            recovery_task_id=(
                result.recovery_task.task_id if result.recovery_task else None
            ),
            recovery_batch_id=result.recovery_batch_id,
        )
        return result

    def remove_tasks(self, batch_id: str, task_ids: list[str]) -> int:
        with self._transaction():
            batch = self._batches.get(batch_id)
            if not batch:
                return 0
            selected = set(task_ids)
            before = len(batch.tasks)
            batch.tasks = [task for task in batch.tasks if task.task_id not in selected]
            removed = before - len(batch.tasks)
            if not batch.tasks:
                self._batches.pop(batch_id, None)
            return removed

    def remove_batch(self, batch_id: str) -> int:
        """仅允许取消已经展示给用户的批次。"""
        with self._transaction():
            batch = self._batches.get(batch_id)
            if batch is None or batch.state != "presented":
                return 0
            if any(task.state != "pending" for task in batch.tasks):
                return 0
            self._batches.pop(batch_id)
            audit("cancel", batch_id=batch_id, task_count=len(batch.tasks))
            return len(batch.tasks)


_queue: QueueManager | None = None


def get_queue() -> QueueManager:
    global _queue
    if _queue is None:
        _queue = QueueManager()
    return _queue


def reset_queue() -> None:
    """测试用：重建队列单例。"""
    global _queue
    _queue = None
