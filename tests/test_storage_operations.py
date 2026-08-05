"""SQLite queue, layout, transaction, revision, and recovery tests."""
from __future__ import annotations

import sqlite3

import pytest

from core.storage.database import open_database
from core.operations import ConfirmationRequired, RevisionConflict, StoreError


def _comment(store, key="PROJ-1", body="x"):
    return store.enqueue("add_comment", key, {"issue_key": key, "body": body}, f"comment {body}", "test")


def _field(store, key="PROJ-1", name="summary"):
    return store.enqueue("update_field", key, {"issue_key": key, "fields": {name: "value"}}, f"field {name}", "test")


def test_schema_pragmas_and_revisions_initialized(queue):
    connection = open_database("operations")
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert queue.get_revisions() == {"queue": 0, "execution": 0, "history": 0}
    finally:
        connection.close()


def test_enqueue_persists_operation_and_atomic_event(queue):
    operation = _comment(queue)
    state = queue.get_state()
    assert operation.operation_id.startswith("op-")
    assert state["queue"]["groups"][0]["operations"][0]["operation_id"] == operation.operation_id
    connection = open_database("operations")
    try:
        event = connection.execute("SELECT * FROM queue_events").fetchone()
        assert event["action_code"] == "mcp_append"
    finally:
        connection.close()


def test_natural_groups_category_insertion_and_standalone_last(queue):
    ten = _comment(queue, "PROJ-10")
    create = queue.enqueue("create_issue", None, {"project_key": "P", "issue_type": "Task", "summary": "x"}, "create", "test")
    two_comment = _comment(queue, "PROJ-2")
    two_field = _field(queue, "PROJ-2")
    value = queue.get_state()["queue"]
    assert [group["issue_key"] for group in value["groups"]] == ["PROJ-2", "PROJ-10"]
    assert [item["operation_id"] for item in value["groups"][0]["operations"]] == [two_field.operation_id, two_comment.operation_id]
    assert value["groups"][1]["operations"][0]["operation_id"] == ten.operation_id
    assert value["standalone"][0]["operation_id"] == create.operation_id


def test_all_six_categories_use_the_defined_default_order(queue):
    definitions = [
        ("add_worklog", {"issue_key": "PROJ-1", "time_spent": "1h"}),
        ("add_comment", {"issue_key": "PROJ-1", "body": "x"}),
        ("add_attachment", {"issue_key": "PROJ-1", "file_path": "C:\\review.txt"}),
        ("transition_issue", {"issue_key": "PROJ-1", "transition_name": "Start"}),
        ("update_custom_field", {"issue_key": "PROJ-1", "custom_fields": {"customfield_1": "x"}}),
        ("update_field", {"issue_key": "PROJ-1", "fields": {"summary": "x"}}),
    ]
    for operation_type, payload in definitions:
        queue.enqueue(operation_type, "PROJ-1", payload, operation_type, "test")
    operations = queue.get_state()["queue"]["groups"][0]["operations"]
    assert [item["category"] for item in operations] == [
        "standard_field", "custom_field", "workflow", "attachment", "comment", "worklog",
    ]


def _group_operations(queue):
    return queue.get_state()["queue"]["groups"][0]["operations"]


def test_same_category_default_order_uses_created_at_then_operation_id(queue):
    for body in ("a", "b", "c"):
        _comment(queue, body=body)
    operations = _group_operations(queue)
    expected = sorted(operations, key=lambda item: (item["created_at"], item["operation_id"]))
    assert [item["operation_id"] for item in operations] == [item["operation_id"] for item in expected]


def test_manual_anchor_survives_new_grouped_enqueue(queue):
    for body in ("a", "b", "c", "d"):
        _comment(queue, body=body)
    initial = [item["operation_id"] for item in _group_operations(queue)]
    first, second, third, fourth = initial
    queue.reorder(
        4,
        [{"issue_key": "PROJ-1", "operation_ids": [first, third, second, fourth]}],
        [],
    )
    reordered = _group_operations(queue)
    assert [item["operation_id"] for item in reordered] == [first, third, second, fourth]
    assert next(item for item in reordered if item["operation_id"] == third)[
        "manual_after_operation_id"
    ] == first

    added = _comment(queue, body="e")
    rebuilt = _group_operations(queue)
    assert [item["operation_id"] for item in rebuilt] == [
        first, third, second, fourth, added.operation_id,
    ]


