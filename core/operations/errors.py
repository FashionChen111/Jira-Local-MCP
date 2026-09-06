"""Expected operation-store state and validation failures."""


class StoreError(RuntimeError):
    """Expected state or validation failure."""


class DependencyError(ValueError):
    """An issue or attachment dependency violates the operations contract."""


class RevisionConflict(StoreError):
    """The submitted queue revision no longer matches persisted state."""


class ConfirmationRequired(StoreError):
    """The requested action requires an additional human confirmation."""


class DependencyScopeIncomplete(StoreError):
    """The explicit cancellation scope omits dependent queued operations."""

    def __init__(
        self, message: str, affected_operation_ids: list[str],
        missing_operation_ids: list[str],
    ) -> None:
        super().__init__(message)
        self.affected_operation_ids = affected_operation_ids
        self.missing_operation_ids = missing_operation_ids


class ExecutionBlocked(StoreError):
    """Unresolved execution state prevents new work from being submitted."""


def store_error_data(exc: StoreError) -> dict[str, object]:
    """Describe an expected business conflict consistently for Web and MCP."""
    extra: dict[str, object] = {}
    if isinstance(exc, DependencyScopeIncomplete):
        code = "dependency_scope_incomplete"
        extra["affected_operation_ids"] = exc.affected_operation_ids
        extra["missing_operation_ids"] = exc.missing_operation_ids
    elif isinstance(exc, ConfirmationRequired):
        code = "confirmation_required"
    elif isinstance(exc, RevisionConflict):
        code = "revision_conflict"
    elif isinstance(exc, ExecutionBlocked):
        code = "execution_blocked"
    else:
        code = "state_conflict"
    return {"error": str(exc), "code": code, **extra}
