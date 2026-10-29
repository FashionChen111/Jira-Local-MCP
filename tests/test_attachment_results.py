"""Shared attachment outcome contracts and execution dependency behavior."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from jira.exceptions import JIRAError
from pydantic import ValidationError

from core.executing import jira_executor, worker
from core.operations import models
from core.operations.dependencies import orchestration
from core.operations.errors import DependencyError, StoreError
from core.operations.rules.safety import OperationSafetyError
from core.storage.database import reset_database_initialization


def test_execution_results_use_one_attachment_collection():
    fields = models.OperationExecutionResult.model_fields
    assert "attachment_results" in fields
    assert not {"attachment_result", "clone_result"} & fields.keys()
    identity = models.AttachmentIdentity(id="10", filename="final.txt")
    item = models.AttachmentResult(
        requested_filename="original.txt", status="success", attachment=identity,
    )
    result = models.OperationExecutionResult(summary="uploaded", attachment_results=(item,))
    assert result.model_dump(mode="json")["attachment_results"][0]["attachment"] == {
        "id": "10", "filename": "final.txt",
    }
    assert models.OperationExecutionResult.model_validate_json(result.model_dump_json()) == result
    assert models.OperationExecutionResult(summary="done").attachment_results == ()


@pytest.mark.parametrize("data", [
    {"status": "success"},
    {"status": "success", "attachment": {"id": "10", "filename": "a"}, "error": "bad"},
    {"status": "failed"},
    {"status": "unknown"},
    {"status": "failed", "attachment": {"id": "10", "filename": "a"}, "error": "bad"},
    {"status": "unattempted", "error": "bad"},
    {"status": "unattempted", "source_attachment_id": " "},
    {"status": "unattempted", "requested_filename": "\x00"},
])
def test_attachment_outcomes_reject_inconsistent_fields(data):
    with pytest.raises(ValidationError):
        models.AttachmentResult.model_validate({"requested_filename": "a.txt", **data})


@pytest.mark.parametrize("status", ["success", "failed", "unknown", "unattempted"])
def test_attachment_outcomes_roundtrip_without_inventing_identities(status):
    data = {"requested_filename": "original.txt", "status": status}
    if status == "success":
        data["attachment"] = {"id": "10", "filename": "final.txt"}
    elif status in {"failed", "unknown"}:
        data["error"] = "upload error"
    value = models.AttachmentResult.model_validate_json(json.dumps(data))
    assert models.AttachmentResult.model_validate_json(value.model_dump_json()) == value
    assert (value.attachment is not None) == (status == "success")


@pytest.mark.parametrize("new_key,items", [(None, "failed"), ("P-20", "success"), ("P-20", "empty")])
def test_warning_requires_confirmed_main_issue_and_attachment_anomaly(new_key, items):
    results = ()
    if items == "failed":
        results = (models.AttachmentResult(requested_filename="a", source_attachment_id="1",
                                          status="failed", error="error"),)
    elif items == "success":
        results = (models.AttachmentResult(requested_filename="a", source_attachment_id="1", status="success",
                   attachment=models.AttachmentIdentity(id="10", filename="a")),)
    with pytest.raises(DependencyError):
        orchestration.resolution_from_producer_result(
            producer_operation_id="op-" + "a" * 32, operation_type="clone_issue",
            outcome="warning", new_issue_key=new_key, attachment_results=results,
        )


@pytest.mark.parametrize("kind,source_id", [("clone_issue", None), ("add_attachment", "10")])
def test_attachment_source_identity_matches_operation_role(kind, source_id):
    item = models.AttachmentResult(
        requested_filename="a.txt", source_attachment_id=source_id,
        status="success", attachment=models.AttachmentIdentity(id="20", filename="a.txt"),
    )
    with pytest.raises(DependencyError, match="源附件"):
        orchestration.resolution_from_producer_result(
            producer_operation_id="op-" + "a" * 32, operation_type=kind,
            outcome="success", new_issue_key="P-20" if kind == "clone_issue" else None,
            attachment_results=(item,),
        )


@pytest.mark.parametrize("count", [0, 1, 3])
def test_successful_clone_attachment_lists_never_enable_attachment_placeholders(queue, monkeypatch, count):
    client = Mock()
    client.issue.return_value = SimpleNamespace(fields=SimpleNamespace(
        project=SimpleNamespace(key="P"), issuetype=SimpleNamespace(id="2", subtask=False),
        summary="source", description=None, priority=None,
        attachment=[SimpleNamespace(id=str(i), filename=f"{i}.txt", get=lambda: b"data") for i in range(count)],
    ))
    client.create_issue.return_value = SimpleNamespace(key="P-20")
    client.add_attachment.side_effect = [SimpleNamespace(id=str(i + 10), filename=f"{i}-final.txt") for i in range(count)]
    monkeypatch.setattr(jira_executor.jira_client, "get_client", lambda: client)
    clone = queue.queue.enqueue("clone_issue", "P-1", {"clone_attachments": True}, "clone")
    queue.submission.submit(1, [clone.operation_id], True)
    assert worker.process_once()
    record = queue.query.read_execution_log(1)[0]
    assert record["outcome"] == "success"
    assert len(record["attachment_results"]) == count
    assert all(item["status"] == "success" for item in record["attachment_results"])
    with pytest.raises(StoreError, match="不是 add_attachment"):
        queue.queue.enqueue("add_comment", "P-20", {
            "body": f"[^{{{{attachment:{clone.operation_id}}}}}]",
        }, "ambiguous reference")


@pytest.mark.parametrize("failure,status", [
    (JIRAError(status_code=400, text="denied SECRET_TOKEN"), "failed"),
    (JIRAError(status_code=503, text="unavailable SECRET_TOKEN"), "unknown"),
    (TimeoutError("timeout SECRET_TOKEN"), "unknown"),
])
def test_failed_upload_keeps_detail_and_blocks_dependants(queue, tmp_path, monkeypatch, failure, status):
    path = tmp_path / "a.txt"
    path.write_bytes(b"data")
    client = Mock()
    client.add_attachment.side_effect = failure
    monkeypatch.setattr(jira_executor.jira_client, "get_client", lambda: client)
    upload = queue.queue.enqueue("add_attachment", "P-1", {"file_path": str(path)}, "upload")
    comment = queue.queue.enqueue("add_comment", "P-1", {
        "body": f"[^{{{{attachment:{upload.operation_id}}}}}]",
    }, "comment")
    queue.submission.submit(2, [upload.operation_id, comment.operation_id], False)
    assert worker.process_once()
    records = {row["operation_id"]: row for row in queue.query.read_execution_log(10)}
    result = records[upload.operation_id]
    assert result["outcome"] == "failed"
    assert result["attachment_results"][0]["status"] == status
    assert result["attachment_results"][0]["attachment"] is None
    assert records[comment.operation_id]["outcome"] == "dependency_failed"
    assert "SECRET_TOKEN" not in json.dumps(records)
    assert client.add_attachment.call_args.kwargs["attachment"].closed


def test_upload_confirmation_failure_is_unknown_even_for_lookup_404(queue, tmp_path, monkeypatch):
    path = tmp_path / "a.txt"
    path.write_bytes(b"data")
    client = Mock()
    client.add_attachment.return_value = SimpleNamespace(id="10", filename=None)
    client.attachment.side_effect = JIRAError(status_code=404, text="not visible")
    monkeypatch.setattr(jira_executor.jira_client, "get_client", lambda: client)
    upload = queue.queue.enqueue("add_attachment", "P-1", {"file_path": str(path)}, "upload")
    result = jira_executor.execute_operation(upload)
    assert result.outcome == "failed"
    assert result.attachment_results[0].status == "unknown"
    assert result.error


def test_disappeared_upload_file_has_failure_detail(queue, tmp_path, monkeypatch):
    path = tmp_path / "gone.txt"
    path.write_bytes(b"data")
    upload = queue.queue.enqueue("add_attachment", "P-1", {"file_path": str(path)}, "upload")
    path.unlink()
    client = Mock()
    monkeypatch.setattr(jira_executor.jira_client, "get_client", lambda: client)
    result = jira_executor.execute_operation(upload)
    assert result.outcome == "failed"
    assert result.attachment_results[0].status == "failed"
    assert result.attachment_results[0].requested_filename == "gone.txt"
    client.add_attachment.assert_not_called()


@pytest.mark.parametrize("filename", ["", " ", "bad\x00.txt", "x" * 1025], ids=["empty", "blank", "nul", "too_long"])
def test_invalid_requested_filename_never_enters_queue(queue, tmp_path, filename):
    path = tmp_path / "a.txt"
    path.write_bytes(b"data")
    with pytest.raises(StoreError, match="文件名"):
        queue.queue.enqueue("add_attachment", "P-1", {
            "file_path": str(path), "file_name": filename,
        }, "upload")
    assert queue.query.total_active() == 0


@pytest.mark.parametrize("filename", ["", " ", "bad\x00.txt", "x" * 1025], ids=["empty", "blank", "nul", "too_long"])
def test_invalid_persisted_requested_filename_never_calls_jira(queue, tmp_path, monkeypatch, filename):
    path = tmp_path / "a.txt"
    path.write_bytes(b"data")
    task = queue.queue.enqueue("add_attachment", "P-1", {"file_path": str(path)}, "upload")
    damaged = task.model_copy(update={"payload": {"file_path": str(path), "file_name": filename}})
    client = Mock()
    client.add_attachment.return_value = SimpleNamespace(id="10", filename="normalized.txt")
    get_client = Mock(return_value=client)
    monkeypatch.setattr(jira_executor.jira_client, "get_client", get_client)
    with pytest.raises(OperationSafetyError, match="文件名"):
        jira_executor.execute_operation(damaged)
    get_client.assert_not_called()
    client.add_attachment.assert_not_called()


def test_clone_warning_releases_issue_dependencies_and_survives_reload(queue, monkeypatch):
    client = Mock()
    client.issue.return_value = SimpleNamespace(fields=SimpleNamespace(
        project=SimpleNamespace(key="P"), issuetype=SimpleNamespace(id="2", subtask=False),
        summary="source", description=None, priority=None,
        attachment=[SimpleNamespace(id=str(i), filename=f"{i}.txt", get=lambda: b"data") for i in range(3)],
    ))
    client.create_issue.return_value = SimpleNamespace(key="P-20")
    client.add_attachment.side_effect = [SimpleNamespace(id="10", filename="final.txt"), TimeoutError("SECRET_TOKEN")]
    monkeypatch.setattr(jira_executor.jira_client, "get_client", lambda: client)
    clone = queue.queue.enqueue("clone_issue", "P-1", {"clone_attachments": True}, "clone")
    staged = queue.queue.enqueue("add_comment", clone.operation_id, {"body": "staged"}, "comment")
    queued = queue.queue.enqueue("add_comment", clone.operation_id, {"body": "queued"}, "comment")
    queue.submission.submit(3, [clone.operation_id, staged.operation_id], True)
    assert worker.process_once()
    record = queue.query.read_execution_log(1)[0]
    assert record["outcome"] == "warning" and record["new_issue_key"] == "P-20"
    assert [item["status"] for item in record["attachment_results"]] == ["success", "unknown", "unattempted"]
    assert record["attachment_results"][0]["attachment"] == {"id": "10", "filename": "final.txt"}
    assert "SECRET_TOKEN" not in json.dumps(record)
    reset_database_initialization()
    assert queue.query.read_execution_log(1)[0] == record
    assert worker.process_once()
    assert client.add_comment.call_args.args[0] == "P-20"
    state = queue.query.get_state()
    queue.submission.submit(state.revisions["queue"], [queued.operation_id], False)
    assert worker.process_once()
    late = queue.queue.enqueue("add_comment", clone.operation_id, {"body": "late"}, "comment")
    assert late.issue_key == "P-20"
    assert client.create_issue.call_count == 1
    with pytest.raises(StoreError, match="不是 add_attachment"):
        queue.queue.enqueue("add_comment", "P-20", {
            "body": f"[^{{{{attachment:{clone.operation_id}}}}}]",
        }, "bad reference")
