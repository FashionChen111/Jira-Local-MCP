"""Serial worker, result persistence, and conservative failure behavior."""
from __future__ import annotations

import pytest

from core.executing import jira_executor
from core.executing import worker as execution_worker
from core.operations.models import OperationRecord
from core.operations.rules import safety as operation_safety
from core.storage.database import open_database


def _stage(queue):
    operation = queue.queue.enqueue(
        "add_comment", "PROJ-1", {"body": "x"}, "comment", "test"
    )
    queue.submission.submit(1, [operation.operation_id], False)
    return operation


def test_worker_executes_success_and_persists_history(queue, monkeypatch):
    operation = _stage(queue)
    monkeypatch.setattr(execution_worker.jira_executor, "execute_operation", lambda task: ("done", None))
    assert execution_worker.process_once() is True
    record = queue.query.read_execution_log(1)[0]
    assert record["operation_id"] == operation.operation_id
    assert record["outcome"] == "success"


def test_worker_captures_jira_exception_as_failed(queue, monkeypatch):
    _stage(queue)
    def fail(_task):
        raise RuntimeError("boom SECRET_TOKEN")
    monkeypatch.setattr(execution_worker.jira_executor, "execute_operation", fail)
    execution_worker.process_once()
    record = queue.query.read_execution_log(1)[0]
    assert record["outcome"] == "failed"
    assert "SECRET_TOKEN" not in record["error"] and "***" in record["error"]


def test_result_transaction_failure_retains_executing(queue, monkeypatch):
    _stage(queue)
    monkeypatch.setattr(execution_worker.jira_executor, "execute_operation", lambda task: ("done", None))
    monkeypatch.setattr(queue.execution, "complete_execution", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("disk")))
    execution_worker.process_once()
    assert queue.query.get_state().execution.executing
    assert not queue.query.read_execution_log(1)


def test_delete_defence_is_rechecked_by_executor(queue, monkeypatch):
    _stage(queue)
    connection = open_database("operations")
    connection.execute(
        "UPDATE execution_staging SET payload_json=?",
        ('{"body":"x","remove":true}',),
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
    assert queue.query.read_execution_log(1)[0]["outcome"] == "failed"


def test_failed_producer_never_calls_executor_for_dependants(queue, monkeypatch):
    create = queue.queue.enqueue(
        "create_issue",
        None,
        {"project_key": "PROJ", "issue_type": "Task", "summary": "new"},
        "create",
        "test",
    )
    child = queue.queue.enqueue(
        "add_comment",
        create.operation_id,
        {"body": "x"},
        "child",
        "test",
    )
    queue.submission.submit(2, [create.operation_id, child.operation_id], False)
    calls = []

    def fail_create(task):
        calls.append(task.operation_id)
        raise RuntimeError("create failed")

    monkeypatch.setattr(
        execution_worker.jira_executor, "execute_operation", fail_create
    )
    assert execution_worker.process_once() is True
    assert execution_worker.process_once() is False
    assert calls == [create.operation_id]
    records = {item["operation_id"]: item for item in queue.query.read_execution_log(10)}
    assert records[child.operation_id]["outcome"] == "dependency_failed"
    assert records[child.operation_id]["issue_key"] == create.operation_id
    assert records[child.operation_id]["payload"] == {"body": "x"}


def test_unresolved_operation_id_never_reaches_jira_handler(queue, monkeypatch):
    _stage(queue)
    connection = open_database("operations")
    connection.execute(
        "UPDATE execution_staging SET issue_key=?",
        ("op-" + "a" * 32,),
    )
    connection.close()
    called = False

    def should_not_execute(_task):
        nonlocal called
        called = True
        return "bad", None

    monkeypatch.setitem(
        execution_worker.jira_executor._HANDLERS,
        "add_comment",
        should_not_execute,
    )
    assert execution_worker.process_once() is False
    assert called is False
    assert queue.query.read_execution_log(1)[0]["outcome"] == "dependency_failed"


def test_executor_runs_safety_gate_before_handler_selection_or_jira_client(
    monkeypatch,
):
    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="add_comment",
        issue_key="op-" + "b" * 32,
        risk_level="low",
        payload={"body": "x"},
        description="unsafe direct execution",
        source="test",
        created_at=1,
    )
    calls = []
    monkeypatch.setattr(
        jira_executor.jira_client,
        "get_client",
        lambda: calls.append("client"),
    )
    monkeypatch.setitem(
        jira_executor._HANDLERS,
        "add_comment",
        lambda _client, _task: calls.append("handler"),
    )

    with pytest.raises(operation_safety.OperationSafetyError):
        jira_executor.execute_operation(operation)
    assert calls == []


def test_executor_rechecks_payload_shape_before_jira_client(monkeypatch):
    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="add_comment",
        issue_key="PROJ-1",
        risk_level="low",
        payload={"body": 1},
        description="malformed persisted payload",
        source="test",
        created_at=1,
    )
    calls = []
    monkeypatch.setattr(
        jira_executor.jira_client,
        "get_client",
        lambda: calls.append("client"),
    )

    with pytest.raises(operation_safety.OperationSafetyError, match="payload.body"):
        jira_executor.execute_operation(operation)
    assert calls == []


def test_executor_reads_both_link_targets_from_top_level(monkeypatch):
    calls = []

    class Client:
        def create_issue_link(self, **kwargs):
            calls.append(kwargs)

    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="add_issuelink",
        issue_key="PROJ-1",
        target_issue_key="PROJ-2",
        risk_level="low",
        payload={"link_type": "Relates"},
        description="link",
        source="test",
        created_at=1,
    )
    monkeypatch.setattr(jira_executor.jira_client, "get_client", Client)

    jira_executor.execute_operation(operation)

    assert calls == [
        {
            "type": "Relates",
            "inwardIssue": "PROJ-1",
            "outwardIssue": "PROJ-2",
        }
    ]


def test_no_work_returns_false(queue):
    assert execution_worker.process_once() is False
