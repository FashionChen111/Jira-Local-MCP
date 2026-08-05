from core.operations.ordering import (
    automatic_ids,
    default_group_order,
    derive_manual_anchors,
    natural_issue_key,
)


def test_natural_issue_key_orders_numeric_suffixes() -> None:
    issue_keys = ["PROJ-10", "PROJ-2", "PROJ-1"]

    assert sorted(issue_keys, key=natural_issue_key) == [
        "PROJ-1",
        "PROJ-2",
        "PROJ-10",
    ]


def test_default_group_order_uses_category_time_and_id() -> None:
    rows = [
        {"operation_id": "comment", "category": "comment", "created_at": 1},
        {"operation_id": "field-b", "category": "standard_field", "created_at": 2},
        {"operation_id": "field-a", "category": "standard_field", "created_at": 2},
        {"operation_id": "workflow", "category": "workflow", "created_at": 1},
    ]

    assert [row["operation_id"] for row in default_group_order(rows)] == [
        "field-a",
        "field-b",
        "workflow",
        "comment",
    ]


def test_automatic_ids_prefers_earlier_default_entries_deterministically() -> None:
    assert automatic_ids(
        ["a", "b", "c", "d"], ["b", "a", "d", "c"]
    ) == {"a", "c"}
    assert automatic_ids(["a"], []) == set()


def test_derive_manual_anchors_only_marks_moved_entries() -> None:
    rows = [
        {
            "operation_id": operation_id,
            "category": "standard_field",
            "created_at": index,
        }
        for index, operation_id in enumerate(("a", "b", "c", "d"))
    ]

    assert derive_manual_anchors(rows, ["b", "a", "d", "c"]) == {
        "b": "",
        "a": None,
        "d": "a",
        "c": None,
    }
