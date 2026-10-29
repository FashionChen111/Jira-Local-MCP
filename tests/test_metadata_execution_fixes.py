"""ID routing, worklog SDK requests and clone outcome invariants."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from jira import JIRA
from jira.exceptions import JIRAError
from requests import Response

from core.executing import jira_executor, worker
from core.mcp_tools import jira_enqueue
from core.operations import StoreError
from core.operations.models import OperationRecord
from core.storage.database import reset_database_initialization


def operation(kind, payload):
    return OperationRecord(operation_id="op-" + "a" * 32, operation_type=kind,
                           issue_key="P-1", payload=payload, risk_level="high",
                           description="test", source="test", created_at=1)


@pytest.mark.parametrize("mode,extras,expected", [
    ("auto", {}, {"adjustEstimate": "auto"}),
    ("leave", {}, {"adjustEstimate": "leave"}),
    ("new", {"new_estimate": "3h"}, {"adjustEstimate": "new", "newEstimate": "3h"}),
    ("manual", {"reduce_by": "1h"}, {"adjustEstimate": "manual", "reduceBy": "1h"}),
])
def test_worklog_offline_sdk_generates_expected_request(queue, monkeypatch, mode, extras, expected):
    client = JIRA(server="https://offline.invalid", get_server_info=False, validate=False)
    response = Response()
    response.status_code = 201
    response._content = b'{"id":"10"}'
    post = Mock(return_value=response)
    monkeypatch.setattr(client._session, "post", post)
    monkeypatch.setattr(jira_executor.jira_client, "get_client", lambda: client)
    record = queue.queue.enqueue("add_worklog", "P-1", {"time_spent": "2h", "started": "2026-09-09T09:30:00+08:00",
                                  "adjust_estimate": mode, **extras}, "worklog")
    assert record.risk_level == "low"
    jira_executor.execute_operation(record)
    assert post.call_args.args == ("https://offline.invalid/rest/api/2/issue/P-1/worklog",)
    assert post.call_args.kwargs["params"] == expected
    assert json.loads(post.call_args.kwargs["data"]) == {"timeSpent": "2h", "started": "2026-09-09T09:30:00.000+0800"}
    client.close()


@pytest.mark.parametrize("extra", [
    {"started": "2026-01-01T12:00:00"}, {"started": "2026-02-30T12:00:00Z"},
    {"started": "nonsense"}, {"adjust_estimate": "new"}, {"adjust_estimate": "manual"},
    {"adjust_estimate": "leave", "new_estimate": "2h"},
    {"adjust_estimate": "auto", "reduce_by": "1h"},
    {"adjust_estimate": "new", "new_estimate": "2h", "reduce_by": "1h"},
    {"adjust_estimate": "invalid"},
])
def test_worklog_invalid_input_keeps_queue_unchanged(queue, extra):
    with pytest.raises(StoreError):
        queue.queue.enqueue("add_worklog", "P-1", {"time_spent": "1h", **extra}, "bad")
    assert queue.query.total_active() == 0


def test_removed_and_unsupported_operations_never_enter_queue(queue):
    assert "未入队" in jira_enqueue.jira_issue_move_append("P-1", "Q", "Bug")
    assert "不支持" in jira_enqueue.jira_issue_clone_append("P-1", clone_subtasks=True)
    for kind, payload in [("move_issue", {"target_project_key": "Q"}),
                          ("change_type", {"target_issue_type_id": "2", "fields_mapping": {"issuetype": {"id": "3"}}}),
                          ("clone_issue", {"field_overrides": {"project": {"key": "Q"}}})]:
        with pytest.raises(StoreError):
            queue.queue.enqueue(kind, "P-1", payload, "bad")
    assert queue.query.total_active() == 0


def test_transition_uses_id_even_when_names_duplicate_and_rechecks_availability(monkeypatch):
    client = Mock()
    client.transitions.return_value = [{"id": "1", "name": "Done"}, {"id": "2", "name": "Done"}]
    monkeypatch.setattr(jira_executor.jira_client, "get_client", lambda: client)
    task = operation("transition_issue", {"transition_id": "2", "fields": {"resolution": {"id": "3"}}})
    jira_executor.execute_operation(task)
    client.transition_issue.assert_called_once_with(client.issue.return_value, "2", fields={"resolution": {"id": "3"}})
    client.transitions.return_value = [{"id": "1", "name": "Done"}]
    with pytest.raises(ValueError, match="当前不可用"):
        jira_executor.execute_operation(task)
    assert client.transition_issue.call_count == 1


def clone_client(monkeypatch, *, failure=False, subtask=False):
    client = Mock()
    source = SimpleNamespace(fields=SimpleNamespace(project=SimpleNamespace(key="P"),
        issuetype=SimpleNamespace(id="2", subtask=subtask), summary="source", description="original",
        priority=SimpleNamespace(id="3"), parent=SimpleNamespace(key="P-10"),
        attachment=[SimpleNamespace(id=str(i), filename=f"{i}.txt", get=lambda: b"binary") for i in range(3)]))
    parent = SimpleNamespace(fields=SimpleNamespace(project=SimpleNamespace(key="P"), issuetype=SimpleNamespace(subtask=False)))
    client.issue.side_effect = lambda key, **kwargs: source if key == "P-1" else parent
    client.create_issue.return_value = SimpleNamespace(key="P-20")
    streams = []
    def upload(**kwargs):
        streams.append(kwargs["attachment"])
        kwargs["attachment"].seek(0)
        assert kwargs["attachment"].read() == b"binary"
        if failure and len(streams) == 2:
            raise JIRAError(status_code=400, text="invalid SECRET_TOKEN")
        return SimpleNamespace(id=str(100 + len(streams)), filename=kwargs["filename"])
    client.add_attachment.side_effect = upload
    monkeypatch.setattr(jira_executor.jira_client, "get_client", lambda: client)
    return client, streams


def test_clone_overrides_parent_and_description_snapshot(queue, monkeypatch, tmp_path):
    client, streams = clone_client(monkeypatch, subtask=True)
    path = tmp_path / "description.txt"
    path.write_text("快照", encoding="utf-8")
    task = queue.queue.enqueue("clone_issue", "P-1", {"clone_attachments": True, "field_overrides": {
        "description": {"file": str(path)}, "customfield_1": {"id": "7"}, "parent": {"key": "P-11"}}}, "clone")
    path.write_text("后来内容", encoding="utf-8")
    result = jira_executor.execute_operation(task)
    assert client.create_issue.call_args.kwargs["fields"] == {
        "project": {"key": "P"}, "issuetype": {"id": "2"}, "summary": "[克隆] source",
        "description": "快照", "priority": {"id": "3"}, "customfield_1": {"id": "7"}, "parent": {"key": "P-11"}}
    assert result.outcome == "success"
    assert [copy.status for copy in result.attachment_results] == ["success"] * 3
    assert all(stream.closed for stream in streams)
    client.create_issue.reset_mock()
    jira_executor.execute_operation(operation("clone_issue", {}))
    assert client.create_issue.call_args.kwargs["fields"]["parent"] == {"key": "P-10"}


def test_clone_warning_persisted_and_releases_issue_dependencies(queue, monkeypatch):
    client, streams = clone_client(monkeypatch, failure=True)
    producer = queue.queue.enqueue("clone_issue", "P-1", {"clone_attachments": True}, "clone")
    staged = queue.queue.enqueue("add_comment", producer.operation_id, {"body": "staged"}, "comment")
    queued = queue.queue.enqueue("add_comment", producer.operation_id, {"body": "queued"}, "comment")
    queue.submission.submit(3, [producer.operation_id, staged.operation_id], True)
    assert worker.process_once()
    records = queue.query.read_execution_log(10)
    clone = next(row for row in records if row["operation_id"] == producer.operation_id)
    assert clone["outcome"] == "warning" and clone["new_issue_key"] == "P-20"
    assert [copy["status"] for copy in clone["attachment_results"]] == ["success", "failed", "unattempted"]
    assert "SECRET_TOKEN" not in json.dumps(clone)
    assert worker.process_once()
    client.add_comment.assert_called_once_with("P-20", "staged")
    state = queue.query.get_state()
    queue.submission.submit(state.revisions["queue"], [queued.operation_id], True)
    assert client.create_issue.call_count == 1 and len(streams) == 2
    assert all(stream.closed for stream in streams)
    reset_database_initialization()
    assert clone in queue.query.read_execution_log(10)


def test_clone_binary_upload_through_offline_sdk(monkeypatch):
    mock_client, _ = clone_client(monkeypatch)
    client = JIRA(server="https://offline.invalid", get_server_info=False, validate=False)
    monkeypatch.setattr(client, "issue", mock_client.issue)
    monkeypatch.setattr(client, "create_issue", mock_client.create_issue)
    posts = []
    def post(url, **kwargs):
        body = kwargs["data"].to_string()
        assert b"binary" in body and b"filename=" in body
        assert url == "https://offline.invalid/rest/api/2/issue/P-20/attachments"
        posts.append(body)
        response = Response()
        response.status_code = 200
        response._content = json.dumps([{"id": str(len(posts)), "filename": f"{len(posts)}.txt", "size": 6}]).encode()
        return response
    monkeypatch.setattr(client._session, "post", post)
    monkeypatch.setattr(jira_executor.jira_client, "get_client", lambda: client)
    result = jira_executor.execute_operation(operation("clone_issue", {"clone_attachments": True}))
    assert result.outcome == "success" and len(posts) == 3
    client.close()


def test_warning_result_commit_failure_uses_existing_interrupted_recovery(queue, monkeypatch):
    clone_client(monkeypatch, failure=True)
    producer = queue.queue.enqueue("clone_issue", "P-1", {"clone_attachments": True}, "clone")
    later = queue.queue.enqueue("add_comment", "P-2", {"body": "later"}, "comment")
    queue.submission.submit(2, [producer.operation_id, later.operation_id], True)
    from core.operations.repository import execution
    original = execution.finish_execution
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("disk failure after update")
    monkeypatch.setattr(execution, "finish_execution", fail)
    assert worker.process_once()
    assert queue.query.get_state().execution.executing
    assert queue.query.read_execution_log(10) == []
    assert worker.recover_interrupted() == 1
    state = queue.query.get_state()
    assert state.execution.indeterminate and state.execution.held