def test_group_start_and_manual_anchor_chain_are_preserved(queue):
    for body in ("a", "b", "c", "d"):
        _comment(queue, body=body)
    first, second, third, fourth = [item["operation_id"] for item in _group_operations(queue)]
    queue.reorder(
        4,
        [{"issue_key": "PROJ-1", "operation_ids": [third, fourth, first, second]}],
        [],
    )
    operations = _group_operations(queue)
    by_id = {item["operation_id"]: item for item in operations}
    assert [item["operation_id"] for item in operations] == [third, fourth, first, second]
    assert by_id[third]["manual_after_operation_id"] == ""
    assert by_id[fourth]["manual_after_operation_id"] == third

    added = _comment(queue, body="e")
    assert [item["operation_id"] for item in _group_operations(queue)] == [
        third, fourth, first, second, added.operation_id,
    ]


def test_cross_group_anchor_and_cycle_degrade_to_default_order(queue):
    for body in ("a", "b", "c"):
        _comment(queue, body=body)
    first, second, third = [item["operation_id"] for item in _group_operations(queue)]
    foreign = _comment(queue, key="PROJ-2", body="foreign")
    connection = open_database("operations")
    connection.execute(
        "UPDATE queue_items SET manual_after_operation_id=? WHERE operation_id=?",
        (foreign.operation_id, third),
    )
    connection.execute(
        "UPDATE queue_items SET manual_after_operation_id=? WHERE operation_id=?",
        (second, first),
    )
    connection.execute(
        "UPDATE queue_items SET manual_after_operation_id=? WHERE operation_id=?",
        (first, second),
    )
    connection.close()

    added = _comment(queue, body="d")
    operations = _group_operations(queue)
    assert [item["operation_id"] for item in operations] == [first, second, third, added.operation_id]
    assert all(item["manual_after_operation_id"] is None for item in operations)


def test_anchor_target_removal_and_detach_are_normalized(queue):
    for body in ("a", "b", "c"):
        _comment(queue, body=body)
    first, second, third = [item["operation_id"] for item in _group_operations(queue)]
    queue.reorder(
        3,
        [{"issue_key": "PROJ-1", "operation_ids": [first, third, second]}],
        [],
    )
    queue.cancel([first])
    remaining = _group_operations(queue)
    assert [item["operation_id"] for item in remaining] == [second, third]
    assert all(item["manual_after_operation_id"] is None for item in remaining)

    queue.reorder(
        5,
        [{"issue_key": "PROJ-1", "operation_ids": [second]}],
        [third],
    )
    standalone = queue.get_state()["queue"]["standalone"][0]
    assert standalone["operation_id"] == third
    assert standalone["manual_after_operation_id"] is None


def test_reorder_is_revision_guarded_and_detach_is_one_way(queue):
    operation = _comment(queue)
    with pytest.raises(RevisionConflict):
        queue.reorder(0, [], [operation.operation_id])
    queue.reorder(1, [], [operation.operation_id])
    assert queue.get_state()["queue"]["standalone"][0]["operation_id"] == operation.operation_id
    with pytest.raises(StoreError):
        queue.reorder(2, [{"issue_key": "PROJ-1", "operation_ids": [operation.operation_id]}], [])


def test_reorder_rejects_cross_issue_and_incomplete_layout(queue):
    first = _comment(queue, "PROJ-1")
    second = _comment(queue, "PROJ-2")
    with pytest.raises(StoreError, match="跨 issue"):
        queue.reorder(2, [{"issue_key": "PROJ-1", "operation_ids": [first.operation_id, second.operation_id]}], [])
    with pytest.raises(StoreError, match="完整"):
        queue.reorder(2, [{"issue_key": "PROJ-1", "operation_ids": [first.operation_id]}], [])


