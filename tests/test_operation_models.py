"""Strict immutable direct-key operation model contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.operations.models import OperationDraft, OperationRecord, StagingRecord


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
