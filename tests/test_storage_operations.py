"""Operations repository transactions and application-service state transitions."""

from __future__ import annotations

import json
import sqlite3

import pytest
from pydantic import ValidationError

from core.operations import ConfirmationRequired, RevisionConflict, StoreError
from core.operations.repository import OperationRepository
from core.storage.database import open_database, transaction


def state(services):
    return services.query.get_state().model_dump(mode="json")


def enqueue_comment(services, key: str, body: str = "x"):
    return services.queue.enqueue(
        "add_comment", key, {"body": body}, body, "test"
    )


def test_schema_pragmas_and_revisions_initialized(queue) -> None:
    connection = open_database("operations")
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        identity = connection.execute(
            "SELECT value FROM schema_metadata WHERE name='schema_identity'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert identity == "jira-local-mcp-operations-attachment-dependencies-v1"
    assert state(queue)["revisions"] == {"queue": 0, "execution": 0, "history": 0}


def test_repository_terminalizes_one_dependency_failure(queue) -> None:
    operation = enqueue_comment(queue, "PROJ-1")
    queue.submission.submit(1, [operation.operation_id], False)
    repository = OperationRepository()

    with transaction() as connection:
        staging = repository.staging_record(connection, operation.operation_id)
        assert staging is not None
        repository.terminalize_dependency_failure(
            connection,
            staging,
            producer_operation_id="op-" + "f" * 32,
        )

    connection = open_database("operations")
    try:
        staging_count = connection.execute(
            "SELECT COUNT(*) FROM execution_staging WHERE operation_id=?",
            (operation.operation_id,),
        ).fetchone()[0]
        execution = connection.execute(
            "SELECT execution_id,outcome FROM execution_records WHERE operation_id=?",
            (operation.operation_id,),
        ).fetchone()
        event_count = connection.execute(
            """SELECT COUNT(*) FROM queue_events
               WHERE operation_id=? AND action_code='dependency_failed'""",
            (operation.operation_id,),
        ).fetchone()[0]
    finally:
        connection.close()

    assert staging_count == 0
    assert execution["execution_id"].startswith("exec-")
    assert execution["outcome"] == "dependency_failed"
    assert event_count == 1


def test_dependency_failure_transition_rolls_back_with_outer_transaction(queue) -> None:
    operation = enqueue_comment(queue, "PROJ-1")
    queue.submission.submit(1, [operation.operation_id], False)
    repository = OperationRepository()

    with pytest.raises(RuntimeError, match="rollback dependency transition"):
        with transaction() as connection:
            staging = repository.staging_record(connection, operation.operation_id)
            assert staging is not None
            repository.terminalize_dependency_failure(
                connection,
                staging,
                producer_operation_id="op-" + "e" * 32,
            )
            raise RuntimeError("rollback dependency transition")

    connection = open_database("operations")
    try:
        staging_count = connection.execute(
            "SELECT COUNT(*) FROM execution_staging WHERE operation_id=?",
            (operation.operation_id,),
        ).fetchone()[0]
        execution_count = connection.execute(
            "SELECT COUNT(*) FROM execution_records WHERE operation_id=?",
            (operation.operation_id,),
        ).fetchone()[0]
        event_count = connection.execute(
            """SELECT COUNT(*) FROM queue_events
               WHERE operation_id=? AND action_code='dependency_failed'""",
            (operation.operation_id,),
        ).fetchone()[0]
    finally:
        connection.close()

    assert staging_count == 1
    assert execution_count == 0
    assert event_count == 0


def test_enqueue_persists_only_business_snapshot_and_atomic_event(queue) -> None:
    operation = enqueue_comment(queue, "PROJ-2", "hello")
    connection = open_database("operations")
    try:
        row = connection.execute(
            "SELECT * FROM queue_items WHERE operation_id=?", (operation.operation_id,)
        ).fetchone()
        event = connection.execute(
            "SELECT * FROM queue_events WHERE operation_id=?", (operation.operation_id,)
        ).fetchone()
        columns = {
            item[1] for item in connection.execute("PRAGMA table_info(queue_items)")
        }
    finally:
        connection.close()
    assert json.loads(row["payload_json"]) == {"body": "hello"}
    assert row["issue_key"] == "PROJ-2" and row["target_issue_key"] is None
    assert event["action_code"] == "mcp_append"
    assert not {
        "layout_kind",
        "category",
        "layout_scope",
        "position",
        "manual_after_operation_id",
        "field_references_json",
    } & columns


def test_repository_strictly_rejects_non_finite_persisted_payload(queue) -> None:
    operation = enqueue_comment(queue, "PROJ-1")
    connection = open_database("operations")
    try:
        connection.execute(
            "UPDATE queue_items SET payload_json=? WHERE operation_id=?",
            ('{"body":NaN}', operation.operation_id),
        )
    finally:
        connection.close()
    with pytest.raises(ValidationError):
        queue.query.get_state()


def test_cancel_is_revision_guarded_and_all_or_nothing(queue) -> None:
    operation = enqueue_comment(queue, "PROJ-1")
    with pytest.raises(RevisionConflict):
        queue.queue.cancel(0, [operation.operation_id], False)
    with pytest.raises(StoreError):
        queue.queue.cancel(1, [operation.operation_id, "op-" + "f" * 32], False)
    assert queue.query.total_active() == 1
    queue.queue.cancel(1, [operation.operation_id], False)
    assert queue.query.total_active() == 0


def test_submit_uses_automatic_natural_display_order(queue) -> None:
    ten = enqueue_comment(queue, "PROJ-10", "ten")
    two = enqueue_comment(queue, "PROJ-2", "two")
    accepted, revisions = queue.submission.submit(
        2, [ten.operation_id, two.operation_id], False
    )
    assert accepted == [two.operation_id, ten.operation_id]
    staged = state(queue)["execution"]["staged"]
    assert [item["operation_id"] for item in staged] == accepted
    assert [item["stage_order"] for item in staged] == [1, 2]
    assert revisions == {"queue": 3, "execution": 1, "history": 0}


def test_bulk_risk_is_submission_scoped(queue) -> None:
    operations = [
        queue.queue.enqueue(
            "update_field",
            "PROJ-1",
            {"fields": {f"field_{index}": index}},
            str(index),
            "test",
        )
        for index in range(5)
    ]
    ids = [item.operation_id for item in operations]
    with pytest.raises(ConfirmationRequired):
        queue.submission.validate_submission(5, ids, False)
    queue.submission.submit(5, ids, True)
    assert all(
        item["submission_risk_level"] == "high"
        for item in state(queue)["execution"]["staged"]
    )


def test_enqueue_event_failure_rolls_back_business_row(queue) -> None:
    connection = open_database("operations")
    try:
        connection.execute(
            """CREATE TRIGGER fail_enqueue_event BEFORE INSERT ON queue_events
               BEGIN SELECT RAISE(ABORT, 'event failed'); END"""
        )
    finally:
        connection.close()
    with pytest.raises(sqlite3.IntegrityError, match="event failed"):
        enqueue_comment(queue, "PROJ-1")
    assert queue.query.total_active() == 0
    assert state(queue)["revisions"]["queue"] == 0


def test_staging_failure_rolls_back_queue_and_events(queue) -> None:
    operation = enqueue_comment(queue, "PROJ-1")
    connection = open_database("operations")
    try:
        connection.execute(
            """CREATE TRIGGER fail_staging BEFORE INSERT ON execution_staging
               BEGIN SELECT RAISE(ABORT, 'staging failed'); END"""
        )
    finally:
        connection.close()
    with pytest.raises(sqlite3.IntegrityError, match="staging failed"):
        queue.submission.submit(1, [operation.operation_id], False)
    current = state(queue)
    assert current["queue"]["groups"][0]["operations"][0]["operation_id"] == operation.operation_id
    assert not current["execution"]["staged"]
    assert current["revisions"] == {"queue": 1, "execution": 0, "history": 0}


def test_claim_complete_and_history_are_self_contained(queue) -> None:
    operation = enqueue_comment(queue, "PROJ-1", "done")
    queue.submission.submit(1, [operation.operation_id], False)
    claim = queue.execution.claim_next()
    assert claim is not None and claim.operation.issue_key == "PROJ-1"
    queue.execution.complete_execution(
        claim.execution_id, "success", "done", None, None, 3
    )
    record = queue.query.read_execution_log(1)[0]
    assert record["operation_id"] == operation.operation_id
    assert record["payload"] == {"body": "done"}
    assert record["outcome"] == "success"
    assert not state(queue)["execution"]["executing"]


def test_execution_result_failure_retains_executing_for_recovery(queue) -> None:
    operation = enqueue_comment(queue, "PROJ-1")
    queue.submission.submit(1, [operation.operation_id], False)
    claim = queue.execution.claim_next()
    assert claim is not None
    connection = open_database("operations")
    try:
        connection.execute(
            """CREATE TRIGGER fail_result BEFORE UPDATE OF outcome ON execution_records
               BEGIN SELECT RAISE(ABORT, 'result failed'); END"""
        )
    finally:
        connection.close()
    with pytest.raises(sqlite3.IntegrityError, match="result failed"):
        queue.execution.complete_execution(
            claim.execution_id, "success", "done", None, None, 1
        )
    assert state(queue)["execution"]["executing"][0]["operation_id"] == operation.operation_id


def prepare_recovery(queue):
    first = enqueue_comment(queue, "PROJ-1", "first")
    second = enqueue_comment(queue, "PROJ-2", "second")
    queue.submission.submit(2, [first.operation_id, second.operation_id], False)
    claim = queue.execution.claim_next()
    assert claim is not None
    assert queue.execution.recover_interrupted() == 1
    return first, second, claim


def test_recovery_marks_indeterminate_and_later_held(queue) -> None:
    first, second, _ = prepare_recovery(queue)
    execution = state(queue)["execution"]
    assert execution["indeterminate"][0]["operation_id"] == first.operation_id
    assert execution["held"][0]["operation_id"] == second.operation_id
    assert queue.execution.claim_next() is None


def test_not_applied_reuses_identity_and_increments_attempt(queue) -> None:
    first, second, _ = prepare_recovery(queue)
    queue.recovery.resolve_indeterminate(first.operation_id, "not_applied", False)
    queued = next(
        item
        for group in state(queue)["queue"]["groups"]
        for item in group["operations"]
    )
    assert queued["operation_id"] == first.operation_id
    assert queued["attempt_no"] == 2
    queue.recovery.resolve_held([second.operation_id], "abandon")


def test_held_requeue_keeps_attempt_and_resume_preserves_stage_order(queue) -> None:
    first, second, _ = prepare_recovery(queue)
    held_order = state(queue)["execution"]["held"][0]["stage_order"]
    queue.recovery.resolve_indeterminate(first.operation_id, "succeeded", False)
    queue.recovery.resolve_held([second.operation_id], "resume")
    resumed = state(queue)["execution"]["staged"][0]
    assert resumed["stage_order"] == held_order
    assert resumed["attempt_no"] == 1


def test_high_risk_not_applied_requires_fresh_confirmation(queue) -> None:
    operation = queue.queue.enqueue(
        "transition_issue",
        "PROJ-1",
        {"transition_name": "Close", "comment": None, "fields": {}},
        "close",
        "test",
    )
    queue.submission.submit(1, [operation.operation_id], True)
    assert queue.execution.claim_next() is not None
    queue.execution.recover_interrupted()
    with pytest.raises(ConfirmationRequired):
        queue.recovery.resolve_indeterminate(
            operation.operation_id, "not_applied", False
        )
    queue.recovery.resolve_indeterminate(
        operation.operation_id, "not_applied", True
    )
    queued = state(queue)["queue"]["groups"][0]["operations"][0]
    assert queued["attempt_no"] == 2


@pytest.mark.parametrize("resolution", ["succeeded", "abandoned"])
def test_manual_terminal_resolution_removes_indeterminate(queue, resolution: str) -> None:
    first, _, _ = prepare_recovery(queue)
    queue.recovery.resolve_indeterminate(first.operation_id, resolution, False)
    assert not state(queue)["execution"]["indeterminate"]
    assert queue.query.read_execution_log(1)[0]["resolution"] == resolution
