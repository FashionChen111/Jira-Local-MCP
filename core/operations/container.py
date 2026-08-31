"""Explicit operations service container used by each external adapter."""

from __future__ import annotations

from dataclasses import dataclass

from core.operations.repository import OperationRepository
from core.operations.services.execution import ExecutionService
from core.operations.services.query import QueryService
from core.operations.services.queue import QueueService
from core.operations.services.recovery import RecoveryService
from core.operations.services.submission import SubmissionService
from core.storage.database import open_database


@dataclass(frozen=True)
class OperationServices:
    """Narrow entry points for all operations use cases."""

    queue: QueueService
    submission: SubmissionService
    execution: ExecutionService
    recovery: RecoveryService
    query: QueryService


_services: OperationServices | None = None


def get_services() -> OperationServices:
    """Initialize the operations schema and return the process-local container."""

    global _services
    if _services is None:
        connection = open_database("operations")
        connection.close()
        repository = OperationRepository()
        _services = OperationServices(
            queue=QueueService(repository),
            submission=SubmissionService(repository),
            execution=ExecutionService(repository),
            recovery=RecoveryService(repository),
            query=QueryService(repository),
        )
    return _services


def reset_services() -> None:
    """Drop the process-local container after tests switch database config."""

    global _services
    _services = None
