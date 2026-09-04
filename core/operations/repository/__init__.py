"""Connection-scoped persistence modules; repository functions never commit."""

from core.operations.repository._records import DatabaseConnection

__all__ = ["DatabaseConnection"]
