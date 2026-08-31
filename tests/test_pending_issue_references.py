"""Direct primary/target producer references across queue and execution states."""

from __future__ import annotations

import json
import sqlite3

import pytest

from core.operations import DependencyConfirmationRequired, StoreError
from core.storage.database import open_database


def public_state(services):
    return services.query.get_state().model_dump(mode="json")


def enqueue_create(services, summary: str = "new"):
    return services.queue.enqueue(
        "create_issue",
        None,
        {"project_key": "PROJ", "issue_type": "Task", "summary": summary},
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
    assert group["operations"][1]["dependency_operation_ids"] == [
        producer.operation_id
    ]

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
    assert state["standalone"][0]["dependency_operation_ids"] == [
        target.operation_id
    ]


def test_two_link_producers_complete_in_order_before_link_claim(queue) -> None:
    left = enqueue_create(queue, "left")
    right = enqueue_create(queue, "right")
    link = enqueue_link(queue, left.operation_id, right.operation_id)
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
    assert all(not item["dependency_operation_ids"] for item in all_items)


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


def test_cancel_producer_cascades_through_primary_and_target_references(queue) -> None:
    producer = enqueue_create(queue)
    primary = enqueue_comment(queue, producer.operation_id)
    target = enqueue_link(queue, "PROJ-1", producer.operation_id)

    with pytest.raises(DependencyConfirmationRequired) as exc_info:
        queue.queue.cancel(3, [producer.operation_id], False)
    assert set(exc_info.value.operation_ids) == {
        producer.operation_id,
        primary.operation_id,
        target.operation_id,
    }
    cancelled, revision = queue.queue.cancel(3, [producer.operation_id], True)
    assert set(cancelled) == set(exc_info.value.operation_ids)
    assert revision == 4
    assert queue.query.total_active() == 0


def test_manual_success_binds_both_targets_before_held_resume(queue) -> None:
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
    queue.recovery.resolve_held(
        [producer.operation_id, link.operation_id], "resume"
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
