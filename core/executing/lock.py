"""Global lock ensuring only one operation can call Jira write APIs at a time."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Generator

import portalocker

from config import get_config


BUSY_MESSAGE = "当前有 Jira 写操作正在执行"


class ExecutionBusyError(RuntimeError):
    """另一个提交正在执行。"""


_PROCESS_LOCK = threading.Lock()


def _lock_path() -> str:
    return get_config().execution_lock_file


def _file_lock() -> portalocker.Lock:
    return portalocker.Lock(
        _lock_path(),
        mode="a",
        timeout=0,
        flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
    )


@contextmanager
def execution_slot() -> Generator[None, None, None]:
    """非阻塞获取进程内及跨进程提交锁，并在退出时可靠释放。"""
    if not _PROCESS_LOCK.acquire(blocking=False):
        raise ExecutionBusyError(BUSY_MESSAGE)

    file_lock = _file_lock()
    try:
        try:
            file_lock.acquire()
        except portalocker.exceptions.LockException as exc:
            raise ExecutionBusyError(BUSY_MESSAGE) from exc
        try:
            yield
        finally:
            file_lock.release()
    finally:
        _PROCESS_LOCK.release()
