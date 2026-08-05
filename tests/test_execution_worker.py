"""Serial worker, result persistence, and conservative failure behavior."""
from __future__ import annotations

from core.execution import worker as execution_worker
from core.storage.database import open_database


def _stage(queue):
    operation = queue.enqueue("add_comment", "PROJ-1", {"issue_key": "PROJ-1", "body": "x"}, "comment", "test")
    queue.submit(1, [operation.operation_id], False)
    return operation


def test_worker_executes_success_and_persists_history(queue, monkeypatch):
    operation = _stage(queue)
    monkeypatch.setattr(execution_worker.jira_executor, "execute_operation", lambda task: ("done", None))
    assert execution_worker.process_once() is True
    record = queue.read_execution_log(1)[0]
    assert record["operation_id"] == operation.operation_id
    assert record["outcome"] == "success"


def test_worker_captures_jira_exception_as_failed(queue, monkeypatch):
    _stage(queue)
    def fail(_task):
        raise RuntimeError("boom SECRET_TOKEN")
    monkeypatch.setattr(execution_worker.jira_executor, "execute_operation", fail)
    execution_worker.process_once()
    record = queue.read_execution_log(1)[0]
    assert record["outcome"] == "failed"
    assert "SECRET_TOKEN" not in record["error"] and "***" in record["error"]


def test_result_transaction_failure_retains_executing(queue, monkeypatch):
    _stage(queue)
    monkeypatch.setattr(execution_worker.jira_executor, "execute_operation", lambda task: ("done", None))
    monkeypatch.setattr(queue, "complete_execution", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("disk")))
    execution_worker.process_once()
    assert queue.get_state()["execution"]["executing"]
    assert not queue.read_execution_log(1)


def test_delete_defence_is_rechecked_by_executor(queue, monkeypatch):
    _stage(queue)
    connection = open_database("operations")
    connection.execute(
        "UPDATE execution_staging SET payload_json=?",
        ('{"issue_key":"PROJ-1","remove":true}',),
    )
    connection.close()
    called = False
    def should_not_execute(_task):
        nonlocal called
        called = True
        return "bad", None
    monkeypatch.setitem(execution_worker.jira_executor._HANDLERS, "add_comment", should_not_execute)
    execution_worker.process_once()
    assert called is False
    assert queue.read_execution_log(1)[0]["outcome"] == "failed"


def test_no_work_returns_false(queue):
    assert execution_worker.process_once() is False
