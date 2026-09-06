"""Attachment-result dependencies across queueing, execution, and Jira text."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from types import SimpleNamespace

import pytest

from core.executing import jira_executor
from core.operations import (
    AttachmentResult,
    DependencyScopeIncomplete,
    StoreError,
)
from core.operations.dependencies import orchestration as dependency_orchestration
from core.operations.errors import DependencyError
from core.operations.models import (
    AttachmentDependencyResolution,
    DependencyState,
    IssueDependencyResolution,
    OperationRecord,
)
from core.operations.rules.safety import (
    OperationSafetyError,
    assert_safe_to_execute,
    submission_problem,
)
from core.operations.services.queue import _snapshot_description_payload
from core.storage.database import open_database
from core.textfile_loader import TextFileError, snapshot_text_source


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
    assert comment_public["dependency_states"] == {
        attachment.operation_id: "queued"
    }
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
    "body",
    [
        "{{attachment:}}",
        "[^{{attachment:op-short}}]",
        "[{{attachment:op-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}}]",
        "{{attachment:not-an-operation-id}}",
    ],
)
def test_attachment_reference_syntax_rejects_malformed_placeholders(
    queue, body: str
) -> None:
    with pytest.raises(StoreError, match="附件引用必须使用"):
        queue.queue.enqueue(
            "add_comment", "PROJ-1", {"body": body}, "invalid", "test"
        )


def test_reference_collection_is_non_throwing_and_syntax_is_explicit() -> None:
    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="add_comment",
        issue_key="PROJ-1",
        risk_level="low",
        payload={"body": "{{attachment:bad}}"},
        description="invalid",
        source="test",
        created_at=1,
    )

    inspection = dependency_orchestration.inspect_dependencies(operation)

    assert inspection.issue_references == ()
    assert inspection.attachment_inspection.references == ()
    assert inspection.attachment_inspection.invalid_placeholder_paths == (
        "body",
    )
    assert (
        dependency_orchestration.collect_referenced_operation_ids(inspection)
        == ()
    )
    with pytest.raises(DependencyError, match="附件引用必须使用"):
        dependency_orchestration.validate_reference_syntax(
            operation,
            inspection,
            allow_unresolved=True,
        )


def test_execution_gate_rejects_unmaterialized_attachment_reference() -> None:
    producer_id = "op-" + "b" * 32
    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="add_comment",
        issue_key="PROJ-1",
        risk_level="low",
        payload={"body": f"[^{{{{attachment:{producer_id}}}}}]"},
        description="unmaterialized",
        source="test",
        created_at=1,
    )

    with pytest.raises(OperationSafetyError, match="执行前必须物化"):
        assert_safe_to_execute(operation)


def test_materialized_jira_attachment_text_passes_execution_syntax() -> None:
    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="add_comment",
        issue_key="PROJ-1",
        risk_level="low",
        payload={"body": "see [^a-1.jpg] and !a-1.jpg!"},
        description="materialized",
        source="test",
        created_at=1,
    )

    inspection = dependency_orchestration.inspect_dependencies(operation)
    dependency_orchestration.validate_reference_syntax(
        operation,
        inspection,
        allow_unresolved=False,
    )


def test_submission_problem_runs_one_syntax_orchestration(monkeypatch) -> None:
    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="add_comment",
        issue_key="PROJ-1",
        risk_level="low",
        payload={"body": "plain"},
        description="consumer",
        source="test",
        created_at=1,
    )
    inspection = dependency_orchestration.inspect_dependencies(operation)
    calls: list[bool] = []
    original = dependency_orchestration.validate_reference_syntax

    def counted(
        record: OperationRecord,
        inspected: dependency_orchestration.DependencyInspection,
        *,
        allow_unresolved: bool,
    ) -> None:
        calls.append(allow_unresolved)
        original(
            record,
            inspected,
            allow_unresolved=allow_unresolved,
        )

    monkeypatch.setattr(
        dependency_orchestration,
        "validate_reference_syntax",
        counted,
    )

    assert submission_problem(operation, inspection, {}) is None
    assert calls == [True]


def test_enqueue_reuses_one_dependency_reference_scan(
    queue, monkeypatch
) -> None:
    scans = 0
    original = dependency_orchestration.inspect_dependencies

    def counted(operation: OperationRecord):
        nonlocal scans
        scans += 1
        return original(operation)

    monkeypatch.setattr(
        dependency_orchestration, "inspect_dependencies", counted
    )

    queue.queue.enqueue(
        "add_comment", "PROJ-1", {"body": "plain"}, "plain", "test"
    )

    assert scans == 1


def test_enqueue_without_available_resolutions_skips_materialization(
    queue, monkeypatch
) -> None:
    def unexpected_materialization(*_args, **_kwargs):
        raise AssertionError("无可用 resolution 时不应进入物化")

    monkeypatch.setattr(
        dependency_orchestration,
        "materialize_references",
        unexpected_materialization,
    )

    queue.queue.enqueue(
        "add_comment", "PROJ-1", {"body": "plain"}, "plain", "test"
    )


def test_query_inspects_each_active_operation_once(queue, monkeypatch) -> None:
    attachment = _attachment(queue)
    comment = queue.queue.enqueue(
        "add_comment",
        "PROJ-1",
        {"body": f"[^{{{{attachment:{attachment.operation_id}}}}}]"},
        "comment",
        "test",
    )
    scanned_operation_ids: list[str] = []
    original = dependency_orchestration.inspect_dependencies

    def counted(operation: OperationRecord):
        scanned_operation_ids.append(operation.operation_id)
        return original(operation)

    monkeypatch.setattr(
        dependency_orchestration,
        "inspect_dependencies",
        counted,
    )

    queue.query.get_state()

    assert Counter(scanned_operation_ids) == {
        attachment.operation_id: 1,
        comment.operation_id: 1,
    }


def test_submission_selection_reuses_one_inspection_per_operation(
    queue, monkeypatch
) -> None:
    attachment = _attachment(queue)
    comment = queue.queue.enqueue(
        "add_comment",
        "PROJ-1",
        {"body": f"[^{{{{attachment:{attachment.operation_id}}}}}]"},
        "comment",
        "test",
    )
    scanned_operation_ids: list[str] = []
    original = dependency_orchestration.inspect_dependencies

    def counted(operation: OperationRecord):
        scanned_operation_ids.append(operation.operation_id)
        return original(operation)

    monkeypatch.setattr(
        dependency_orchestration,
        "inspect_dependencies",
        counted,
    )

    queue.submission.validate_submission(
        2,
        [comment.operation_id, attachment.operation_id],
        False,
    )

    assert Counter(scanned_operation_ids) == {
        attachment.operation_id: 1,
        comment.operation_id: 1,
    }


def test_cancel_builds_dependency_edges_with_one_inspection_each(
    queue, monkeypatch
) -> None:
    producer = queue.queue.enqueue(
        "create_issue",
        None,
        {"project_key": "PROJ", "issue_type": "Task", "summary": "new"},
        "create",
        "test",
    )
    child = queue.queue.enqueue(
        "create_issue",
        None,
        {"project_key": "PROJ", "issue_type": "Task", "summary": "child"},
        "create",
        "test",
    )
    dependant = queue.queue.enqueue(
        "add_issuelink",
        producer.operation_id,
        {"link_type": "Relates"},
        "link",
        "test",
        child.operation_id,
    )
    scanned_operation_ids: list[str] = []
    original = dependency_orchestration.inspect_dependencies

    def counted(operation: OperationRecord):
        scanned_operation_ids.append(operation.operation_id)
        return original(operation)

    monkeypatch.setattr(
        dependency_orchestration,
        "inspect_dependencies",
        counted,
    )

    with pytest.raises(DependencyScopeIncomplete):
        queue.queue.cancel(3, [producer.operation_id])

    assert Counter(scanned_operation_ids) == {
        producer.operation_id: 1,
        child.operation_id: 1,
        dependant.operation_id: 1,
    }


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


def test_description_paths_share_attachment_reference_rules() -> None:
    assert dependency_orchestration.attachment_reference_description_paths(
        "create_issue"
    ) == ("description",)
    assert dependency_orchestration.attachment_reference_description_paths(
        "update_field"
    ) == (
        "fields.description",
    )
    assert dependency_orchestration.attachment_reference_description_paths(
        "change_type"
    ) == (
        "fields_mapping.description",
    )
    assert dependency_orchestration.attachment_reference_description_paths(
        "transition_issue"
    ) == (
        "fields.description",
    )


def test_create_description_sources_are_normalized_by_priority(
    queue, tmp_path
) -> None:
    file_source = tmp_path / "file-source.txt"
    top_source = tmp_path / "top-source.txt"
    file_source.write_text("file wins", encoding="utf-8")
    top_source.write_text("top wins", encoding="utf-8")
    missing = tmp_path / "must-not-be-read.txt"

    first_payload: dict[str, object] = {
        "project_key": "PROJ",
        "issue_type": "Task",
        "summary": "file",
        "description_file": str(file_source),
        "description": {"file": str(missing)},
        "extra_fields": {"labels": ["kept"]},
    }
    first = queue.queue.enqueue(
        "create_issue", None, first_payload, "file", "test"
    )
    assert first.payload["description"] == "file wins"
    assert "description_file" not in first.payload
    assert first.payload["extra_fields"] == {"labels": ["kept"]}
    assert first_payload["description_file"] == str(file_source)

    second = queue.queue.enqueue(
        "create_issue",
        None,
        {
            "project_key": "PROJ",
            "issue_type": "Task",
            "summary": "top",
            "description_file": None,
            "description": {"file": str(top_source)},
            "extra_fields": {"components": [{"name": "API"}]},
        },
        "top",
        "test",
    )
    assert second.payload["description"] == "top wins"
    assert second.payload["extra_fields"] == {
        "components": [{"name": "API"}]
    }


@pytest.mark.parametrize(
    "reserved_field",
    ["project", "issuetype", "summary", "description", "priority", "assignee"],
)
def test_create_rejects_every_dedicated_field_in_extra_fields_without_writes(
    queue, reserved_field: str
) -> None:
    with pytest.raises(StoreError, match="extra_fields 不得包含专用字段"):
        queue.queue.enqueue(
            "create_issue",
            None,
            {
                "project_key": "PROJ",
                "issue_type": "Task",
                "summary": "new",
                "description": "top-level",
                "extra_fields": {reserved_field: "shadow"},
            },
            "create",
            "test",
        )

    assert queue.query.total_active() == 0
    assert queue.query.get_state().revisions == {
        "queue": 0,
        "execution": 0,
        "history": 0,
    }
    connection = open_database("operations")
    try:
        assert connection.execute("SELECT COUNT(*) FROM queue_events").fetchone()[0] == 0
    finally:
        connection.close()


def test_create_extra_fields_description_alone_is_not_an_input_route(queue) -> None:
    with pytest.raises(StoreError, match="请使用顶层 description"):
        queue.queue.enqueue(
            "create_issue",
            None,
            {
                "project_key": "PROJ",
                "issue_type": "Task",
                "summary": "new",
                "extra_fields": {"description": "nested-only"},
            },
            "create",
            "test",
        )


def test_execution_gate_rejects_legacy_create_field_override() -> None:
    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="create_issue",
        issue_key=None,
        risk_level="medium",
        payload={
            "project_key": "PROJ",
            "issue_type": "Task",
            "summary": "new",
            "description": None,
            "extra_fields": {"description": "legacy"},
        },
        description="create",
        source="test",
        created_at=1,
    )

    with pytest.raises(OperationSafetyError, match="extra_fields 不得包含专用字段"):
        assert_safe_to_execute(operation)


def test_batch_materialization_uses_one_typed_resolution_sequence() -> None:
    issue_producer_id = "op-" + "b" * 32
    attachment_producer_id = "op-" + "c" * 32
    marker = f"[^{{{{attachment:{attachment_producer_id}}}}}]"
    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="add_comment",
        issue_key=issue_producer_id,
        risk_level="low",
        payload={"body": marker},
        description="consumer",
        source="test",
        created_at=1,
    )
    inspection = dependency_orchestration.inspect_dependencies(operation)
    assert dependency_orchestration.collect_referenced_operation_ids(
        inspection
    ) == (issue_producer_id, attachment_producer_id)
    resolutions = dependency_orchestration.collect_resolutions(
        inspection,
        {
            issue_producer_id: DependencyState(
                operation_id=issue_producer_id,
                operation_type="create_issue",
                state="success",
                new_issue_key="PROJ-10",
            ),
            attachment_producer_id: DependencyState(
                operation_id=attachment_producer_id,
                operation_type="add_attachment",
                state="success",
                operation_issue_key=issue_producer_id,
                attachment_result=AttachmentResult(
                    id="10", filename="a-1.jpg"
                ),
            ),
        },
    )
    assert isinstance(resolutions[0], IssueDependencyResolution)
    assert isinstance(resolutions[1], AttachmentDependencyResolution)

    result = dependency_orchestration.materialize_references(
        operation, resolutions
    )

    assert result.operation.issue_key == "PROJ-10"
    assert result.operation.payload["body"] == "[^a-1.jpg]"
    assert operation.issue_key == issue_producer_id
    assert operation.payload["body"] == marker
    assert [event.action_code for event in result.events] == [
        "issue_key_resolved",
        "attachment_resolved",
    ]
    assert result.events[0].data["fields"] == ("issue_key",)
    assert result.events[1].data["paths"] == ("body",)
    event_data = result.events[1].data
    attachment_data = event_data["attachment_result"]
    assert isinstance(attachment_data, dict)
    attachment_data["id"] = "mutated-copy"
    assert result.events[1].data["attachment_result"] == {
        "id": "10",
        "filename": "a-1.jpg",
    }


def test_materializers_return_original_snapshot_when_no_resolution_matches() -> None:
    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="add_comment",
        issue_key="PROJ-1",
        risk_level="low",
        payload={"body": "plain"},
        description="consumer",
        source="test",
        created_at=1,
    )
    result = dependency_orchestration.materialize_references(
        operation,
        (
            IssueDependencyResolution(
                producer_operation_id="op-" + "b" * 32,
                issue_key="PROJ-2",
            ),
            AttachmentDependencyResolution(
                producer_operation_id="op-" + "c" * 32,
                attachment_result=AttachmentResult(
                    id="10", filename="a-1.jpg"
                ),
            ),
        ),
    )

    assert result.operation is operation
    assert result.events == ()


def test_issue_dual_field_materialization_validates_one_snapshot(
    monkeypatch,
) -> None:
    producer_id = "op-" + "b" * 32
    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="add_issuelink",
        issue_key=producer_id,
        target_issue_key=producer_id,
        risk_level="low",
        payload={"link_type": "Relates"},
        description="consumer",
        source="test",
        created_at=1,
    )
    original_validate = OperationRecord.model_validate
    validation_count = 0

    def counted_validate(cls, value, *args, **kwargs):
        nonlocal validation_count
        validation_count += 1
        return original_validate(value, *args, **kwargs)

    monkeypatch.setattr(
        OperationRecord,
        "model_validate",
        classmethod(counted_validate),
    )

    result = dependency_orchestration.materialize_references(
        operation,
        (
            IssueDependencyResolution(
                producer_operation_id=producer_id,
                issue_key="PROJ-10",
            ),
        ),
    )

    assert validation_count == 1
    assert result.operation.issue_key == "PROJ-10"
    assert result.operation.target_issue_key == "PROJ-10"
    assert result.events[0].data["fields"] == (
        "issue_key",
        "target_issue_key",
    )
    assert operation.issue_key == producer_id
    assert operation.target_issue_key == producer_id


def test_attachment_multi_path_materialization_validates_one_snapshot(
    monkeypatch,
) -> None:
    producer_id = "op-" + "b" * 32
    link = f"[^{{{{attachment:{producer_id}}}}}]"
    image = f"!{{{{attachment:{producer_id}}}}}!"
    operation = OperationRecord(
        operation_id="op-" + "a" * 32,
        operation_type="transition_issue",
        issue_key="PROJ-1",
        risk_level="high",
        payload={
            "transition_name": "Start",
            "fields": {"description": f"{link} {link}"},
            "comment": image,
        },
        description="consumer",
        source="test",
        created_at=1,
    )
    original_validate = OperationRecord.model_validate
    validation_count = 0

    def counted_validate(cls, value, *args, **kwargs):
        nonlocal validation_count
        validation_count += 1
        return original_validate(value, *args, **kwargs)

    monkeypatch.setattr(
        OperationRecord,
        "model_validate",
        classmethod(counted_validate),
    )

    result = dependency_orchestration.materialize_references(
        operation,
        (
            AttachmentDependencyResolution(
                producer_operation_id=producer_id,
                attachment_result=AttachmentResult(
                    id="10", filename="a-1.jpg"
                ),
            ),
        ),
    )

    assert validation_count == 1
    assert result.operation.payload["fields"] == {
        "description": "[^a-1.jpg] [^a-1.jpg]"
    }
    assert result.operation.payload["comment"] == "!a-1.jpg!"
    assert result.events[0].data["paths"] == (
        "fields.description",
        "comment",
    )
    assert operation.payload["fields"] == {"description": f"{link} {link}"}
    assert operation.payload["comment"] == image


def test_description_snapshot_is_copy_on_write(tmp_path) -> None:
    path = tmp_path / "description.txt"
    path.write_text("snapshot", encoding="utf-8")
    untouched = {"labels": ["same"]}
    fields: dict[str, object] = {
        "description": {"file": str(path)},
        "summary": "same",
    }
    payload: dict[str, object] = {"fields": fields, "untouched": untouched}

    snapped = _snapshot_description_payload("update_field", payload)

    assert payload["fields"] is fields
    assert fields["description"] == {"file": str(path)}
    assert snapped is not payload
    assert snapped["fields"] is not fields
    assert snapped["untouched"] is untouched
    assert isinstance(snapped["fields"], dict)
    assert snapped["fields"]["description"] == "snapshot"


def test_inline_description_snapshot_reuses_untouched_container() -> None:
    fields: dict[str, object] = {"description": "inline", "summary": "same"}
    payload: dict[str, object] = {"fields": fields}

    snapped = _snapshot_description_payload("update_field", payload)

    assert snapped is not payload
    assert snapped["fields"] is fields


def test_text_source_shape_is_strict_and_field_aware(tmp_path) -> None:
    path = tmp_path / "source.txt"
    path.write_text("text", encoding="utf-8")
    assert snapshot_text_source("inline", field_path="fields.description") == "inline"
    assert snapshot_text_source(None, field_path="fields.description") is None
    assert snapshot_text_source(
        {"file": str(path)}, field_path="fields.description"
    ) == "text"

    invalid_values: tuple[object, ...] = (
        {"file": str(path), "extra": True},
        {"file": ""},
        {"file": 1},
        {"file": "relative.txt"},
        ["not", "text"],
        False,
    )
    for value in invalid_values:
        with pytest.raises(TextFileError, match="fields.description"):
            snapshot_text_source(value, field_path="fields.description")


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
        not item.dependency_states
        for group in queue.query.get_state().queue.groups
        for item in group.operations
    )


def test_upload_result_is_persisted_and_broadcast_to_staging(
    queue, monkeypatch
) -> None:
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
    materialized_operation_ids: list[str] = []
    original_materialize = dependency_orchestration.materialize_references

    def counted_materialize(operation: OperationRecord, resolutions):
        materialized_operation_ids.append(operation.operation_id)
        return original_materialize(operation, resolutions)

    monkeypatch.setattr(
        dependency_orchestration,
        "materialize_references",
        counted_materialize,
    )
    queue.execution.complete_execution(
        claim.execution_id, "success", "uploaded", None, None, 2, result
    )

    assert Counter(materialized_operation_ids) == {
        attachment.operation_id: 1,
        comment.operation_id: 1,
    }

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
