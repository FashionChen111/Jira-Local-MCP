"""Direct primary/target producer references across queue and execution states."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter

import pytest

from core.executing import worker as execution_worker
from core.operations import DependencyScopeIncomplete, StoreError
from core.operations.dependencies import orchestration as dependency_orchestration
from core.operations.models import OperationExecutionResult, OperationRecord
from core.operations.repository import execution as execution_repository
from core.operations.repository import metadata as repository_metadata
from core.operations.repository import queue as queue_repository
from core.storage.database import open_database, transaction


def public_state(services):
    return services.query.get_state().model_dump(mode="json")


def enqueue_create(services, summary: str = "new"):
    return services.queue.enqueue(
        "create_issue",
        None,
        {"project_key": "PROJ", "issue_type_id": "10001", "summary": summary},
        f"create {summary}",
        "test",
    )


def enqueue_comment(services, issue_key: str, body: str = "child"):
    return services.queue.enqueue(
        "add_comment",
        issue_key,
        {"body": body},
        body,
        "test",
    )


def enqueue_link(services, issue_key: str, target_issue_key: str):
    return services.queue.enqueue(
        "add_issuelink",
        issue_key,
        {"link_type": "Relates"},
        "link",
        "test",
        target_issue_key,
    )


def finish_create(services, issue_key: str):
    claim = services.execution.claim_next()
    assert claim is not None and claim.operation.operation_type == "create_issue"
    services.execution.complete_execution(
        claim.execution_id, "success", "created", None, issue_key, 1
    )
    return claim


def unresolved_consumer(producer_operation_id: str) -> OperationRecord:
    return OperationRecord(
        operation_id="op-" + "f" * 32,
        operation_type="add_comment",
        issue_key=producer_operation_id,
        risk_level="low",
        payload={"body": "malformed persisted consumer"},
        description="malformed persisted consumer",
        source="test",
        created_at=10,
    )


def test_pending_group_and_submission_closure_are_automatic(queue) -> None:
    producer = enqueue_create(queue)
    child = enqueue_comment(queue, producer.operation_id)

    group = public_state(queue)["queue"]["groups"][0]
    assert group["kind"] == "pending_issue"
    assert group["group_key"] == producer.operation_id
    assert group["producer_state"] == "queued"
    assert [item["operation_id"] for item in group["operations"]] == [
        producer.operation_id,
        child.operation_id,
    ]
    assert group["operations"][1]["dependency_states"] == {
        producer.operation_id: "queued"
    }

    with pytest.raises(StoreError, match="缺少依赖"):
        queue.submission.submit(2, [child.operation_id], False)
    accepted, _ = queue.submission.submit(
        2, [child.operation_id, producer.operation_id], False
    )
    assert accepted == [producer.operation_id, child.operation_id]


def test_link_primary_group_ignores_second_target_dependency(queue) -> None:
    target = enqueue_create(queue, "target")
    link = enqueue_link(queue, "PROJ-9", target.operation_id)

    state = public_state(queue)["queue"]
    pending = next(group for group in state["groups"] if group["kind"] == "pending_issue")
    assert [item["operation_id"] for item in pending["operations"]] == [
        target.operation_id
    ]
    assert [item["operation_id"] for item in state["standalone"]] == [
        link.operation_id
    ]
    assert state["standalone"][0]["dependency_states"] == {
        target.operation_id: "queued"
    }


def test_two_link_producers_complete_in_order_before_link_claim(queue) -> None:
    left = enqueue_create(queue, "left")
    right = enqueue_create(queue, "right")
    link = enqueue_link(queue, left.operation_id, right.operation_id)
    details = queue.query.get_queue_operations([link.operation_id])
    assert details.items[0].dependency_states == {
        left.operation_id: "queued", right.operation_id: "queued"
    }
    assert "dependency_operation_ids" not in details.items[0].model_dump()
    accepted, _ = queue.submission.submit(
        3, [link.operation_id, right.operation_id, left.operation_id], False
    )
    assert accepted == [left.operation_id, right.operation_id, link.operation_id]

    finish_create(queue, "PROJ-10")
    claim = queue.execution.claim_next()
    assert claim is not None and claim.operation.operation_id == right.operation_id
    queue.execution.complete_execution(
        claim.execution_id, "success", "created", None, "PROJ-11", 1
    )
    link_claim = queue.execution.claim_next()
    assert link_claim is not None
    assert link_claim.operation.issue_key == "PROJ-10"
    assert link_claim.operation.target_issue_key == "PROJ-11"


def test_same_producer_in_both_link_targets_is_bound_once(queue) -> None:
    producer = enqueue_create(queue)
    link = enqueue_link(queue, producer.operation_id, producer.operation_id)
    queue.submission.submit(2, [producer.operation_id, link.operation_id], False)
    finish_create(queue, "PROJ-20")

    staging = public_state(queue)["execution"]["staged"][0]
    assert staging["issue_key"] == "PROJ-20"
    assert staging["target_issue_key"] == "PROJ-20"
    connection = open_database("operations")
    try:
        events = connection.execute(
            """SELECT data_json FROM queue_events
               WHERE operation_id=? AND action_code='issue_key_resolved'""",
            (link.operation_id,),
        ).fetchall()
    finally:
        connection.close()
    assert len(events) == 1
    assert json.loads(events[0]["data_json"])["fields"] == [
        "issue_key",
        "target_issue_key",
    ]


def test_producer_broadcast_updates_queue_and_staging_once_without_reordering(queue) -> None:
    producer = enqueue_create(queue)
    staged_link = enqueue_link(queue, producer.operation_id, producer.operation_id)
    queue.submission.submit(
        2, [producer.operation_id, staged_link.operation_id], False
    )
    queued_link = enqueue_link(queue, producer.operation_id, producer.operation_id)
    producer_claim = queue.execution.claim_next()
    assert producer_claim is not None
    before = public_state(queue)
    stage_order = before["execution"]["staged"][0]["stage_order"]
    revisions = dict(before["revisions"])

    queue.execution.complete_execution(
        producer_claim.execution_id,
        "success",
        "created",
        None,
        "PROJ-30",
        1,
    )

    after = public_state(queue)
    assert after["revisions"]["queue"] == revisions["queue"] + 1
    assert after["revisions"]["execution"] == revisions["execution"] + 1
    queued = after["queue"]["standalone"][0]
    assert queued["operation_id"] == queued_link.operation_id
    assert queued["issue_key"] == queued["target_issue_key"] == "PROJ-30"
    staged = after["execution"]["staged"][0]
    assert staged["operation_id"] == staged_link.operation_id
    assert staged["stage_order"] == stage_order
    assert staged["issue_key"] == staged["target_issue_key"] == "PROJ-30"


def test_completed_producer_is_materialized_during_later_enqueue(queue) -> None:
    producer = enqueue_create(queue)
    queue.submission.submit(1, [producer.operation_id], False)
    finish_create(queue, "PROJ-40")

    child = enqueue_comment(queue, producer.operation_id)
    link = enqueue_link(queue, producer.operation_id, producer.operation_id)

    assert child.issue_key == "PROJ-40"
    assert link.issue_key == link.target_issue_key == "PROJ-40"
    state = public_state(queue)["queue"]
    all_items = [
        item
        for group in state["groups"]
        for item in group["operations"]
    ] + state["standalone"]
    assert all(not item["dependency_states"] for item in all_items)


def test_queue_reclassifies_automatically_after_producer_result(queue) -> None:
    producer = enqueue_create(queue)
    queue.submission.submit(1, [producer.operation_id], False)
    claim = queue.execution.claim_next()
    assert claim is not None
    child = enqueue_comment(queue, producer.operation_id)
    before_group = public_state(queue)["queue"]["groups"][0]
    assert before_group["kind"] == "pending_issue"
    assert before_group["group_key"] == producer.operation_id

    queue.execution.complete_execution(
        claim.execution_id, "success", "created", None, "PROJ-41", 1
    )

    after_group = public_state(queue)["queue"]["groups"][0]
    assert after_group["kind"] == "jira_issue"
    assert after_group["group_key"] == "PROJ-41"
    assert after_group["operations"][0]["operation_id"] == child.operation_id


def test_failed_producer_terminalizes_staged_dependants_for_either_key(queue) -> None:
    producer = enqueue_create(queue)
    primary = enqueue_comment(queue, producer.operation_id)
    target = enqueue_link(queue, "PROJ-1", producer.operation_id)
    queue.submission.submit(
        3,
        [producer.operation_id, primary.operation_id, target.operation_id],
        False,
    )
    staged = public_state(queue)["execution"]["staged"]
    for item in staged:
        assert "dependency_operation_ids" not in item
        assert item["dependency_states"] == (
            {} if item["operation_id"] == producer.operation_id
            else {producer.operation_id: "staged"}
        )
    claim = queue.execution.claim_next()
    assert claim is not None
    queue.execution.complete_execution(
        claim.execution_id, "failed", None, "failed", None, 1
    )

    assert not public_state(queue)["execution"]["staged"]
    history = queue.query.execution_history(None, None).model_dump(mode="json")
    by_id = {item["operation_id"]: item for item in history["items"]}
    assert by_id[producer.operation_id]["outcome"] == "failed"
    assert by_id[primary.operation_id]["outcome"] == "dependency_failed"
    assert by_id[target.operation_id]["outcome"] == "dependency_failed"
    for operation_id in (primary.operation_id, target.operation_id):
        assert by_id[operation_id]["dependency_states"] == {
            producer.operation_id: "failed"
        }
        assert "dependency_operation_ids" not in by_id[operation_id]


def test_public_dependency_mapping_preserves_unknown_references(queue) -> None:
    missing_id = "op-" + "e" * 32
    consumer = unresolved_consumer(missing_id)
    with transaction() as connection:
        queue_repository.insert_queue(
            connection, consumer, action_code="mcp_append",
            event_data={"source": "test"},
        )
        repository_metadata.bump_revisions(connection, "queue")

    detail = queue.query.get_queue_operations([consumer.operation_id]).items[0]
    group = queue.query.get_state().queue.groups[0]
    assert detail.dependency_states == {missing_id: "unknown"}
    assert detail.submittable is False
    assert group.operations == (detail,)
    assert group.producer_state == "unknown"
    assert "dependency_operation_ids" not in detail.model_dump()


def test_cancel_producer_cascades_through_primary_and_target_references(queue) -> None:
    producer = enqueue_create(queue)
    primary = enqueue_comment(queue, producer.operation_id)
    target = enqueue_link(queue, "PROJ-1", producer.operation_id)

    with pytest.raises(DependencyScopeIncomplete) as exc_info:
        queue.queue.cancel(3, [producer.operation_id])
    assert set(exc_info.value.affected_operation_ids) == {
        producer.operation_id,
        primary.operation_id,
        target.operation_id,
    }
    assert set(exc_info.value.missing_operation_ids) == {primary.operation_id, target.operation_id}
    cancelled, revision = queue.queue.cancel(3, exc_info.value.affected_operation_ids)
    assert set(cancelled) == set(exc_info.value.affected_operation_ids)
    assert revision == 4
    assert queue.query.total_active() == 0


def test_manual_success_binds_both_targets_before_held_resume(
    queue, monkeypatch
) -> None:
    blocker = enqueue_comment(queue, "PROJ-1", "blocker")
    producer = enqueue_create(queue)
    link = enqueue_link(queue, producer.operation_id, producer.operation_id)
    queue.submission.submit(
        3, [blocker.operation_id, producer.operation_id, link.operation_id], False
    )
    claim = queue.execution.claim_next()
    assert claim is not None and claim.operation.operation_id == blocker.operation_id
    assert queue.execution.recover_interrupted() == 1
    queue.recovery.resolve_indeterminate(
        blocker.operation_id, "succeeded", False
    )
    inspected_operation_ids: list[str] = []
    original_inspect = dependency_orchestration.inspect_dependencies

    def counted_inspect(operation: OperationRecord):
        inspected_operation_ids.append(operation.operation_id)
        return original_inspect(operation)

    monkeypatch.setattr(
        dependency_orchestration,
        "inspect_dependencies",
        counted_inspect,
    )
    queue.recovery.resolve_held(
        [producer.operation_id, link.operation_id], "resume"
    )
    assert Counter(inspected_operation_ids) == {
        producer.operation_id: 1,
        link.operation_id: 1,
    }
    monkeypatch.setattr(
        dependency_orchestration,
        "inspect_dependencies",
        original_inspect,
    )
    producer_claim = queue.execution.claim_next()
    assert producer_claim is not None
    assert queue.execution.recover_interrupted() == 1
    queue.recovery.resolve_indeterminate(
        producer.operation_id, "succeeded", False, "PROJ-50"
    )

    held = public_state(queue)["execution"]["held"][0]
    assert held["issue_key"] == held["target_issue_key"] == "PROJ-50"


def test_broadcast_failure_rolls_back_result_keys_events_and_revisions(queue) -> None:
    producer = enqueue_create(queue)
    link = enqueue_link(queue, producer.operation_id, producer.operation_id)
    queue.submission.submit(2, [producer.operation_id, link.operation_id], False)
    claim = queue.execution.claim_next()
    assert claim is not None
    before = public_state(queue)["revisions"]
    connection = open_database("operations")
    try:
        connection.execute(
            """CREATE TRIGGER fail_target_resolution
               BEFORE UPDATE OF target_issue_key ON execution_staging
               BEGIN SELECT RAISE(ABORT, 'forced resolution failure'); END"""
        )
    finally:
        connection.close()

    with pytest.raises(sqlite3.IntegrityError, match="forced resolution failure"):
        queue.execution.complete_execution(
            claim.execution_id, "success", "created", None, "PROJ-60", 1
        )

    after = public_state(queue)
    assert after["revisions"] == before
    assert after["execution"]["executing"][0]["operation_id"] == producer.operation_id
    assert after["execution"]["staged"][0]["issue_key"] == producer.operation_id
    assert after["execution"]["staged"][0]["target_issue_key"] == producer.operation_id


def test_successful_unmaterialized_queue_reference_is_consistency_blocker(
    queue,
) -> None:
    producer = enqueue_create(queue)
    queue.submission.submit(1, [producer.operation_id], False)
    finish_create(queue, "PROJ-70")
    consumer = unresolved_consumer(producer.operation_id)
    with transaction() as connection:
        queue_repository.insert_queue(
            connection,
            consumer,
            action_code="mcp_append",
            event_data={"source": "test"},
        )
        repository_metadata.bump_revisions(connection, "queue")

    state = queue.query.get_state()
    public_consumer = state.queue.groups[0].operations[0]
    assert public_consumer.submittable is False
    assert "数据不一致" in (public_consumer.submission_block_reason or "")
    with pytest.raises(StoreError, match="数据不一致"):
        queue.submission.submit(
            state.revisions["queue"], [consumer.operation_id], False
        )


def test_successful_unmaterialized_held_reference_cannot_resume(queue) -> None:
    producer = enqueue_create(queue)
    queue.submission.submit(1, [producer.operation_id], False)
    finish_create(queue, "PROJ-71")
    consumer = unresolved_consumer(producer.operation_id)
    with transaction() as connection:
        execution_repository.insert_staging(
            connection,
            consumer,
            stage_order=1,
            submission_risk_level="low",
            submitted_at=10,
        )
        execution_repository.set_staging_state(
            connection,
            consumer.operation_id,
            state="held",
            held_reason="test_invariant",
        )
        repository_metadata.bump_revisions(connection, "execution")

    with pytest.raises(StoreError, match="数据不一致"):
        queue.recovery.resolve_held([consumer.operation_id], "resume")


def test_successful_unmaterialized_staged_reference_fails_without_jira(
    queue, monkeypatch
) -> None:
    producer = enqueue_create(queue)
    queue.submission.submit(1, [producer.operation_id], False)
    finish_create(queue, "PROJ-72")
    consumer = unresolved_consumer(producer.operation_id)
    with transaction() as connection:
        execution_repository.insert_staging(
            connection,
            consumer,
            stage_order=1,
            submission_risk_level="low",
            submitted_at=10,
        )
        repository_metadata.bump_revisions(connection, "execution")
    jira_calls: list[str] = []

    def should_not_execute(operation: OperationRecord) -> OperationExecutionResult:
        jira_calls.append(operation.operation_id)
        return OperationExecutionResult(summary="unexpected")

    monkeypatch.setattr(
        execution_worker.jira_executor,
        "execute_operation",
        should_not_execute,
    )

    assert execution_worker.process_once() is True
    assert jira_calls == []
    history = queue.query.execution_history(None, None).items
    consumer_history = next(
        item for item in history if item.operation_id == consumer.operation_id
    )
    assert consumer_history.outcome == "failed"
    assert "数据不一致" in (consumer_history.error or "")
