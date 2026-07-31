"""执行引擎守卫测试：nonce 校验、探活失败、删除拦截、move 降级、成功执行。"""
from __future__ import annotations

import json
import multiprocessing
import threading
from unittest.mock import MagicMock

import pytest

from core import jira_client, jira_executor
from core.jira_executor import CommitError


def _append_commit_records(cfg, start, count, ready, begin) -> None:
    """子进程并发追加提交日志的测试辅助函数。"""
    import config as config_mod
    from core import jira_executor as executor

    config_mod.set_config(cfg)
    ready.set()
    assert begin.wait(10)
    for sequence in range(start, start + count):
        executor._append_commit_log({"sequence": sequence})


def _enqueue_comment(queue):
    task = queue.enqueue("add_comment", "PROJ-1", {"issue_key": "PROJ-1", "body": "hi"}, "加评论")
    queue.present_pending_batches()
    return task


def test_wrong_nonce_rejected(queue, monkeypatch):
    task = _enqueue_comment(queue)
    client = MagicMock()
    monkeypatch.setattr(jira_client, "ping", lambda: True)
    monkeypatch.setattr(jira_client, "get_client", lambda: client)

    with pytest.raises(CommitError):
        jira_executor.execute_batch(task.batch_id, [task.task_id], "WRONGCODE")

    client.add_comment.assert_not_called()
    assert queue.get_task(task.batch_id, task.task_id).state == "pending"


def test_ping_failure_aborts(queue, monkeypatch):
    task = _enqueue_comment(queue)
    batch = queue.get_batch(task.batch_id)
    ping = MagicMock(return_value=False)
    get_client = MagicMock()
    monkeypatch.setattr(jira_client, "ping", ping)
    monkeypatch.setattr(jira_client, "get_client", get_client)

    with pytest.raises(CommitError):
        jira_executor.execute_batch(task.batch_id, [task.task_id], batch.nonce)

    ping.assert_called_once_with()
    get_client.assert_not_called()
    assert queue.get_task(task.batch_id, task.task_id).state == "pending"


def test_successful_comment_execution(queue, monkeypatch):
    task = _enqueue_comment(queue)
    batch = queue.get_batch(task.batch_id)

    client = MagicMock()
    monkeypatch.setattr(jira_client, "ping", lambda: True)
    monkeypatch.setattr(jira_client, "get_client", lambda: client)

    result = jira_executor.execute_batch(task.batch_id, [task.task_id], batch.nonce)
    assert result["ok_count"] == 1
    client.add_comment.assert_called_once_with("PROJ-1", "hi")
    # 执行后从队列移除
    assert queue.total_pending() == 0


def test_move_downgrades_to_failure(queue, monkeypatch):
    task = queue.enqueue(
        "move_issue", "PROJ-1",
        {"issue_key": "PROJ-1", "target_project_key": "NEW"}, "移动",
    )
    queue.present_pending_batches()
    batch = queue.get_batch(task.batch_id)
    client = MagicMock()
    monkeypatch.setattr(jira_client, "ping", lambda: True)
    monkeypatch.setattr(jira_client, "get_client", lambda: client)

    result = jira_executor.execute_batch(task.batch_id, [task.task_id], batch.nonce)
    assert result["fail_count"] == 1
    assert "不支持" in result["results"][0]["error"]


def test_create_records_new_key(queue, monkeypatch):
    task = queue.enqueue(
        "create_issue", None,
        {"project_key": "PROJ", "issue_type": "Task", "summary": "s"}, "新建",
    )
    queue.present_pending_batches()
    batch = queue.get_batch(task.batch_id)
    client = MagicMock()
    created = MagicMock()
    created.key = "PROJ-999"
    client.create_issue.return_value = created
    monkeypatch.setattr(jira_client, "ping", lambda: True)
    monkeypatch.setattr(jira_client, "get_client", lambda: client)

    result = jira_executor.execute_batch(task.batch_id, [task.task_id], batch.nonce)
    assert result["results"][0]["new_issue_key"] == "PROJ-999"


def test_commit_log_written(queue, monkeypatch):
    task = _enqueue_comment(queue)
    batch = queue.get_batch(task.batch_id)
    client = MagicMock()
    monkeypatch.setattr(jira_client, "ping", lambda: True)
    monkeypatch.setattr(jira_client, "get_client", lambda: client)
    jira_executor.execute_batch(task.batch_id, [task.task_id], batch.nonce)
    logs = jira_executor.read_commit_log(5)
    assert len(logs) == 1
    assert logs[0]["ok_count"] == 1


