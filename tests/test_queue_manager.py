"""队列测试：服务端批次生成、封批、容量与持久化。"""
from __future__ import annotations

import json
import multiprocessing
import time

import pytest


def _enqueue_in_process(cfg, start) -> None:
    import config as config_mod
    from core import queue_manager

    config_mod.set_config(cfg)
    queue_manager.reset_queue()
    start.wait(10)
    queue_manager.get_queue().enqueue(
        "add_comment", "PROJ-1", {"issue_key": "PROJ-1", "body": "parallel"}, "并发入队"
    )


def _enqueue(queue, body: str = "hi"):
    return queue.enqueue(
        "add_comment", "PROJ-1", {"issue_key": "PROJ-1", "body": body}, "加评论"
    )


def test_enqueue_uses_server_generated_active_batch(queue, cfg):
    first = _enqueue(queue, "a")
    second = _enqueue(queue, "b")
    assert first.batch_id.startswith("batch-")
    assert len(first.batch_id) == len("batch-") + 32
    assert second.batch_id == first.batch_id
    assert first.task_id.startswith("task-")


def test_show_seals_current_batch_and_next_append_uses_new_batch(queue, cfg):
    first = _enqueue(queue)
    presented = queue.present_pending_batches()
    assert [batch.batch_id for batch in presented] == [first.batch_id]
    assert queue.get_batch(first.batch_id).state == "presented"

    second = _enqueue(queue, "next")
    assert second.batch_id != first.batch_id
    assert queue.list_presented_batches()[0].batch_id == first.batch_id


def test_capacity_seals_batch_and_moves_next_task(queue, cfg):
    cfg.max_tasks_per_batch = 2
    first = _enqueue(queue, "1")
    second = _enqueue(queue, "2")
    third = _enqueue(queue, "3")

    assert first.batch_id == second.batch_id
    assert third.batch_id != first.batch_id
    assert queue.get_batch(first.batch_id).state == "sealed"
    assert queue.get_batch(third.batch_id).state == "open"

    presented = queue.present_pending_batches()
    assert [batch.batch_id for batch in presented] == [first.batch_id, third.batch_id]


def test_persisted_active_batch_is_reused_after_restart(queue, cfg):
    first = _enqueue(queue)
    from core import queue_manager

    queue_manager.reset_queue()
    restored = queue_manager.get_queue()
    second = _enqueue(restored, "after restart")
    assert second.batch_id == first.batch_id

    raw = json.load(open(cfg.queue_file, encoding="utf-8"))
    assert raw["active_batch_id"] == first.batch_id


def test_two_processes_enqueue_without_losing_tasks(queue, cfg):
    from core import queue_manager

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(target=_enqueue_in_process, args=(cfg, start))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0

    queue_manager.reset_queue()
    restored = queue_manager.get_queue()
    assert restored.total_pending() == 2


def test_nonce_not_in_public_dict(queue):
    task = _enqueue(queue)
    assert "nonce" not in task.to_public_dict()
    assert "state" not in task.to_public_dict()


def test_reserve_tasks_rejects_duplicate_ids_without_changing_task_state(queue):
    task = _enqueue(queue)
    queue.present_pending_batches()
    batch = queue.get_batch(task.batch_id)

    with pytest.raises(ValueError, match="task_ids 不得重复"):
        queue.reserve_tasks(
            task.batch_id,
            [task.task_id, task.task_id],
            batch.nonce,
        )

    unchanged = queue.get_task(task.batch_id, task.task_id)
    assert unchanged is not None
    assert unchanged.state == "pending"


def test_reserve_tasks_rejects_unknown_id_without_changing_any_task_state(queue):
    first = _enqueue(queue)
    second = _enqueue(queue)
    queue.present_pending_batches()
    batch = queue.get_batch(first.batch_id)

    with pytest.raises(ValueError, match="提交包含不存在的任务"):
        queue.reserve_tasks(
            first.batch_id,
            [first.task_id, "task-" + "f" * 32, second.task_id],
            batch.nonce,
        )

    assert queue.get_task(first.batch_id, first.task_id).state == "pending"
    assert queue.get_task(second.batch_id, second.task_id).state == "pending"


def test_expire_purge(queue, cfg):
    cfg.expire_minutes = 0
    _enqueue(queue)
    time.sleep(0.01)
    assert queue.total_pending() == 0


def test_expire_purge_preserves_executing_batch(queue, cfg):
    task = _enqueue(queue)
    queue.present_pending_batches()
    batch = queue.get_batch(task.batch_id)
    queue.reserve_tasks(task.batch_id, [task.task_id], batch.nonce)

    cfg.expire_minutes = 0
    time.sleep(0.01)

    assert queue.purge_expired() == 0
    assert queue.get_task(task.batch_id, task.task_id).state == "executing"


def test_reserve_records_execution_metadata(queue):
    task = _enqueue(queue)
    queue.present_pending_batches()
    batch = queue.get_batch(task.batch_id)

    reserved = queue.reserve_tasks(task.batch_id, [task.task_id], batch.nonce)[0]

    assert reserved.state == "executing"
    assert reserved.execution_id.startswith("exec-")
    assert reserved.execution_reserved_at is not None
    assert reserved.recovery_reason is None


