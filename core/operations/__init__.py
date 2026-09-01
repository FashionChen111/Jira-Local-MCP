"""Operations domain, application services, and stable adapter-facing errors."""

from core.operations.container import OperationServices, get_services, reset_services
from core.operations.errors import (
    ConfirmationRequired,
    DependencyConfirmationRequired,
    ExecutionBlocked,
    RevisionConflict,
    StoreError,
)
from core.operations.models import (
    AttachmentResult,
    ExecutionClaim,
    OperationExecutionResult,
    OperationRecord,
)
from core.operations.rules.identifiers import is_jira_issue_key, is_operation_id
from core.operations.rules.safety import assert_safe_to_execute

__all__ = [
    "ConfirmationRequired",
    "AttachmentResult",
    "DependencyConfirmationRequired",
    "ExecutionBlocked",
    "ExecutionClaim",
    "OperationRecord",
    "OperationExecutionResult",
    "OperationServices",
    "RevisionConflict",
    "StoreError",
    "assert_safe_to_execute",
    "get_services",
    "is_jira_issue_key",
    "is_operation_id",
    "reset_services",
]