def test_failed_execution_redacts_commit_log_error(queue, cfg, monkeypatch):
    task = _enqueue_comment(queue)
    batch = queue.get_batch(task.batch_id)
    cfg.jira.username = "sensitive-user"
    cfg.jira.password = "sensitive-password"
    detail = (
        "HTTP 403 diagnostic detail "
        "url=https://jira.example.com/rest/api/2/comment "
        "user=sensitive-user password=sensitive-password token=SECRET_TOKEN"
    )
    client = MagicMock()
    client.add_comment.side_effect = RuntimeError(detail)
    monkeypatch.setattr(jira_client, "ping", lambda: True)
    monkeypatch.setattr(jira_client, "get_client", lambda: client)

    result = jira_executor.execute_batch(
        task.batch_id, [task.task_id], batch.nonce
    )
    error = result["results"][0]["error"]
    stored_error = jira_executor.read_commit_log(1)[0]["results"][0]["error"]

    assert error == stored_error
    assert "RuntimeError" in stored_error
    assert "HTTP 403 diagnostic detail" in stored_error
    assert "rest/api/2/comment" in stored_error
    assert "***" in stored_error
    for sensitive in cfg.sensitive_values():
        assert sensitive not in stored_error


@pytest.mark.parametrize("limit", [0, -1, True, False])
def test_read_commit_log_rejects_invalid_limit_even_without_file(cfg, limit):
    with pytest.raises(ValueError, match="limit 必须是大于等于 1 的整数"):
        jira_executor.read_commit_log(limit)


def test_read_commit_log_limit_above_available_returns_all(cfg):
    logs = [{"sequence": i} for i in range(101)]
    with open(cfg.commit_log_file, "w", encoding="utf-8") as f:
        json.dump(logs, f)

    assert jira_executor.read_commit_log(1) == [{"sequence": 100}]
    result = jira_executor.read_commit_log(300)
    assert len(result) == 101
    assert result[0] == {"sequence": 100}
    assert result[-1] == {"sequence": 0}


def test_commit_log_threaded_appends_do_not_lose_records(cfg):
    thread_count = 8
    records_per_thread = 5
    barrier = threading.Barrier(thread_count)

    def append_records(start):
        barrier.wait()
        for sequence in range(start, start + records_per_thread):
            jira_executor._append_commit_log({"sequence": sequence})

    threads = [
        threading.Thread(target=append_records, args=(index * records_per_thread,))
        for index in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert all(not thread.is_alive() for thread in threads)
    logs = jira_executor.read_commit_log(thread_count * records_per_thread)
    assert {record["sequence"] for record in logs} == set(
        range(thread_count * records_per_thread)
    )


def test_commit_log_concurrent_reads_remain_valid(cfg):
    jira_executor._append_commit_log({"sequence": -1})
    writer_done = threading.Event()
    read_errors = []

    def append_records():
        try:
            for sequence in range(20):
                jira_executor._append_commit_log({"sequence": sequence})
        finally:
            writer_done.set()

    def read_records():
        while not writer_done.is_set():
            logs = jira_executor.read_commit_log(50)
            if not logs or not all("sequence" in record for record in logs):
                read_errors.append(logs)

    writer = threading.Thread(target=append_records)
    reader = threading.Thread(target=read_records)
    writer.start()
    reader.start()
    writer.join(10)
    reader.join(10)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert read_errors == []


def test_commit_log_multiprocess_appends_do_not_lose_records(cfg):
    process_count = 4
    records_per_process = 5
    cfg.commit_log_keep = process_count * records_per_process
    context = multiprocessing.get_context("spawn")
    begin = context.Event()
    ready_events = [context.Event() for _ in range(process_count)]
    processes = [
        context.Process(
            target=_append_commit_records,
            args=(
                cfg,
                index * records_per_process,
                records_per_process,
                ready_events[index],
                begin,
            ),
        )
        for index in range(process_count)
    ]
    for process in processes:
        process.start()
    try:
        assert all(ready.wait(10) for ready in ready_events)
        begin.set()
        for process in processes:
            process.join(15)
    finally:
        begin.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)

    assert all(process.exitcode == 0 for process in processes)
    logs = jira_executor.read_commit_log(process_count * records_per_process)
    assert {record["sequence"] for record in logs} == set(
        range(process_count * records_per_process)
    )
