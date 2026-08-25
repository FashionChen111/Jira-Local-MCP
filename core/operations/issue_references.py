"""Validation and lifecycle lookup for pending Jira issue references."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
OPERATION_ID_RE = re.compile(r"^op-[0-9a-f]{32}$")
PRODUCER_OPS = {"create_issue", "clone_issue"}
REFERENCE_FIELDS_BY_OPERATION: dict[str, tuple[str, ...]] = {
    "move_issue": ("issue_key",),
    "change_type": ("issue_key",),
    "transition_issue": ("issue_key",),
    "update_field": ("issue_key",),
    "update_custom_field": ("issue_key",),
    "add_attachment": ("issue_key",),
    "add_comment": ("issue_key",),
    "add_worklog": ("issue_key",),
    "add_issuelink": ("issue_key", "target_issue_key"),
}


class IssueReferenceError(ValueError):
    """A supplied Jira key or producer-operation reference is invalid."""


@dataclass(frozen=True)
class ProducerState:
    operation_id: str
    operation_type: str | None
    state: str
    issue_key: str | None = None

    @property
    def active(self) -> bool:
        return self.state in {
            "queued",
            "staged",
            "executing",
            "indeterminate",
            "held",
            "retry_pending",
        }

    @property
    def terminal_failure(self) -> bool:
        return self.state in {"failed", "abandoned", "dependency_failed"}


def is_issue_key(value: object) -> bool:
    return isinstance(value, str) and bool(ISSUE_KEY_RE.fullmatch(value))


def is_operation_id(value: object) -> bool:
    return isinstance(value, str) and bool(OPERATION_ID_RE.fullmatch(value))


def reference_fields(operation_type: str) -> tuple[str, ...]:
    return REFERENCE_FIELDS_BY_OPERATION.get(operation_type, ())


def producer_state(
    connection: sqlite3.Connection, operation_id: str
) -> ProducerState:
    row = connection.execute(
        "SELECT operation_type FROM queue_items WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if row is not None:
        return ProducerState(operation_id, row["operation_type"], "queued")

    row = connection.execute(
        "SELECT operation_type,state FROM execution_staging WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if row is not None:
        return ProducerState(operation_id, row["operation_type"], row["state"])

    row = connection.execute(
        """SELECT operation_type,outcome,resolution,new_issue_key
           FROM execution_records WHERE operation_id=?
           ORDER BY attempt_no DESC,started_at DESC,execution_id DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if row is None:
        return ProducerState(operation_id, None, "unknown")
    if row["outcome"] == "success":
        return ProducerState(
            operation_id, row["operation_type"], "success", row["new_issue_key"]
        )
    if row["outcome"] == "indeterminate":
        if row["resolution"] == "succeeded":
            return ProducerState(
                operation_id,
                row["operation_type"],
                "success",
                row["new_issue_key"],
            )
        if row["resolution"] == "not_applied":
            return ProducerState(operation_id, row["operation_type"], "retry_pending")
        if row["resolution"] == "abandoned":
            return ProducerState(operation_id, row["operation_type"], "abandoned")
        return ProducerState(operation_id, row["operation_type"], "indeterminate")
    return ProducerState(
        operation_id, row["operation_type"], row["outcome"] or "executing"
    )


def build_issue_references(
    connection: sqlite3.Connection,
    operation_type: str,
    payload: dict[str, Any],
) -> dict[str, dict[str, str | None]]:
    """Validate target fields and capture producer references with known results."""
    if operation_type == "clone_issue":
        value = payload.get("issue_key")
        if not is_issue_key(value):
            raise IssueReferenceError(
                "clone_issue 的来源事务必须使用真实 Jira issue key"
            )
        return {}

    references: dict[str, dict[str, str | None]] = {}
    for field_name in reference_fields(operation_type):
        value = payload.get(field_name)
        if is_issue_key(value):
            continue
        if not is_operation_id(value):
            raise IssueReferenceError(
                f"{field_name} 必须是 Jira issue key 或 create/clone operation ID"
            )
        state = producer_state(connection, value)
        if state.operation_type not in PRODUCER_OPS:
            if state.operation_type is None:
                raise IssueReferenceError(f"引用的 operation 不存在: {value}")
            raise IssueReferenceError(
                f"引用的 operation 不会创建 Jira 事务: {value}"
            )
        if state.terminal_failure:
            raise IssueReferenceError(
                f"引用的创建/克隆 operation 已失败或放弃: {value}"
            )
        if state.state == "success" and not is_issue_key(state.issue_key):
            raise IssueReferenceError(
                f"引用的创建/克隆 operation 缺少合法的新事务 Key: {value}"
            )
        references[field_name] = {
            "operation_id": value,
            "resolved_issue_key": state.issue_key if state.state == "success" else None,
        }
    return references


def resolved_reference_copy(
    references: dict[str, dict[str, str | None]],
    producer_operation_id: str,
    issue_key: str,
) -> tuple[dict[str, dict[str, str | None]], list[str]]:
    """Return updated provenance and the payload fields resolved by one producer."""
    updated = {
        field_name: dict(reference)
        for field_name, reference in references.items()
    }
    changed: list[str] = []
    for field_name, reference in updated.items():
        if (
            reference.get("operation_id") == producer_operation_id
            and not reference.get("resolved_issue_key")
        ):
            reference["resolved_issue_key"] = issue_key
            changed.append(field_name)
    return updated, changed


def first_unresolved_producer(
    operation_type: str,
    references: dict[str, dict[str, str | None]],
) -> str | None:
    for field_name in reference_fields(operation_type):
        reference = references.get(field_name)
        if reference and not reference.get("resolved_issue_key"):
            return str(reference["operation_id"])
    return None
