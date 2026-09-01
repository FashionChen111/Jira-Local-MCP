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