def test_cancel_is_all_or_nothing(queue):
    operation = _comment(queue)
    with pytest.raises(StoreError):
        queue.cancel([operation.operation_id, "op-" + "f" * 32])
    assert queue.total_active() == 1
    queue.cancel([operation.operation_id])
    assert queue.total_active() == 0


def test_submit_uses_persisted_server_order(queue):
    ten = _comment(queue, "PROJ-10")
    two = _comment(queue, "PROJ-2")
    accepted, revisions = queue.submit(2, [ten.operation_id, two.operation_id], False)
    assert accepted == [two.operation_id, ten.operation_id]
    staged = queue.get_state()["execution"]["staged"]
    assert [item["stage_order"] for item in staged] == [1, 2]
    assert revisions == {"queue": 3, "execution": 1, "history": 0}


def test_bulk_risk_is_submission_scoped(queue):
    ids = [_field(queue, name=f"field{i}").operation_id for i in range(5)]
    with pytest.raises(ConfirmationRequired):
        queue.validate_submission(5, ids, False)
    queue.submit(5, ids, True)
    assert all(item["submission_risk_level"] == "high" for item in queue.get_state()["execution"]["staged"])


def test_event_and_staging_failures_roll_back_business_state(queue):
    connection = open_database("operations")
    connection.execute("CREATE TRIGGER fail_event BEFORE INSERT ON queue_events BEGIN SELECT RAISE(ABORT,'fail'); END")
    connection.close()
    with pytest.raises(sqlite3.IntegrityError):
        _comment(queue)
    assert queue.total_active() == 0


def test_staging_failure_leaves_queue_unchanged(queue):
    operation = _comment(queue)
    connection = open_database("operations")
    connection.execute("CREATE TRIGGER fail_stage BEFORE INSERT ON execution_staging BEGIN SELECT RAISE(ABORT,'fail'); END")
    connection.close()
    with pytest.raises(sqlite3.IntegrityError):
        queue.submit(1, [operation.operation_id], False)
    assert queue.get_state()["queue"]["groups"][0]["operations"][0]["operation_id"] == operation.operation_id


def test_queue_delete_failure_rolls_back_staging_and_event(queue):
    operation = _comment(queue)
    connection = open_database("operations")
    connection.execute(
        "CREATE TRIGGER fail_queue_delete BEFORE DELETE ON queue_items BEGIN SELECT RAISE(ABORT,'fail'); END"
    )
    connection.close()
    with pytest.raises(sqlite3.IntegrityError):
        queue.submit(1, [operation.operation_id], False)
    state = queue.get_state()
    assert state["queue"]["groups"][0]["operations"][0]["operation_id"] == operation.operation_id
    assert not state["execution"]["staged"]


def test_claim_complete_and_history(queue):
    operation = _comment(queue)
    queue.submit(1, [operation.operation_id], False)
    claim = queue.claim_next()
    assert claim and claim.execution_id.startswith("exec-")
    queue.complete_execution(claim.execution_id, "success", "done", None, "PROJ-9", 3)
    record = queue.read_execution_log(1)[0]
    assert record["outcome"] == "success" and record["new_issue_key"] == "PROJ-9"
    assert not queue.get_state()["execution"]["executing"]


def test_execution_result_failure_is_atomic(queue):
    operation = _comment(queue)
    queue.submit(1, [operation.operation_id], False)
    claim = queue.claim_next()
    assert claim
    connection = open_database("operations")
    connection.execute(
        """CREATE TRIGGER fail_result BEFORE UPDATE ON execution_records
           WHEN NEW.outcome IS NOT NULL BEGIN SELECT RAISE(ABORT,'fail'); END"""
    )
    connection.close()
    with pytest.raises(sqlite3.IntegrityError):
        queue.complete_execution(claim.execution_id, "success", "done", None, None, 1)
    assert queue.get_state()["execution"]["executing"][0]["operation_id"] == operation.operation_id
    connection = open_database("operations")
    try:
        assert connection.execute(
            "SELECT outcome FROM execution_records WHERE execution_id=?", (claim.execution_id,)
        ).fetchone()[0] is None
    finally:
        connection.close()


