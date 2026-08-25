"""Pending create/clone references, binding, and dependency outcomes."""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from core import risk_engine
from core.operations import (
    DependencyConfirmationRequired,
    RevisionConflict,
    StoreError,
)
from core.storage.database import open_database


def _create(queue):
    return queue.enqueue(
        "create_issue",
        None,
        {"project_key": "PROJ", "issue_type": "Task", "summary": "new"},
        "create new",
        "test",
    )


def _comment(queue, target, body="body"):
    return queue.enqueue(
        "add_comment",
        target,
        {"issue_key": target, "body": body},
        f"comment {target}",
        "test",
    )


def test_pending_reference_forms_a_planned_group_and_requires_closure(queue):
    create = _create(queue)
    comment = _comment(queue, create.operation_id)

    planned = queue.get_state()["queue"]["planned_groups"][0]
    assert planned["producer_operation_id"] == create.operation_id
    assert [item["operation_id"] for item in planned["operations"]] == [
        create.operation_id,
        comment.operation_id,
    ]
    assert planned["operations"][1]["payload"]["issue_key"] == create.operation_id

    with pytest.raises(StoreError, match="缺少依赖"):
        queue.submit(2, [comment.operation_id], False)
    accepted, _ = queue.submit(
        2, [comment.operation_id, create.operation_id], False
    )
    assert accepted == [create.operation_id, comment.operation_id]


def test_success_resolves_staged_and_queued_dependants(queue):
    create = _create(queue)
    staged_comment = _comment(queue, create.operation_id, "staged")
    queued_comment = _comment(queue, create.operation_id, "queued")
    queue.submit(3, [create.operation_id, staged_comment.operation_id], False)

    claim = queue.claim_next()
    assert claim and claim.operation.operation_id == create.operation_id
    queue.complete_execution(
        claim.execution_id, "success", "created", None, "PROJ-9", 1
    )

    state = queue.get_state()
    assert state["execution"]["staged"][0]["payload"]["issue_key"] == "PROJ-9"
    group = state["queue"]["groups"][0]
    assert group["issue_key"] == "PROJ-9"
    assert group["operations"][0]["payload"]["issue_key"] == "PROJ-9"
    assert not state["queue"]["planned_groups"]

    later = _comment(queue, create.operation_id, "later")
    later_public = next(
        item
        for item in queue.get_state()["queue"]["groups"][0]["operations"]
        if item["operation_id"] == later.operation_id
    )
    assert later_public["payload"]["issue_key"] == "PROJ-9"
    assert later_public["issue_references"]["issue_key"][
        "resolved_issue_key"
    ] == "PROJ-9"

    dependent_claim = queue.claim_next()
    assert dependent_claim
    assert dependent_claim.operation.issue_key == "PROJ-9"
    assert dependent_claim.operation.payload["issue_key"] == "PROJ-9"


def test_raw_payload_is_immutable_and_history_records_concrete_call(queue):
    create = _create(queue)
    staged_child = _comment(queue, create.operation_id, "staged")
    queued_child = _comment(queue, create.operation_id, "queued")
    queue.submit(3, [create.operation_id, staged_child.operation_id], False)
    producer_claim = queue.claim_next()
    assert producer_claim
    queue.complete_execution(
        producer_claim.execution_id, "success", "created", None, "PROJ-40", 1
    )

    connection = open_database("operations")
    staged_row = connection.execute(
        "SELECT payload_json,issue_references_json FROM execution_staging "
        "WHERE operation_id=?",
        (staged_child.operation_id,),
    ).fetchone()
    queued_row = connection.execute(
        "SELECT payload_json,issue_references_json FROM queue_items WHERE operation_id=?",
        (queued_child.operation_id,),
    ).fetchone()
    connection.close()
    for row in (staged_row, queued_row):
        assert json.loads(row["payload_json"])["issue_key"] == create.operation_id
        assert json.loads(row["issue_references_json"])["issue_key"][
            "resolved_issue_key"
        ] == "PROJ-40"

    child_claim = queue.claim_next()
    assert child_claim and child_claim.operation.issue_key == "PROJ-40"
    queue.complete_execution(
        child_claim.execution_id, "success", "commented", None, None, 1
    )
    connection = open_database("operations")
    history_row = connection.execute(
        "SELECT payload_json,executed_issue_key,executed_payload_json "
        "FROM execution_records WHERE operation_id=?",
        (staged_child.operation_id,),
    ).fetchone()
    connection.close()
    assert json.loads(history_row["payload_json"])["issue_key"] == create.operation_id
    assert history_row["executed_issue_key"] == "PROJ-40"
    assert json.loads(history_row["executed_payload_json"])["issue_key"] == "PROJ-40"

    public = next(
        item
        for item in queue.read_execution_log(10)
        if item["operation_id"] == staged_child.operation_id
    )
    assert public["payload"]["issue_key"] == "PROJ-40"
    assert public["reviewed_payload"]["issue_key"] == create.operation_id


