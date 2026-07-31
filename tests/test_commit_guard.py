"""全局提交锁的进程内、异常释放和跨进程行为测试。"""
from __future__ import annotations

import multiprocessing

import pytest

import config as config_mod
from core import commit_guard


def _hold_execution_slot(cfg, entered, release) -> None:
    config_mod.set_config(cfg)
    with commit_guard.execution_slot():
        entered.set()
        assert release.wait(10)


def test_execution_slot_releases_after_exception(cfg):
    with pytest.raises(RuntimeError, match="boom"):
        with commit_guard.execution_slot():
            raise RuntimeError("boom")

    assert commit_guard.execution_busy() is False
    with commit_guard.execution_slot():
        assert commit_guard.execution_busy() is True


def test_execution_slot_blocks_another_process(cfg):
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_execution_slot,
        args=(cfg, entered, release),
    )
    process.start()
    try:
        assert entered.wait(10)
        assert commit_guard.execution_busy() is True
        with pytest.raises(commit_guard.ExecutionBusyError, match=commit_guard.BUSY_MESSAGE):
            with commit_guard.execution_slot():
                pass
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert process.exitcode == 0
    assert commit_guard.execution_busy() is False
