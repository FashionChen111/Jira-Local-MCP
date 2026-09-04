from __future__ import annotations

import pytest

from core.operations.models import DependencyState, OperationRecord
from core.operations.rules.ordering import (
    OrderingError,
    automatic_queue,
    natural_issue_key,
    stable_dependency_order,
)


def op(
    operation_id: str,
    operation_type: str,
    issue_key: str | None,
    created_at: int,
    *,
    target_issue_key: str | None = None,
) -> OperationRecord:
    payloads = {
        "create_issue": {"project_key": "P", "issue_type": "Task", "summary": "x"},
        "update_field": {"fields": {"summary": "x"}},
        "update_custom_field": {"custom_fields": {"customfield_1": "x"}},
        "transition_issue": {"transition_name": "Start", "fields": {}, "comment": None},
        "move_issue": {"target_project_key": "OTHER", "target_issue_type": None},
        "change_type": {"target_issue_type": "Bug", "fields_mapping": {}},
        "add_attachment": {"file_path": "C:\\a.txt", "file_name": None},
        "add_comment": {"body": "x"},
        "add_worklog": {"time_spent": "1h", "comment": None, "started": None},
        "add_issuelink": {"link_type": "Relates"},
    }
    return OperationRecord.model_validate(
        {
            "operation_id": operation_id,
            "operation_type": operation_type,
            "issue_key": issue_key,
            "target_issue_key": target_issue_key,
            "risk_level": "low",
            "payload": payloads[operation_type],
            "description": operation_id,
            "source": "test",
            "created_at": created_at,
            "attempt_no": 1,
        }
    )


def test_natural_jira_groups_category_order_and_standalone_last() -> None:
    records = [
        op("op-" + "1" * 32, "add_comment", "PROJ-10", 1),
        op("op-" + "2" * 32, "add_worklog", "PROJ-2", 1),
        op("op-" + "3" * 32, "transition_issue", "PROJ-2", 4),
        op("op-" + "4" * 32, "update_custom_field", "PROJ-2", 3),
        op("op-" + "5" * 32, "update_field", "PROJ-2", 5),
        op("op-" + "6" * 32, "add_issuelink", "PROJ-1", 0, target_issue_key="PROJ-2"),
        op("op-" + "7" * 32, "move_issue", "PROJ-3", 2),
        op("op-" + "8" * 32, "change_type", "PROJ-3", 3),
    ]

    layout = automatic_queue(records)

    assert [group.group_key for group in layout.groups] == ["PROJ-2", "PROJ-10"]
    assert [item.operation_type for item in layout.groups[0].operations] == [
        "update_field",
        "update_custom_field",
        "transition_issue",
        "add_worklog",
    ]
    assert [item.operation_type for item in layout.standalone] == [
        "add_issuelink",
        "move_issue",
        "change_type",
    ]
    assert sorted(["PROJ-10", "PROJ-2", "PROJ-1"], key=natural_issue_key) == [
        "PROJ-1",
        "PROJ-2",
        "PROJ-10",
    ]


def test_pending_groups_use_producer_time_then_member_fallback() -> None:
    first_id = "op-" + "a" * 32
    second_id = "op-" + "b" * 32
    missing_id = "op-" + "c" * 32
    records = [
        op(first_id, "create_issue", None, 20),
        op("op-" + "d" * 32, "add_comment", first_id, 1),
        op(second_id, "create_issue", None, 10),
        op("op-" + "e" * 32, "add_comment", missing_id, 15),
    ]
    states = {
        item: DependencyState(
            operation_id=item,
            operation_type="create_issue" if item != missing_id else None,
            state="queued" if item != missing_id else "unknown",
        )
        for item in (first_id, second_id, missing_id)
    }

    layout = automatic_queue(records, states)

    assert [group.group_key for group in layout.groups] == [
        second_id,
        missing_id,
        first_id,
    ]
    first_group = next(group for group in layout.groups if group.group_key == first_id)
    assert [item.operation_type for item in first_group.operations] == [
        "create_issue",
        "add_comment",
    ]


def test_stable_topological_order_preserves_display_priority_and_rejects_cycles() -> None:
    producer = "op-" + "a" * 32
    child = "op-" + "b" * 32
    independent = "op-" + "c" * 32
    ordered = stable_dependency_order(
        (
            op(child, "add_comment", producer, 1),
            op(independent, "add_comment", "PROJ-1", 2),
            op(producer, "create_issue", None, 3),
        ),
        {child: (producer,), independent: (), producer: ()},
    )
    assert [item.operation_id for item in ordered] == [
        independent,
        producer,
        child,
    ]
    already_valid = stable_dependency_order(
        (
            op(producer, "create_issue", None, 1),
            op(child, "add_comment", producer, 2),
            op(independent, "add_comment", "PROJ-1", 3),
        ),
        {producer: (), child: (producer,), independent: ()},
    )
    assert [item.operation_id for item in already_valid] == [
        producer,
        child,
        independent,
    ]

    left = "op-" + "d" * 32
    right = "op-" + "e" * 32
    with pytest.raises(OrderingError, match="循环"):
        stable_dependency_order(
            (
                op(left, "add_comment", right, 1),
                op(right, "add_comment", left, 2),
            ),
            {left: (right,), right: (left,)},
        )