def test_failed_producer_terminalizes_staged_dependants_without_jira(queue):
    create = _create(queue)
    staged_comment = _comment(queue, create.operation_id, "staged")
    queued_comment = _comment(queue, create.operation_id, "queued")
    queue.submit(3, [create.operation_id, staged_comment.operation_id], False)

    claim = queue.claim_next()
    assert claim
    queue.complete_execution(
        claim.execution_id, "failed", None, "jira failed", None, 1
    )

    outcomes = {
        item["operation_id"]: item["outcome"]
        for item in queue.read_execution_log(10)
    }
    assert outcomes[create.operation_id] == "failed"
    assert outcomes[staged_comment.operation_id] == "dependency_failed"
    queued = queue.get_state()["queue"]["planned_groups"][0]["operations"][0]
    assert queued["operation_id"] == queued_comment.operation_id
    assert queued["submittable"] is False


def test_link_may_reference_two_pending_producers(queue):
    left = _create(queue)
    right = _create(queue)
    link = queue.enqueue(
        "add_issuelink",
        left.operation_id,
        {
            "issue_key": left.operation_id,
            "target_issue_key": right.operation_id,
            "link_type": "Relates",
        },
        "link pending",
        "test",
    )
    with pytest.raises(StoreError, match="缺少依赖"):
        queue.submit(3, [left.operation_id, link.operation_id], False)
    accepted, _ = queue.submit(
        3, [link.operation_id, right.operation_id, left.operation_id], False
    )
    assert accepted[-1] == link.operation_id


def test_clone_operation_is_a_producer_for_followups(queue):
    clone = queue.enqueue(
        "clone_issue",
        "PROJ-1",
        {"issue_key": "PROJ-1", "summary": "copy"},
        "clone",
        "test",
    )
    child = _comment(queue, clone.operation_id)
    queue.submit(2, [clone.operation_id, child.operation_id], False)
    claim = queue.claim_next()
    assert claim and claim.operation.operation_id == clone.operation_id
    queue.complete_execution(
        claim.execution_id, "success", "cloned", None, "PROJ-2", 1
    )
    assert queue.get_state()["execution"]["staged"][0]["payload"][
        "issue_key"
    ] == "PROJ-2"


def test_indeterminate_producer_success_requires_and_binds_real_key(queue):
    create = _create(queue)
    comment = _comment(queue, create.operation_id)
    queue.submit(2, [create.operation_id, comment.operation_id], False)
    queue.claim_next()
    queue.recover_interrupted()

    with pytest.raises(StoreError, match="必须填写"):
        queue.resolve_indeterminate(create.operation_id, "succeeded", False)
    queue.resolve_indeterminate(
        create.operation_id, "succeeded", False, "PROJ-11"
    )
    held = queue.get_state()["execution"]["held"][0]
    assert held["payload"]["issue_key"] == "PROJ-11"


