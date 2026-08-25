"""Typed persisted operation-attempt and execution-claim models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    operation_type: str
    issue_key: str | None
    risk_level: str
    payload: dict[str, Any]
    description: str
    source: str
    created_at: int
    issue_references: dict[str, dict[str, str | None]] = field(default_factory=dict)
    attempt_no: int = 1

    def effective_payload(self) -> dict[str, Any]:
        """Return the reviewed payload with resolved issue references overlaid."""
        payload = dict(self.payload)
        for field_name, reference in self.issue_references.items():
            resolved = reference.get("resolved_issue_key")
            if resolved:
                payload[field_name] = resolved
        return payload

    def effective_issue_key(self) -> str | None:
        reference = self.issue_references.get("issue_key")
        if reference and reference.get("resolved_issue_key"):
            return reference["resolved_issue_key"]
        return self.issue_key

    def unresolved_dependency_ids(self) -> list[str]:
        return sorted(
            {
                str(reference["operation_id"])
                for reference in self.issue_references.values()
                if not reference.get("resolved_issue_key")
            }
        )

    def concrete_copy(self) -> "OperationRecord":
        """Build the concrete snapshot passed to Jira after all refs resolve."""
        if self.unresolved_dependency_ids():
            raise ValueError("operation 仍含未解析的待创建事务引用")
        return replace(
            self,
            issue_key=self.effective_issue_key(),
            payload=self.effective_payload(),
        )

    def to_public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["issue_key"] = self.effective_issue_key()
        data["payload"] = self.effective_payload()
        data["unresolved_dependency_operation_ids"] = (
            self.unresolved_dependency_ids()
        )
        return data


@dataclass(frozen=True)
class ExecutionClaim:
    """One atomically claimed execution and its persisted operation snapshot."""

    execution_id: str
    operation: OperationRecord
    submission_risk_level: str
    stage_order: int
    submitted_at: int
    started_at: int