def _make_blocked(queue):
    first = _comment(queue, body="first")
    second = _comment(queue, body="second")
    queue.submit(2, [first.operation_id, second.operation_id], False)
    claim = queue.claim_next()
    assert claim
    assert queue.recover_interrupted() == 1
    return first, second, claim


def test_recovery_marks_indeterminate_and_later_held(queue):
    first, second, claim = _make_blocked(queue)
    execution = queue.get_state()["execution"]
    assert execution["indeterminate"][0]["operation_id"] == first.operation_id
    assert execution["held"][0]["operation_id"] == second.operation_id
    assert execution["held"][0]["blocked_by_execution_id"] == claim.execution_id
    assert queue.claim_next() is None


def test_not_applied_reuses_operation_and_increments_attempt(queue):
    first, second, first_claim = _make_blocked(queue)
    queue.resolve_indeterminate(first.operation_id, "not_applied", False)
    queued = queue.get_state()["queue"]["groups"][0]["operations"][0]
    assert queued["operation_id"] == first.operation_id and queued["attempt_no"] == 2
    queue.resolve_held([second.operation_id], "abandon")
    queue.submit(4, [first.operation_id], False)
    second_claim = queue.claim_next()
    assert second_claim is not None
    assert second_claim.operation.attempt_no == 2
    assert second_claim.execution_id != first_claim.execution_id


def test_held_requeue_keeps_attempt(queue):
    _first, second, _claim = _make_blocked(queue)
    queue.resolve_held([second.operation_id], "requeue")
    queued = queue.get_state()["queue"]["groups"][0]["operations"][0]
    assert queued["operation_id"] == second.operation_id and queued["attempt_no"] == 1


@pytest.mark.parametrize("action,expected_state", [("resume", "staged"), ("abandon", None)])
def test_other_held_resolutions_do_not_call_jira(queue, action, expected_state):
    _first, second, _claim = _make_blocked(queue)
    queue.resolve_held([second.operation_id], action)
    execution = queue.get_state()["execution"]
    locations = [name for name, items in execution.items() if any(
        item["operation_id"] == second.operation_id for item in items
    )]
    assert locations == ([] if expected_state is None else [expected_state])


def test_high_risk_not_applied_requires_fresh_confirmation(queue):
    operation = queue.enqueue(
        "transition_issue", "PROJ-1",
        {"issue_key": "PROJ-1", "transition_name": "Done"}, "transition", "test",
    )
    queue.submit(1, [operation.operation_id], True)
    queue.claim_next()
    queue.recover_interrupted()
    with pytest.raises(ConfirmationRequired):
        queue.resolve_indeterminate(operation.operation_id, "not_applied", False)
    queue.resolve_indeterminate(operation.operation_id, "not_applied", True)
    assert queue.get_state()["queue"]["groups"][0]["operations"][0]["attempt_no"] == 2


def test_recovery_cleans_terminal_staging_residue(queue):
    operation = _comment(queue)
    queue.submit(1, [operation.operation_id], False)
    claim = queue.claim_next()
    assert claim
    connection = open_database("operations")
    connection.execute(
        "UPDATE execution_records SET outcome='success',finished_at=? WHERE execution_id=?",
        (1, claim.execution_id),
    )
    connection.close()
    assert queue.recover_interrupted() == 0
    assert not queue.get_state()["execution"]["executing"]


@pytest.mark.parametrize("resolution", ["succeeded", "abandoned"])
def test_terminal_reconciliation_removes_staging(queue, resolution):
    first, _second, _claim = _make_blocked(queue)
    queue.resolve_indeterminate(first.operation_id, resolution, False)
    assert not queue.get_state()["execution"]["indeterminate"]
    assert queue.read_execution_log(1)[0]["resolution"] == resolution