def test_abandoned_indeterminate_producer_terminalizes_held_dependants(queue):
    create = _create(queue)
    child = _comment(queue, create.operation_id)
    queue.submit(2, [create.operation_id, child.operation_id], False)
    queue.claim_next()
    queue.recover_interrupted()
    queue.resolve_indeterminate(create.operation_id, "abandoned", False)
    assert not queue.get_state()["execution"]["held"]
    record = next(
        item
        for item in queue.read_execution_log(10)
        if item["operation_id"] == child.operation_id
    )
    assert record["outcome"] == "dependency_failed"


def test_not_applied_producer_requires_held_dependant_to_requeue(queue):
    create = _create(queue)
    child = _comment(queue, create.operation_id)
    queue.submit(2, [create.operation_id, child.operation_id], False)
    queue.claim_next()
    queue.recover_interrupted()
    queue.resolve_indeterminate(create.operation_id, "not_applied", False)

    with pytest.raises(StoreError, match="重新入队"):
        queue.resolve_held([child.operation_id], "resume")
    queue.resolve_held([child.operation_id], "requeue")

    planned = queue.get_state()["queue"]["planned_groups"][0]
    assert [item["operation_id"] for item in planned["operations"]] == [
        create.operation_id,
        child.operation_id,
    ]
    assert planned["operations"][1]["unresolved_dependency_operation_ids"] == [
        create.operation_id
    ]


def test_held_producer_and_dependant_can_resume_together(queue):
    blocker = _comment(queue, "PROJ-1", "blocker")
    create = _create(queue)
    child = _comment(queue, create.operation_id)
    queue.submit(
        3, [blocker.operation_id, create.operation_id, child.operation_id], False
    )
    claim = queue.claim_next()
    assert claim and claim.operation.operation_id == blocker.operation_id
    queue.recover_interrupted()
    queue.resolve_indeterminate(blocker.operation_id, "succeeded", False)

    queue.resolve_held([create.operation_id, child.operation_id], "resume")
    resumed = queue.get_state()["execution"]["staged"]
    assert [item["operation_id"] for item in resumed] == [
        create.operation_id,
        child.operation_id,
    ]
    producer_claim = queue.claim_next()
    assert producer_claim and producer_claim.operation.operation_id == create.operation_id


@pytest.mark.parametrize(
    "operation_type,payload",
    [
        ("move_issue", {"target_project_key": "NEXT"}),
        ("change_type", {"target_issue_type": "Bug"}),
        ("transition_issue", {"transition_name": "Start"}),
        ("update_field", {"fields": {"summary": "x"}}),
        ("update_custom_field", {"custom_fields": {"customfield_1": "x"}}),
        ("add_attachment", {"file_path": "C:\\review.txt"}),
        ("add_comment", {"body": "x"}),
        ("add_worklog", {"time_spent": "1h"}),
        ("add_issuelink", {"target_issue_key": "PROJ-2", "link_type": "Relates"}),
    ],
)
def test_all_modify_operations_accept_pending_primary_target(
    queue, operation_type, payload
):
    create = _create(queue)
    value = {"issue_key": create.operation_id, **payload}
    operation = queue.enqueue(
        operation_type,
        create.operation_id,
        value,
        operation_type,
        "test",
    )
    assert operation.issue_references["issue_key"]["operation_id"] == create.operation_id


def test_unknown_nonproducer_failed_and_clone_source_references_are_rejected(queue):
    unknown = "op-" + "f" * 32
    with pytest.raises(risk_engine.RiskError, match="不存在"):
        _comment(queue, unknown)

    ordinary = _comment(queue, "PROJ-1")
    with pytest.raises(risk_engine.RiskError, match="不会创建"):
        _comment(queue, ordinary.operation_id)

    create = _create(queue)
    with pytest.raises(risk_engine.RiskError, match="来源事务必须"):
        queue.enqueue(
            "clone_issue",
            create.operation_id,
            {"issue_key": create.operation_id},
            "clone pending",
            "test",
        )

    queue.submit(2, [create.operation_id], False)
    claim = queue.claim_next()
    assert claim
    queue.complete_execution(claim.execution_id, "failed", None, "failed", None, 1)
    with pytest.raises(risk_engine.RiskError, match="已失败"):
        _comment(queue, create.operation_id)


