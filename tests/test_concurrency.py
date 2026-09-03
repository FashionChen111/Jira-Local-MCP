"""Cross-thread/process exclusion and SQLite concurrent writer tests."""
from __future__ import annotations

import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import config as config_mod
from core.executing import lock as execution_lock
from core.executing import worker as execution_worker
from core.operations.models import OperationExecutionResult


def _hold_slot(cfg, entered, release):
    config_mod.set_config(cfg)
    with execution_lock.execution_slot():
        entered.set()
        assert release.wait(10)


def _enqueue_process(cfg, suffix):
    config_mod.set_config(cfg)
    from core.operations import get_services, reset_services
    from core.storage.database import reset_database_initialization
    reset_database_initialization()
    reset_services()
    get_services().queue.enqueue(
        "add_comment", f"PROJ-{suffix}",
        {"body": str(suffix)}, str(suffix), "process",
    )


def _worker_process(cfg, start, calls):
    config_mod.set_config(cfg)
    from core.executing import worker
    from core.storage.database import reset_database_initialization
    from core.operations import reset_services

    reset_database_initialization()
    reset_services()

    def handler(_operation):
        with calls.get_lock():
            calls.value += 1
        time.sleep(0.1)
        return OperationExecutionResult(summary="ok")

    worker.jira_executor.execute_operation = handler
    assert start.wait(10)
    worker.process_once()


def test_execution_slot_blocks_another_process(cfg):
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    process = context.Process(target=_hold_slot, args=(cfg, entered, release))
    process.start()
    try:
        assert entered.wait(10)
        with __import__("pytest").raises(execution_lock.ExecutionBusyError):
            with execution_lock.execution_slot():
                pass
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


def test_concurrent_process_enqueues_are_not_lost(cfg, queue):
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_enqueue_process, args=(cfg, index + 1)) for index in range(4)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    state = queue.query.get_state()
    assert sum(len(group.operations) for group in state.queue.groups) == 4
    assert state.revisions["queue"] == 4


def test_threaded_workers_never_overlap_jira_writes(queue, monkeypatch):
    first = queue.queue.enqueue("add_comment", "PROJ-1", {"body": "1"}, "1", "test")
    second = queue.queue.enqueue("add_comment", "PROJ-2", {"body": "2"}, "2", "test")
    queue.submission.submit(2, [first.operation_id, second.operation_id], False)
    active = 0
    maximum = 0
    lock = threading.Lock()
    def handler(_task):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return OperationExecutionResult(summary="ok")
    monkeypatch.setattr(execution_worker.jira_executor, "execute_operation", handler)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _value: execution_worker.process_once(), range(2)))
    while execution_worker.process_once():
        pass
    assert maximum == 1
    assert len(queue.query.read_execution_log(10)) == 2


def test_spawned_workers_do_not_claim_the_same_operation(cfg, queue):
    operation = queue.queue.enqueue(
        "add_comment", "PROJ-1", {"body": "one"}, "one", "test"
    )
    queue.submission.submit(1, [operation.operation_id], False)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    calls = context.Value("i", 0)
    processes = [context.Process(target=_worker_process, args=(cfg, start, calls)) for _ in range(2)]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    assert calls.value == 1
    assert len(queue.query.read_execution_log(10)) == 1


def test_submit_can_continue_while_an_operation_is_executing(queue, monkeypatch):
    first = queue.queue.enqueue(
        "add_comment", "PROJ-1", {"body": "one"}, "one", "test"
    )
    queue.submission.submit(1, [first.operation_id], False)
    entered = threading.Event()
    release = threading.Event()

    def handler(_operation):
        entered.set()
        assert release.wait(10)
        return OperationExecutionResult(summary="ok")

    monkeypatch.setattr(execution_worker.jira_executor, "execute_operation", handler)
    thread = threading.Thread(target=execution_worker.process_once)
    thread.start()
    assert entered.wait(10)
    second = queue.queue.enqueue(
        "add_comment", "PROJ-2", {"body": "two"}, "two", "test"
    )
    queue.submission.submit(3, [second.operation_id], False)
    release.set()
    thread.join(10)
    assert not thread.is_alive()
    monkeypatch.setattr(
        execution_worker.jira_executor,
        "execute_operation",
        lambda _operation: OperationExecutionResult(summary="ok"),
    )
    assert execution_worker.process_once() is True
    assert len(queue.query.read_execution_log(10)) == 2


def test_staged_operation_continues_in_a_fresh_process(cfg, queue):
    operation = queue.queue.enqueue(
        "add_comment", "PROJ-1", {"body": "restart"}, "restart", "test"
    )
    queue.submission.submit(1, [operation.operation_id], False)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    calls = context.Value("i", 0)
    process = context.Process(target=_worker_process, args=(cfg, start, calls))
    process.start()
    start.set()
    process.join(15)
    assert process.exitcode == 0
    assert calls.value == 1
    assert queue.query.read_execution_log(1)[0]["operation_id"] == operation.operation_id
