"""Background serial consumer for Web-authorized staging operations."""
from __future__ import annotations

import threading
import time
import traceback

from core.execution import jira_executor, lock as execution_lock
from core.runtime_logger import get_logger, redact
from core.operations import get_store

_POLL_SECONDS = 1.0
_lifecycle_lock = threading.Lock()
_wake_event = threading.Event()
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None

def recover_interrupted() -> int:
    """Conservatively reconcile abandoned executing rows while holding the Jira lock."""
    try:
        with execution_lock.execution_slot():
            return get_store().recover_interrupted()
    except execution_lock.ExecutionBusyError:
        return 0


def process_once() -> bool:
    """Claim and execute at most one operation; return whether work was claimed."""
    try:
        with execution_lock.execution_slot():
            claim = get_store().claim_next()
            if claim is None:
                return False
            execution_id = claim.execution_id
            started = time.monotonic_ns()
            try:
                summary, new_issue_key = jira_executor.execute_operation(claim.operation)
                outcome = "success"
                error = None
            except Exception as exc:
                outcome = "failed"
                summary = None
                new_issue_key = None
                error = redact(f"{type(exc).__name__}: {exc}")
                get_logger().error(
                    f"operation {claim.operation.operation_id} 执行失败: {error}\n"
                    f"{redact(traceback.format_exc())}"
                )
            elapsed_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
            try:
                get_store().complete_execution(
                    execution_id, outcome, summary, error, new_issue_key, int(elapsed_ms)
                )
            except Exception as exc:
                # The executing row is deliberately retained. Startup recovery makes it
                # indeterminate because Jira may already have observed the request.
                get_logger().error(
                    f"持久化 execution {execution_id} 结果失败，保留 executing: {type(exc).__name__}"
                )
            return True
    except execution_lock.ExecutionBusyError:
        return False


def _run() -> None:
    recover_interrupted()
    while not _stop_event.is_set():
        worked = process_once()
        if worked:
            continue
        # Another process may have held the execution lock during our startup recovery.
        # Retrying here prevents an abandoned executing row from blocking forever.
        recover_interrupted()
        _wake_event.wait(_POLL_SECONDS)
        _wake_event.clear()


def start_worker() -> None:
    global _worker_thread
    with _lifecycle_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        get_store().initialize()
        _stop_event.clear()
        _wake_event.clear()
        _worker_thread = threading.Thread(target=_run, daemon=True, name="jira-execution-worker")
        _worker_thread.start()


def wake_worker() -> None:
    _wake_event.set()


def stop_worker(timeout: float = 5.0) -> None:
    global _worker_thread
    with _lifecycle_lock:
        thread = _worker_thread
        if thread is None:
            return
        _stop_event.set()
        _wake_event.set()
    thread.join(timeout=timeout)
    with _lifecycle_lock:
        if _worker_thread is thread and not thread.is_alive():
            _worker_thread = None
