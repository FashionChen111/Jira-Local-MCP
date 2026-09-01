"""Attachment-result dependencies across queueing, execution, and Jira text."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from core.executing import jira_executor
from core.operations import AttachmentResult, StoreError
from core.operations.models import OperationRecord
from core.storage.database import open_database


def _attachment(queue, issue_key: str = "PROJ-1"):
    return queue.queue.enqueue(
        "add_attachment",
        issue_key,
        {"file_path": "C:\\fixtures\\a.jpg", "file_name": "a.jpg"},
        "attachment",
        "test",
    )


def _finish_attachment(queue, attachment) -> AttachmentResult:
    queue.submission.submit(
        queue.query.get_state().revisions["queue"],
        [attachment.operation_id],
        False,
    )
    claim = queue.execution.claim_next()
    assert claim is not None
    result = AttachmentResult(id="10001", filename="a-1.jpg")
    queue.execution.complete_execution(
        claim.execution_id,
        "success",
        "uploaded",
        None,
        None,
        1,
        result,
    )
    return result


def test_attachment_reference_participates_in_closure_and_topological_order(
    queue,
) -> None:
    attachment = _attachment(queue)
    comment = queue.queue.enqueue(
        "add_comment",
        "PROJ-1",
        {"body": f"new [^{{{{attachment:{attachment.operation_id}}}}}]"},
        "comment",
        "test",
    )

    state = queue.query.get_state().model_dump(mode="json")
    comment_public = state["queue"]["groups"][0]["operations"][1]
    assert comment_public["dependency_operation_ids"] == [
        attachment.operation_id
    ]
    with pytest.raises(StoreError, match="缺少依赖"):
        queue.submission.submit(2, [comment.operation_id], False)
    accepted, _ = queue.submission.submit(
        2, [comment.operation_id, attachment.operation_id], False
    )
    assert accepted == [attachment.operation_id, comment.operation_id]


def test_attachment_reference_requires_same_issue_and_attachment_producer(
    queue,
) -> None:
    attachment = _attachment(queue, "PROJ-1")
    marker = f"[^{{{{attachment:{attachment.operation_id}}}}}]"
    with pytest.raises(StoreError, match="同一 Jira 事务"):
        queue.queue.enqueue(
            "add_comment", "PROJ-2", {"body": marker}, "cross", "test"
        )

    create = queue.queue.enqueue(
        "create_issue",
        None,
        {"project_key": "PROJ", "issue_type": "Task", "summary": "new"},
        "create",
        "test",
    )
    with pytest.raises(StoreError, match="不是 add_attachment"):
        queue.queue.enqueue(
            "add_comment",
            create.operation_id,
            {"body": f"[^{{{{attachment:{create.operation_id}}}}}]"},
            "wrong producer",
            "test",
        )


@pytest.mark.parametrize(
    "description",
    [
        "see [^a.jpg]",
        "see !a.jpg!",
        "see [^{{attachment:op-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}}]",
    ],
)
def test_create_initial_description_rejects_local_attachment_syntax(
    queue, description: str
) -> None:
    with pytest.raises(StoreError, match="初始 description 不得引用附件"):
        queue.queue.enqueue(
            "create_issue",
            None,
            {
                "project_key": "PROJ",
                "issue_type": "Task",
                "summary": "new",
                "description": description,
            },
            "create",
            "test",
        )


def test_create_initial_description_allows_external_image_url(queue) -> None:
    operation = queue.queue.enqueue(
        "create_issue",
        None,
        {
            "project_key": "PROJ",
            "issue_type": "Task",
            "summary": "new",
            "description": "!https://example.com/image.jpg!",
        },
        "create",
        "test",
    )
    assert operation.payload["description"] == "!https://example.com/image.jpg!"


@pytest.mark.parametrize(
    "operation_type,container",
    [
        ("update_field", "fields"),
        ("change_type", "fields_mapping"),
        ("transition_issue", "fields"),
    ],
)
def test_description_files_are_snapshotted_during_enqueue(
    queue, tmp_path, operation_type: str, container: str
) -> None:
    path = tmp_path / f"{operation_type}.txt"
    path.write_text("入队快照", encoding="utf-8")
    payload: dict[str, object] = {
        container: {"description": {"file": str(path)}}
    }
    if operation_type == "change_type":
        payload["target_issue_type"] = "Bug"
    if operation_type == "transition_issue":
        payload["transition_name"] = "开始处理"

    operation = queue.queue.enqueue(
        operation_type, "PROJ-1", payload, "snapshot", "test"
    )
    path.write_text("执行前已改变", encoding="utf-8")

    stored = operation.payload[container]
    assert isinstance(stored, dict)
    assert stored["description"] == "入队快照"


def test_create_description_file_is_snapshotted_and_path_removed(
    queue, tmp_path
) -> None:
    path = tmp_path / "description.txt"
    path.write_text("创建快照", encoding="utf-8")
    operation = queue.queue.enqueue(
        "create_issue",
        None,
        {
            "project_key": "PROJ",
            "issue_type": "Task",
            "summary": "new",
            "description": "inline",
            "description_file": str(path),
        },
        "create",
        "test",
    )
    assert operation.payload["description"] == "创建快照"
    assert "description_file" not in operation.payload


def test_completed_upload_materializes_every_supported_content_path(queue) -> None:
    attachment = _attachment(queue)
    _finish_attachment(queue, attachment)
    link = f"[^{{{{attachment:{attachment.operation_id}}}}}]"
    image = f"!{{{{attachment:{attachment.operation_id}}}}}!"
    operations = [
        queue.queue.enqueue(
            "update_field",
            "PROJ-1",
            {"fields": {"description": link}},
            "update",
            "test",
        ),
        queue.queue.enqueue(
            "change_type",
            "PROJ-1",
            {
                "target_issue_type": "Bug",
                "fields_mapping": {"description": image},
            },
            "change",
            "test",
        ),
        queue.queue.enqueue(
            "transition_issue",
            "PROJ-1",
            {
                "transition_name": "开始处理",
                "fields": {"description": link},
                "comment": image,
            },
            "transition",
            "test",
        ),
        queue.queue.enqueue(
            "add_comment",
            "PROJ-1",
            {"body": link},
            "comment",
            "test",
        ),
        queue.queue.enqueue(
            "add_worklog",
            "PROJ-1",
            {"time_spent": "1h", "comment": image, "started": None},
            "worklog",
            "test",
        ),
    ]

    assert operations[0].payload["fields"] == {
        "description": "[^a-1.jpg]"
    }
    assert operations[1].payload["fields_mapping"] == {
        "description": "!a-1.jpg!"
    }
    assert operations[2].payload["fields"] == {
        "description": "[^a-1.jpg]"
    }
    assert operations[2].payload["comment"] == "!a-1.jpg!"
    assert operations[3].payload["body"] == "[^a-1.jpg]"
    assert operations[4].payload["comment"] == "!a-1.jpg!"
    assert all(
        not item.dependency_operation_ids
        for group in queue.query.get_state().queue.groups
        for item in group.operations
    )


def test_upload_result_is_persisted_and_broadcast_to_staging(queue) -> None:
    attachment = _attachment(queue)
    comment = queue.queue.enqueue(
        "add_comment",
        "PROJ-1",
        {
            "body": (
                f"new !{{{{attachment:{attachment.operation_id}}}}}!"
            )
        },
        "comment",
        "test",
    )
    queue.submission.submit(
        2, [comment.operation_id, attachment.operation_id], False
    )
    claim = queue.execution.claim_next()
    assert claim is not None
    result = AttachmentResult(id="20002", filename="a-1.jpg")
    queue.execution.complete_execution(
        claim.execution_id, "success", "uploaded", None, None, 2, result
    )

    next_claim = queue.execution.claim_next()
    assert next_claim is not None
    assert next_claim.operation.payload["body"] == "new !a-1.jpg!"
    history = queue.query.read_execution_log(1)[0]
    assert history["attachment_result"] == {
        "id": "20002",
        "filename": "a-1.jpg",
    }
    connection = open_database("operations")
    try:
        event = connection.execute(
            """SELECT data_json FROM queue_events
               WHERE operation_id=? AND action_code='attachment_resolved'""",
            (comment.operation_id,),
        ).fetchone()
    finally:
        connection.close()
    assert event is not None
    assert json.loads(event["data_json"])["attachment_result"]["id"] == "20002"


def test_indeterminate_upload_recovery_requires_and_broadcasts_complete_result(
    queue,
) -> None:
    attachment = _attachment(queue)
    comment = queue.queue.enqueue(
        "add_comment",
        "PROJ-1",
        {
            "body": f"[^{{{{attachment:{attachment.operation_id}}}}}]",
        },
        "comment",
        "test",
    )
    queue.submission.submit(
        2, [attachment.operation_id, comment.operation_id], False
    )
    claim = queue.execution.claim_next()
    assert claim is not None
    assert queue.execution.recover_interrupted() == 1

    with pytest.raises(StoreError, match="附件 ID 和最终文件名"):
        queue.recovery.resolve_indeterminate(
            attachment.operation_id, "succeeded", False
        )
    result = AttachmentResult(id="50005", filename="a-1.jpg")
    response = queue.recovery.resolve_indeterminate(
        attachment.operation_id,
        "succeeded",
        False,
        attachment_result=result,
    )

    assert response["attachment_result"] == result.model_dump(mode="json")
    held = queue.query.get_state().execution.held[0]
    assert held.payload["body"] == "[^a-1.jpg]"
    history = queue.query.read_execution_log(1)[0]
    assert history["attachment_result"] == result.model_dump(mode="json")


def test_attachment_broadcast_failure_rolls_back_result_and_revisions(queue) -> None:
    attachment = _attachment(queue)
    comment = queue.queue.enqueue(
        "add_comment",
        "PROJ-1",
        {
            "body": f"[^{{{{attachment:{attachment.operation_id}}}}}]",
        },
        "comment",
        "test",
    )
    queue.submission.submit(
        2, [attachment.operation_id, comment.operation_id], False
    )
    claim = queue.execution.claim_next()
    assert claim is not None
    before = queue.query.get_state().revisions
    connection = open_database("operations")
    try:
        connection.execute(
            """CREATE TRIGGER fail_attachment_resolution
               BEFORE UPDATE OF payload_json ON execution_staging
               BEGIN SELECT RAISE(ABORT, 'forced attachment failure'); END"""
        )
    finally:
        connection.close()

    with pytest.raises(sqlite3.IntegrityError, match="forced attachment failure"):
        queue.execution.complete_execution(
            claim.execution_id,
            "success",
            "uploaded",
            None,
            None,
            1,
            AttachmentResult(id="60006", filename="a-1.jpg"),
        )

    after = queue.query.get_state()
    assert after.revisions == before
    assert after.execution.executing[0].operation_id == attachment.operation_id
    assert after.execution.staged[0].payload["body"] != "[^a-1.jpg]"
    assert queue.query.read_execution_log(1) == []


def test_executor_returns_final_jira_filename_without_list_query(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "a.jpg"
    path.write_bytes(b"new")
    attachment_queries: list[str] = []

    class Client:
        def add_attachment(self, **_kwargs):
            return SimpleNamespace(id="30003", filename="a-1.jpg")

        def attachment(self, attachment_id: str):
            attachment_queries.append(attachment_id)
            raise AssertionError("完整上传响应不应触发回查")

    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="add_attachment",
        issue_key="PROJ-1",
        risk_level="medium",
        payload={"file_path": str(path), "file_name": "a.jpg"},
        description="upload",
        source="test",
        created_at=1,
    )
    monkeypatch.setattr(jira_executor.jira_client, "get_client", Client)

    result = jira_executor.execute_operation(operation)

    assert result.attachment_result == AttachmentResult(
        id="30003", filename="a-1.jpg"
    )
    assert attachment_queries == []


def test_executor_falls_back_to_attachment_id_when_filename_is_missing(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "a.jpg"
    path.write_bytes(b"new")

    class Client:
        def add_attachment(self, **_kwargs):
            return SimpleNamespace(id="40004", filename=None)

        def attachment(self, attachment_id: str):
            assert attachment_id == "40004"
            return SimpleNamespace(id=attachment_id, filename="a-1.jpg")

    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="add_attachment",
        issue_key="PROJ-1",
        risk_level="medium",
        payload={"file_path": str(path), "file_name": "a.jpg"},
        description="upload",
        source="test",
        created_at=1,
    )
    monkeypatch.setattr(jira_executor.jira_client, "get_client", Client)

    result = jira_executor.execute_operation(operation)

    assert result.attachment_result is not None
    assert result.attachment_result.filename == "a-1.jpg"


def test_executor_fails_when_id_lookup_still_has_no_filename(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "a.jpg"
    path.write_bytes(b"new")

    class Client:
        def add_attachment(self, **_kwargs):
            return SimpleNamespace(id="70007", filename=None)

        def attachment(self, _attachment_id: str):
            return SimpleNamespace(id="70007", filename=None)

    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="add_attachment",
        issue_key="PROJ-1",
        risk_level="medium",
        payload={"file_path": str(path), "file_name": "a.jpg"},
        description="upload",
        source="test",
        created_at=1,
    )
    monkeypatch.setattr(jira_executor.jira_client, "get_client", Client)

    with pytest.raises(RuntimeError, match="最终文件名"):
        jira_executor.execute_operation(operation)
