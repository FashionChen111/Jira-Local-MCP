"""Expected operation-store state and validation failures."""


class StoreError(RuntimeError):
    """Expected state or validation failure."""


class RevisionConflict(StoreError):
    """The submitted queue revision no longer matches persisted state."""


class ConfirmationRequired(StoreError):
    """The requested action requires an additional human confirmation."""


class DependencyConfirmationRequired(ConfirmationRequired):
    """Cancelling a producer requires visible confirmation of its dependants."""

    def __init__(self, message: str, operation_ids: list[str]) -> None:
        super().__init__(message)
        self.operation_ids = operation_ids


class ExecutionBlocked(StoreError):
    """Unresolved execution state prevents new work from being submitted."""
