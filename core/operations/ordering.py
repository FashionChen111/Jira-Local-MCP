"""Pure queue grouping and ordering algorithms."""

from __future__ import annotations

import re
from typing import Any, Iterable, TypeVar

CATEGORY_BY_OPERATION = {
    "update_field": "standard_field",
    "update_custom_field": "custom_field",
    "transition_issue": "workflow",
    "add_attachment": "attachment",
    "add_comment": "comment",
    "add_worklog": "worklog",
}
CATEGORY_RANK = {
    "producer": -1,
    "standard_field": 0,
    "custom_field": 1,
    "workflow": 2,
    "attachment": 3,
    "comment": 4,
    "worklog": 5,
    "other": 6,
}

_Row = TypeVar("_Row")


def natural_issue_key(issue_key: str) -> tuple[str, int, str]:
    match = re.match(r"^(.*)-(\d+)$", issue_key)
    if not match:
        return issue_key, -1, issue_key
    return match.group(1), int(match.group(2)), issue_key


def default_group_order(rows: Iterable[_Row]) -> list[_Row]:
    return sorted(
        rows,
        key=lambda row: (
            CATEGORY_RANK[row["category"]],  # type: ignore[index]
            row["created_at"],  # type: ignore[index]
            row["operation_id"],  # type: ignore[index]
        ),
    )


def automatic_ids(default_ids: list[str], submitted_ids: list[str]) -> set[str]:
    """Return a deterministic LCS, preferring earlier default-order entries."""
    if not submitted_ids:
        return set()
    default_index = {
        operation_id: index for index, operation_id in enumerate(default_ids)
    }
    sequence = [default_index[operation_id] for operation_id in submitted_ids]
    lengths = [1] * len(sequence)
    for left in range(len(sequence) - 1, -1, -1):
        for right in range(left + 1, len(sequence)):
            if sequence[right] > sequence[left]:
                lengths[left] = max(lengths[left], 1 + lengths[right])

    remaining = max(lengths)
    position = 0
    minimum_index = -1
    automatic: set[str] = set()
    while remaining:
        candidates = [
            index
            for index in range(position, len(sequence))
            if sequence[index] > minimum_index and lengths[index] >= remaining
        ]
        chosen = min(candidates, key=lambda index: sequence[index])
        automatic.add(submitted_ids[chosen])
        minimum_index = sequence[chosen]
        position = chosen + 1
        remaining -= 1
    return automatic


def derive_manual_anchors(
    rows: list[Any], submitted_ids: list[str]
) -> dict[str, str | None]:
    default_ids = [row["operation_id"] for row in default_group_order(rows)]
    automatic = automatic_ids(default_ids, submitted_ids)
    return {
        operation_id: (
            None
            if operation_id in automatic
            else ("" if index == 0 else submitted_ids[index - 1])
        )
        for index, operation_id in enumerate(submitted_ids)
    }