def test_restart_recovers_executing_task_as_indeterminate(queue):
    task = _enqueue(queue)
    queue.present_pending_batches()
    batch = queue.get_batch(task.batch_id)
    queue.reserve_tasks(task.batch_id, [task.task_id], batch.nonce)

    from core import queue_manager

    queue_manager.reset_queue()
    restored = queue_manager.get_queue()
    recovered = restored.get_task(task.batch_id, task.task_id)

    assert recovered is not None
    assert recovered.state == "indeterminate"
    assert recovered.execution_id is not None
    assert recovered.execution_reserved_at is not None
    assert recovered.recovery_reason == "process_restarted_during_execution"
    assert restored.list_presented_batches() == []
    assert [item.task_id for item in restored.list_indeterminate_tasks()] == [
        task.task_id
    ]


def test_recovery_does_not_touch_task_while_execution_lock_is_held(queue):
    from core import commit_guard

    task = _enqueue(queue)
    queue.present_pending_batches()
    batch = queue.get_batch(task.batch_id)
    queue.reserve_tasks(task.batch_id, [task.task_id], batch.nonce)

    with commit_guard.execution_slot():
        assert queue.recover_interrupted_tasks() == 0

    assert queue.get_task(task.batch_id, task.task_id).state == "executing"


def test_expiry_removes_pending_sibling_but_preserves_indeterminate(queue, cfg):
    first = _enqueue(queue, "uncertain")
    second = _enqueue(queue, "pending")
    queue.present_pending_batches()
    batch = queue.get_batch(first.batch_id)
    queue.reserve_tasks(first.batch_id, [first.task_id], batch.nonce)

    from core import queue_manager

    queue_manager.reset_queue()
    restored = queue_manager.get_queue()
    cfg.expire_minutes = 0
    time.sleep(0.01)

    assert restored.purge_expired() == 1
    assert restored.get_task(first.batch_id, first.task_id).state == "indeterminate"
    assert restored.get_task(second.batch_id, second.task_id) is None


def _make_indeterminate(queue, body="hi"):
    task = _enqueue(queue, body)
    queue.present_pending_batches()
    batch = queue.get_batch(task.batch_id)
    queue.reserve_tasks(task.batch_id, [task.task_id], batch.nonce)

    from core import queue_manager

    queue_manager.reset_queue()
    return queue_manager.get_queue(), task


@pytest.mark.parametrize(
    ("resolution", "terminal_state"),
    [("succeeded", "succeeded"), ("abandoned", "cancelled")],
)
def test_terminal_reconciliation_archives_without_requeue(
    queue, resolution, terminal_state
):
    restored, task = _make_indeterminate(queue)
    result = restored.resolve_indeterminate(
        task.batch_id, task.task_id, resolution
    )

    assert result.resolved_state == terminal_state
    assert result.original_task.task_id == task.task_id
    assert result.recovery_task is None
    assert restored.get_task(task.batch_id, task.task_id) is None
    assert restored.total_pending() == 0


def test_not_applied_creates_new_recovery_task_and_batch(queue, monkeypatch):
    restored, task = _make_indeterminate(queue)
    failed = restored.get_task(task.batch_id, task.task_id)
    original_execution_id = failed.execution_id
    audit_records = []
    monkeypatch.setattr(
        "core.queue_manager.secrets.token_hex", lambda _size: "deadbeef"
    )
    monkeypatch.setattr(
        "core.queue_manager.audit",
        lambda event, **fields: audit_records.append((event, fields)),
    )

    result = restored.resolve_indeterminate(
        task.batch_id, task.task_id, "not_applied"
    )
    recovered = result.recovery_task
    recovery_batch = restored.get_batch(result.recovery_batch_id)

    assert result.resolved_state == "pending"
    assert recovered.task_id != task.task_id
    assert recovered.created_at > task.created_at
    assert recovered.batch_id == recovery_batch.batch_id
    assert recovered.state == "pending"
    assert recovered.origin_task_id == task.task_id
    assert recovered.origin_batch_id == task.batch_id
    assert recovered.previous_execution_ids == [original_execution_id]
    assert recovered.recovery_attempt == 1
    assert recovered.recovered_at == recovered.created_at
    assert recovered.recovery_reason == "human_confirmed_not_applied"
    assert recovered.execution_id is None
    assert recovered.execution_reserved_at is None
    assert recovery_batch.kind == "recovery"
    assert recovery_batch.recovery_from == task.batch_id
    assert recovery_batch.state == "presented"
    assert recovery_batch.nonce == "DEADBEEF"
    assert restored.get_batch(task.batch_id) is None
    assert audit_records == [
        (
            "indeterminate_resolved",
            {
                "batch_id": task.batch_id,
                "task_id": task.task_id,
                "execution_id": original_execution_id,
                "operation_type": task.operation_type,
                "issue_key": task.issue_key,
                "resolution": "not_applied",
                "recovery_task_id": recovered.task_id,
                "recovery_batch_id": recovery_batch.batch_id,
            },
        )
    ]


