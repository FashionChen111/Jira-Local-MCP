"""Expected operation-store state and validation failures."""


class StoreError(RuntimeError):
    """Expected state or validation failure."""


class RevisionConflict(StoreError):
    """The submitted queue revision no longer matches persisted state."""


class ConfirmationRequired(StoreError):
    """The requested action requires an additional human confirmation."""


class ExecutionBlocked(StoreError):
    """Unresolved execution state prevents new work from being submitted."""
