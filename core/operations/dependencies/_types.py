"""Internal immutable values shared by dependency rule modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.operations.models import (
    AttachmentResult,
    DependencyResolution,
)


@dataclass(frozen=True)
class IssueReference:
    """One explicit Jira issue target backed by another operation."""

    operation_id: str
    path: Literal["issue_key", "target_issue_key"]


@dataclass(frozen=True)
class AttachmentReference:
    """One attachment producer reference embedded in a Jira text field."""

    operation_id: str
    path: str
    render_kind: Literal["link", "image"]


@dataclass(frozen=True)
class AttachmentReferenceInspection:
    """One scan result containing references and syntax diagnostics."""

    references: tuple[AttachmentReference, ...]
    invalid_placeholder_paths: tuple[str, ...]
    jira_attachment_markup_paths: tuple[str, ...]


@dataclass(frozen=True)
class DependencyReferences:
    """All dependency references discovered by one operation scan."""

    issues: tuple[IssueReference, ...]
    attachments: AttachmentReferenceInspection


@dataclass(frozen=True)
class DependencyResolutions:
    """Already available results for references owned by one consumer."""

    issue_keys: dict[str, str]
    attachments: dict[str, AttachmentResult]


@dataclass(frozen=True)
class DependencyResolutionEvent:
    """One enqueue-time audit event for an immediately materialized result."""

    action_code: str
    producer_operation_id: str
    data: dict[str, object]


@dataclass(frozen=True)
class MaterializableDependency:
    """A completed dependency with one validated typed result."""

    resolution: DependencyResolution


@dataclass(frozen=True)
class WaitingDependency:
    """A valid dependency whose producer may still complete."""


@dataclass(frozen=True)
class UnavailableDependency:
    """A missing, invalid, or terminal dependency that cannot materialize."""


DependencyStateDecision = (
    MaterializableDependency | WaitingDependency | UnavailableDependency
)