def test_reconciliation_audit_runs_only_after_queue_flush(queue, monkeypatch):
    restored, task = _make_indeterminate(queue)
    recorded = []
    monkeypatch.setattr(
        "core.queue_manager.audit",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    monkeypatch.setattr(
        restored, "_flush_locked", lambda: (_ for _ in ()).throw(OSError("flush failed"))
    )

    with pytest.raises(OSError, match="flush failed"):
        restored.resolve_indeterminate(
            task.batch_id, task.task_id, "not_applied"
        )

    assert recorded == []


def test_same_failed_batch_reuses_recovery_batch_nonce_and_extends_ttl(queue):
    first = _enqueue(queue, "first")
    second = _enqueue(queue, "second")
    queue.present_pending_batches()
    batch = queue.get_batch(first.batch_id)
    queue.reserve_tasks(first.batch_id, [first.task_id, second.task_id], batch.nonce)

    from core import queue_manager

    queue_manager.reset_queue()
    restored = queue_manager.get_queue()
    first_result = restored.resolve_indeterminate(
        first.batch_id, first.task_id, "not_applied"
    )
    recovery_batch = restored.get_batch(first_result.recovery_batch_id)
    first_created_at = recovery_batch.created_at
    nonce = recovery_batch.nonce
    time.sleep(0.01)

    second_result = restored.resolve_indeterminate(
        second.batch_id, second.task_id, "not_applied"
    )
    reused = restored.get_batch(second_result.recovery_batch_id)

    assert second_result.recovery_batch_id == first_result.recovery_batch_id
    assert reused.nonce == nonce
    assert reused.created_at > first_created_at
    assert {item.task_id for item in reused.tasks} == {
        first_result.recovery_task.task_id,
        second_result.recovery_task.task_id,
    }


def test_recovery_leaves_pending_sibling_in_original_batch(queue):
    failed = _enqueue(queue, "failed")
    sibling = _enqueue(queue, "pending")
    sibling_created_at = sibling.created_at
    queue.present_pending_batches()
    original_batch = queue.get_batch(failed.batch_id)
    original_created_at = original_batch.created_at
    queue.reserve_tasks(failed.batch_id, [failed.task_id], original_batch.nonce)

    from core import queue_manager

    queue_manager.reset_queue()
    restored = queue_manager.get_queue()
    result = restored.resolve_indeterminate(
        failed.batch_id, failed.task_id, "not_applied"
    )
    retained_batch = restored.get_batch(failed.batch_id)
    retained_sibling = restored.get_task(sibling.batch_id, sibling.task_id)

    assert result.recovery_batch_id != failed.batch_id
    assert retained_batch.created_at == original_created_at
    assert retained_batch.state == "presented"
    assert [item.task_id for item in retained_batch.tasks] == [sibling.task_id]
    assert retained_sibling.created_at == sibling_created_at
    assert retained_sibling.state == "pending"


def test_recovery_batch_is_persisted_and_never_reused_by_normal_enqueue(queue):
    restored, task = _make_indeterminate(queue)
    result = restored.resolve_indeterminate(
        task.batch_id, task.task_id, "not_applied"
    )

    from core import queue_manager

    queue_manager.reset_queue()
    reloaded = queue_manager.get_queue()
    recovery_batch = reloaded.get_batch(result.recovery_batch_id)
    normal = _enqueue(reloaded, "normal")

    assert recovery_batch.kind == "recovery"
    assert recovery_batch.recovery_from == task.batch_id
    assert recovery_batch.tasks[0].origin_task_id == task.task_id
    assert normal.batch_id != recovery_batch.batch_id
    assert reloaded.get_batch(normal.batch_id).kind == "normal"


def test_second_recovery_forms_chain_and_preserves_origin(queue):
    restored, original = _make_indeterminate(queue)
    first = restored.resolve_indeterminate(
        original.batch_id, original.task_id, "not_applied"
    )
    first_batch = restored.get_batch(first.recovery_batch_id)
    first_reserved = restored.reserve_tasks(
        first_batch.batch_id, [first.recovery_task.task_id], first_batch.nonce
    )[0]

    from core import queue_manager

    queue_manager.reset_queue()
    restarted = queue_manager.get_queue()
    second = restarted.resolve_indeterminate(
        first_batch.batch_id, first_reserved.task_id, "not_applied"
    )

    assert second.recovery_batch_id != first_batch.batch_id
    second_batch = restarted.get_batch(second.recovery_batch_id)
    assert second_batch.recovery_from == first_batch.batch_id
    assert second.recovery_task.origin_task_id == original.task_id
    assert second.recovery_task.origin_batch_id == original.batch_id
    assert second.recovery_task.recovery_attempt == 2
    assert second.recovery_task.previous_execution_ids == [
        first.original_task.execution_id,
        first_reserved.execution_id,
    ]
