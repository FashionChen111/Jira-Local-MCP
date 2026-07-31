"""全局提交执行守卫，确保任意时刻只有一个批次调用 Jira 写接口。"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

import portalocker

from config import get_config


BUSY_MESSAGE = "当前有批次正在执行，请等待完成后再提交"


class ExecutionBusyError(RuntimeError):
    """另一个提交正在执行。"""


_PROCESS_LOCK = threading.Lock()


def _lock_path() -> str:
    return os.path.join(get_config().data_dir, "commit_execution.lock")


def _file_lock() -> portalocker.Lock:
    os.makedirs(get_config().data_dir, exist_ok=True)
    return portalocker.Lock(
        _lock_path(),
        mode="a",
        timeout=0,
        flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
    )


@contextmanager
def execution_slot() -> Iterator[None]:
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


def execution_busy() -> bool:
    """返回提交锁的瞬时状态，仅用于页面提示；提交时仍须实际抢锁。"""
    if _PROCESS_LOCK.locked():
        return True

    file_lock = _file_lock()
    try:
        file_lock.acquire()
    except portalocker.exceptions.LockException:
        return True
    else:
        file_lock.release()
        return False
