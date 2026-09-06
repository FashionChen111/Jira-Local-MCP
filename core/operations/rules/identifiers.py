"""Pure validators for Jira keys and server-generated operation identifiers."""

from __future__ import annotations

import re
from typing import TypeGuard


_OPERATION_ID = re.compile(r"^op-[0-9a-f]{32}$")
_JIRA_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$")


def is_operation_id(value: object) -> TypeGuard[str]:
    """Return whether *value* is a server-generated operation identifier."""

    return isinstance(value, str) and bool(_OPERATION_ID.fullmatch(value))


def is_jira_issue_key(value: object) -> TypeGuard[str]:
    """Return whether *value* has the accepted Jira issue-key shape."""

    return isinstance(value, str) and bool(_JIRA_ISSUE_KEY.fullmatch(value))


def validate_operation_ids(operation_ids: list[str], *, maximum: int) -> None:
    """Require a nonempty bounded list of unique server-generated IDs."""
    if (
        not isinstance(operation_ids, list)
        or not 1 <= len(operation_ids) <= maximum
        or any(not is_operation_id(value) for value in operation_ids)
        or len(operation_ids) != len(set(operation_ids))
    ):
        raise ValueError(f"operation_ids 必须为 1 至 {maximum} 个合法且不重复的 operation ID")