def test_cancel_producer_requires_confirmed_cascade_but_child_can_cancel_alone(queue):
    create = _create(queue)
    child = _comment(queue, create.operation_id)
    with pytest.raises(DependencyConfirmationRequired) as required:
        queue.cancel(2, [create.operation_id], False)
    assert set(required.value.operation_ids) == {
        create.operation_id,
        child.operation_id,
    }
    cancelled, _revision = queue.cancel(2, [create.operation_id], True)
    assert set(cancelled) == {create.operation_id, child.operation_id}

    create = _create(queue)
    child = _comment(queue, create.operation_id)
    queue.cancel(5, [child.operation_id], False)
    remaining = queue.get_state()["queue"]["planned_groups"][0]["operations"]
    assert [item["operation_id"] for item in remaining] == [create.operation_id]


def test_reference_binding_rolls_back_with_result_transaction(queue):
    create = _create(queue)
    child = _comment(queue, create.operation_id)
    queue.submit(2, [create.operation_id, child.operation_id], False)
    claim = queue.claim_next()
    assert claim
    connection = open_database("operations")
    connection.execute(
        """CREATE TRIGGER fail_reference_binding
           BEFORE UPDATE OF issue_references_json ON execution_staging
           BEGIN SELECT RAISE(ABORT,'fail'); END"""
    )
    connection.close()

    with pytest.raises(sqlite3.IntegrityError):
        queue.complete_execution(
            claim.execution_id, "success", "created", None, "PROJ-20", 1
        )
    state = queue.get_state()
    assert state["execution"]["executing"][0]["operation_id"] == create.operation_id
    assert state["execution"]["staged"][0]["payload"]["issue_key"] == create.operation_id
    assert not queue.read_execution_log(10)


def test_completion_race_with_enqueue_always_exposes_concrete_key(queue):
    create = _create(queue)
    queue.submit(1, [create.operation_id], False)
    claim = queue.claim_next()
    assert claim
    start = threading.Event()
    errors: list[Exception] = []

    def complete():
        start.wait()
        try:
            queue.complete_execution(
                claim.execution_id, "success", "created", None, "PROJ-30", 1
            )
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    def enqueue():
        start.wait()
        try:
            _comment(queue, create.operation_id, "raced")
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    threads = [threading.Thread(target=complete), threading.Thread(target=enqueue)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
    assert not errors and all(not thread.is_alive() for thread in threads)
    operation = queue.get_state()["queue"]["groups"][0]["operations"][0]
    assert operation["payload"]["issue_key"] == "PROJ-30"


def test_completion_race_with_submit_never_leaves_unresolved_staging(queue):
    create = _create(queue)
    child = _comment(queue, create.operation_id)
    queue.submit(2, [create.operation_id], False)
    claim = queue.claim_next()
    assert claim
    start = threading.Event()
    errors: list[Exception] = []
    submit_outcome: list[str] = []

    def complete():
        start.wait()
        try:
            queue.complete_execution(
                claim.execution_id, "success", "created", None, "PROJ-31", 1
            )
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    def submit():
        start.wait()
        try:
            queue.submit(3, [child.operation_id], False)
            submit_outcome.append("submitted")
        except RevisionConflict:
            submit_outcome.append("revision_conflict")
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    threads = [threading.Thread(target=complete), threading.Thread(target=submit)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
    assert not errors and submit_outcome and all(
        not thread.is_alive() for thread in threads
    )

    state = queue.get_state()
    candidates = [
        *state["execution"]["staged"],
        *[
            item
            for group in state["queue"]["groups"]
            for item in group["operations"]
        ],
    ]
    visible_child = next(
        item for item in candidates if item["operation_id"] == child.operation_id
    )
    assert visible_child["payload"]["issue_key"] == "PROJ-31"
    assert not visible_child["unresolved_dependency_operation_ids"]
