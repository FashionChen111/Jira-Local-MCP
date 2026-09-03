"""Strict immutable direct-key operation model contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.operations.dependencies import (
    MaterializableDependency,
    classify_dependency_state,
    dependency_resolution_from_result,
)
from core.operations.models import (
    AttachmentDependencyResolution,
    AttachmentResult,
    DependencyState,
    IssueDependencyResolution,
    OperationDraft,
    OperationExecutionResult,
    OperationRecord,
    StagingRecord,
)


def operation(**changes: object) -> OperationRecord:
    values: dict[str, object] = {
        "operation_id": "op-" + "a" * 32,
        "operation_type": "add_issuelink",
        "issue_key": "PROJ-1",
        "target_issue_key": "PROJ-2",
        "risk_level": "low",
        "payload": {"link_type": "Relates"},
        "description": "link",
        "source": "test",
        "created_at": 1,
        "attempt_no": 1,
    }
    values.update(changes)
    return OperationRecord.model_validate(values)


def test_operation_models_are_strict_frozen_and_forbid_extra_fields() -> None:
    value = operation()

    with pytest.raises(ValidationError):
        value.issue_key = "PROJ-9"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        operation(extra="forbidden")
    with pytest.raises(ValidationError):
        operation(attempt_no="1")
    with pytest.raises(ValidationError):
        operation(operation_type="unknown")


@pytest.mark.parametrize(
    "payload",
    [
        {"value": object()},
        {"value": float("nan")},
        {"value": ("tuple",)},
        {1: "non-string key"},
    ],
)
def test_payload_rejects_non_json_values(payload: object) -> None:
    with pytest.raises(ValidationError):
        operation(payload=payload)


def test_model_dump_is_the_single_public_json_serialization_path() -> None:
    value = operation(payload={"nested": [False, 0, "", None]})

    assert value.model_dump(mode="json") == {
        "operation_id": "op-" + "a" * 32,
        "operation_type": "add_issuelink",
        "issue_key": "PROJ-1",
        "target_issue_key": "PROJ-2",
        "risk_level": "low",
        "payload": {"nested": [False, 0, "", None]},
        "description": "link",
        "source": "test",
        "created_at": 1,
        "attempt_no": 1,
    }


def test_draft_forbids_payload_target_duplicates_at_safety_boundary() -> None:
    draft = OperationDraft(
        operation_type="add_comment",
        issue_key="PROJ-1",
        payload={"body": "hello"},
        description="comment",
    )

    assert draft.target_issue_key is None


def test_staging_model_rejects_unknown_persisted_state() -> None:
    with pytest.raises(ValidationError):
        StagingRecord.model_validate(
            {
                **operation(target_issue_key=None, operation_type="add_comment").model_dump(),
                "stage_order": 1,
                "state": "waiting",
                "submission_risk_level": "low",
                "execution_id": None,
                "submitted_at": 1,
                "started_at": None,
                "held_reason": None,
                "blocked_by_execution_id": None,
            }
        )


def test_attachment_result_is_one_strict_frozen_identity_value() -> None:
    attachment = AttachmentResult(id="10001", filename="a-1.jpg")
    result = OperationExecutionResult(
        summary="uploaded", attachment_result=attachment
    )

    assert result.model_dump(mode="json")["attachment_result"] == {
        "id": "10001",
        "filename": "a-1.jpg",
    }
    with pytest.raises(ValidationError):
        AttachmentResult(id="", filename="a.jpg")
    with pytest.raises(ValidationError):
        attachment.filename = "changed.jpg"  # type: ignore[misc]


def test_dependency_state_separates_operation_context_from_results() -> None:
    state = DependencyState(
        operation_id="op-" + "a" * 32,
        operation_type="add_attachment",
        state="queued",
        operation_issue_key="PROJ-1",
    )

    assert state.operation_issue_key == "PROJ-1"
    assert state.new_issue_key is None
    assert state.attachment_result is None
    with pytest.raises(ValidationError):
        DependencyState.model_validate(
            {
                "operation_id": "op-" + "a" * 32,
                "operation_type": "add_attachment",
                "state": "queued",
                "issue_key": "PROJ-1",
            }
        )


def test_dependency_results_project_to_a_discriminated_resolution() -> None:
    issue = dependency_resolution_from_result(
        producer_operation_id="op-" + "a" * 32,
        operation_type="create_issue",
        successful=True,
        new_issue_key="PROJ-10",
        attachment_result=None,
    )
    attachment = dependency_resolution_from_result(
        producer_operation_id="op-" + "b" * 32,
        operation_type="add_attachment",
        successful=True,
        new_issue_key=None,
        attachment_result=AttachmentResult(id="10", filename="a.jpg"),
    )

    assert isinstance(issue, IssueDependencyResolution)
    assert issue.kind == "issue" and issue.issue_key == "PROJ-10"
    assert isinstance(attachment, AttachmentDependencyResolution)
    assert attachment.kind == "attachment"

    decision = classify_dependency_state(
        DependencyState(
            operation_id=issue.producer_operation_id,
            operation_type="create_issue",
            state="success",
            new_issue_key=issue.issue_key,
        )
    )
    assert isinstance(decision, MaterializableDependency)
    assert decision.resolution == issue
