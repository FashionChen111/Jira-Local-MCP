"""Operations safety, direct-target rules, and advisory prompt contracts."""

from __future__ import annotations

import pytest
from pydantic import JsonValue

from core.operations.rules.dependencies import DependencyError, validate_target_syntax
from core.operations.models import OperationDraft, OperationRecord
from core.operations.rules.safety import (
    OperationSafetyError,
    assert_allowed_and_non_delete,
    assert_valid_payload_shape,
    validate_for_enqueue,
)
from knowledge.prompts import PROMPTS


@pytest.mark.parametrize("operation", ["delete_issue", "remove_comment", "purge"])
def test_unknown_or_delete_operation_rejected(operation: str) -> None:
    with pytest.raises(OperationSafetyError):
        assert_allowed_and_non_delete(operation, {})


@pytest.mark.parametrize(
    "payload",
    [{"remove": True}, {"value": "delete this"}, {"nested": ["删除"]}],
)
def test_payload_delete_semantics_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(OperationSafetyError):
        assert_allowed_and_non_delete("add_comment", payload)  # type: ignore[arg-type]


def record(
    operation_type: str,
    issue_key: str | None,
    target_issue_key: str | None = None,
) -> OperationRecord:
    return OperationRecord.model_validate(
        {
            "operation_id": "op-" + "a" * 32,
            "operation_type": operation_type,
            "issue_key": issue_key,
            "target_issue_key": target_issue_key,
            "risk_level": "low",
            "payload": {"body": "x"}
            if operation_type == "add_comment"
            else {"link_type": "Relates"},
            "description": "x",
            "source": "test",
            "created_at": 1,
            "attempt_no": 1,
        }
    )


@pytest.mark.parametrize("key", [None, "$TASK{x}", "{new}", "bad", "PROJ-x"])
def test_modify_requires_real_key_or_server_operation_id(key: str | None) -> None:
    with pytest.raises(DependencyError):
        validate_target_syntax(record("add_comment", key), allow_operation_ids=True)


def test_clone_source_is_always_real_and_link_target_is_explicit() -> None:
    operation_id = "op-" + "b" * 32
    with pytest.raises(DependencyError, match="clone_issue"):
        validate_target_syntax(
            OperationRecord(
                operation_id="op-" + "a" * 32,
                operation_type="clone_issue",
                issue_key=operation_id,
                risk_level="medium",
                payload={"summary": None},
                description="clone",
                source="test",
                created_at=1,
            ),
            allow_operation_ids=True,
        )
    validate_target_syntax(
        record("add_issuelink", "PROJ-1", operation_id),
        allow_operation_ids=True,
    )
    with pytest.raises(DependencyError, match="target_issue_key"):
        validate_target_syntax(
            record("add_comment", "PROJ-1", "PROJ-2"),
            allow_operation_ids=True,
        )


def test_create_has_no_key_and_payload_cannot_duplicate_targets() -> None:
    draft = OperationDraft(
        operation_type="create_issue",
        issue_key=None,
        payload={"project_key": "P", "issue_type": "Task", "summary": "x"},
        description="create",
    )
    risk, _ = validate_for_enqueue(draft)
    assert risk == "medium"

    duplicate = OperationDraft(
        operation_type="add_comment",
        issue_key="PROJ-1",
        payload={"issue_key": "PROJ-1", "body": "x"},
        description="comment",
    )
    with pytest.raises(OperationSafetyError, match="不得重复"):
        validate_for_enqueue(duplicate)


@pytest.mark.parametrize(
    ("operation_type", "payload"),
    [
        (
            "create_issue",
            {"project_key": "P", "issue_type": "Task", "summary": "new"},
        ),
        ("clone_issue", {}),
        ("move_issue", {"target_project_key": "OTHER"}),
        ("change_type", {"target_issue_type": "Bug"}),
        ("transition_issue", {"transition_name": "Start"}),
        ("update_field", {"fields": {"summary": "new"}}),
        (
            "update_custom_field",
            {"custom_fields": {"customfield_10001": ["one"]}},
        ),
        ("add_attachment", {"file_path": "C:\\fixtures\\a.txt"}),
        ("add_comment", {"body": "hello"}),
        ("add_worklog", {"time_spent": "1h"}),
        ("add_issuelink", {"link_type": "Relates"}),
    ],
)
def test_payload_shape_covers_every_operation_without_jira_value_rules(
    operation_type: str, payload: dict[str, JsonValue]
) -> None:
    assert_valid_payload_shape(operation_type, payload)


@pytest.mark.parametrize(
    ("operation_type", "payload", "message"),
    [
        (
            "create_issue",
            {"project_key": "P", "issue_type": "Task"},
            "缺少必需字段",
        ),
        ("clone_issue", {"clone_subtasks": "false"}, "必须是布尔值"),
        (
            "transition_issue",
            {"transition_name": "Start", "fields": []},
            "必须是对象",
        ),
        ("update_field", {"fields": {}}, "必须是非空对象"),
        ("add_comment", {"body": 1}, "必须是字符串"),
        (
            "add_worklog",
            {"time_spent": "1h", "ignored": True},
            "包含未定义字段",
        ),
        (
            "update_custom_field",
            {"custom_fields": {"summary": "wrong tool"}},
            "包含无效字段 ID",
        ),
    ],
)
def test_payload_shape_rejects_only_handler_relevant_shape_errors(
    operation_type: str,
    payload: dict[str, JsonValue],
    message: str,
) -> None:
    with pytest.raises(OperationSafetyError, match=message):
        assert_valid_payload_shape(operation_type, payload)


@pytest.mark.parametrize(
    ("operation_type", "payload"),
    [
        (
            "create_issue",
            {
                "project_key": "P",
                "issue_type": "Task",
                "summary": "new",
                "extra_fields": {"description": {"file": "stale.txt"}},
            },
        ),
        ("update_field", {"fields": {"description": ["not text"]}}),
        (
            "change_type",
            {
                "target_issue_type": "Bug",
                "fields_mapping": {"description": False},
            },
        ),
        (
            "transition_issue",
            {"transition_name": "Start", "fields": {"description": 1}},
        ),
    ],
)
def test_payload_shape_requires_materialized_description_snapshots(
    operation_type: str, payload: dict[str, JsonValue]
) -> None:
    with pytest.raises(OperationSafetyError, match="入队时固化"):
        assert_valid_payload_shape(operation_type, payload)


def test_enqueue_boundary_reuses_payload_shape_validation() -> None:
    draft = OperationDraft(
        operation_type="add_comment",
        issue_key="PROJ-1",
        payload={"body": 1},
        description="invalid comment",
    )

    with pytest.raises(OperationSafetyError, match="payload.body"):
        validate_for_enqueue(draft)


def test_prompts_publish_review_and_untrusted_data_contract() -> None:
    combined = "\n".join(PROMPTS.values())
    assert "jira_show_queue" in combined and "jira_get_execution_log" in combined
    assert "jira_show_pending_queue" not in combined
    assert "不可信业务数据" in combined
    assert "仅入队" in combined and "尚未执行" in combined
    assert "create-with-followups-workflow" in PROMPTS
