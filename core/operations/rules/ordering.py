"""Pure automatic queue grouping and stable dependency-aware ordering."""

from __future__ import annotations

import heapq
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

from core.operations.dependencies import orchestration as dependency_orchestration
from core.operations.models import DependencyState, OperationRecord, QueueGroupKind
from core.operations.rules.validation import is_jira_issue_key


CATEGORY_BY_OPERATION = {
    "update_field": "standard_field",
    "update_custom_field": "custom_field",
    "transition_issue": "workflow",
    "add_attachment": "attachment",
    "add_comment": "comment",
    "add_worklog": "worklog",
}
_CATEGORY_RANK = {
    "producer": 0,
    "standard_field": 1,
    "custom_field": 2,
    "workflow": 3,
    "attachment": 4,
    "comment": 5,
    "worklog": 6,
    "other": 7,
}
_NATURAL_PARTS = re.compile(r"(\d+)")


class OrderingError(ValueError):
    """The selected operations cannot be placed in a valid dependency order."""


@dataclass(frozen=True)
class AutomaticQueueGroup:
    """Internal automatically derived queue group."""

    group_key: str
    kind: QueueGroupKind
    producer_state: str | None
    operations: tuple[OperationRecord, ...]


@dataclass(frozen=True)
class AutomaticQueue:
    """Internal ordered queue layout with no persisted positioning metadata."""

    groups: tuple[AutomaticQueueGroup, ...]
    standalone: tuple[OperationRecord, ...]

    def flattened(self) -> tuple[OperationRecord, ...]:
        return tuple(
            operation
            for group in self.groups
            for operation in group.operations
        ) + self.standalone


def natural_issue_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """Build a deterministic case-insensitive natural-sort key."""

    parts: list[tuple[int, int | str]] = []
    for part in _NATURAL_PARTS.split(value.upper()):
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part))
    return tuple(parts)


def operation_category(operation: OperationRecord) -> str:
    """Return the fixed in-group category for an operation."""

    if (
        dependency_orchestration.pending_issue_group_id(operation)
        == operation.operation_id
    ):
        return "producer"
    return CATEGORY_BY_OPERATION.get(operation.operation_type, "other")


def operation_order_key(operation: OperationRecord) -> tuple[int, int, str]:
    """Return the immutable in-group ordering key."""

    return (
        _CATEGORY_RANK[operation_category(operation)],
        operation.created_at,
        operation.operation_id,
    )


def automatic_queue(
    operations: list[OperationRecord] | tuple[OperationRecord, ...],
    producer_states: dict[str, DependencyState] | None = None,
) -> AutomaticQueue:
    """Derive all groups and ordering from immutable operation snapshots."""

    producer_states = producer_states or {}
    by_id = {operation.operation_id: operation for operation in operations}
    jira_groups: dict[str, list[OperationRecord]] = {}
    pending_groups: dict[str, list[OperationRecord]] = {}
    standalone: list[OperationRecord] = []

    for operation in operations:
        pending_group = dependency_orchestration.pending_issue_group_id(operation)
        if pending_group is not None:
            pending_groups.setdefault(pending_group, []).append(operation)
        elif (
            operation.operation_type in CATEGORY_BY_OPERATION
            and is_jira_issue_key(operation.issue_key)
        ):
            jira_groups.setdefault(operation.issue_key, []).append(operation)
        else:
            standalone.append(operation)

    groups: list[AutomaticQueueGroup] = []
    for issue_key in sorted(jira_groups, key=natural_issue_key):
        groups.append(
            AutomaticQueueGroup(
                group_key=issue_key,
                kind="jira_issue",
                producer_state=None,
                operations=tuple(sorted(jira_groups[issue_key], key=operation_order_key)),
            )
        )

    def pending_group_key(group_key: str) -> tuple[int, str]:
        members = pending_groups[group_key]
        producer = by_id.get(group_key)
        created_at = (
            producer.created_at
            if producer is not None
            else min(member.created_at for member in members)
        )
        return created_at, group_key

    for producer_id in sorted(pending_groups, key=pending_group_key):
        state = producer_states.get(producer_id)
        groups.append(
            AutomaticQueueGroup(
                group_key=producer_id,
                kind="pending_issue",
                producer_state=state.state if state is not None else "unknown",
                operations=tuple(
                    sorted(pending_groups[producer_id], key=operation_order_key)
                ),
            )
        )

    return AutomaticQueue(
        groups=tuple(groups),
        standalone=tuple(
            sorted(
                standalone,
                key=lambda operation: (operation.created_at, operation.operation_id),
            )
        ),
    )


def stable_dependency_order(
    operations: list[OperationRecord] | tuple[OperationRecord, ...],
    referenced_operation_ids_by_consumer: Mapping[str, Collection[str]],
) -> tuple[OperationRecord, ...]:
    """Order a selected set from its already-inspected dependency adjacency."""

    ordered = tuple(operations)
    operation_by_id = {operation.operation_id: operation for operation in ordered}
    display_index = {
        operation.operation_id: index for index, operation in enumerate(ordered)
    }
    graph = {
        operation.operation_id: {
            referenced_operation_id
            for referenced_operation_id in (
                referenced_operation_ids_by_consumer.get(
                    operation.operation_id, ()
                )
            )
            if referenced_operation_id in operation_by_id
        }
        for operation in ordered
    }
    sorter = TopologicalSorter(graph)
    try:
        sorter.prepare()
    except CycleError as exc:
        raise OrderingError("提交集合包含循环依赖") from exc

    # TopologicalSorter owns graph validation and cycle detection.  A one-node
    # Kahn frontier then supplies the stable tie-break that static_order cannot:
    # a newly-ready earlier display item may run before a later independent item.
    children: dict[str, list[str]] = {operation_id: [] for operation_id in graph}
    remaining_dependencies = {
        operation_id: len(dependencies)
        for operation_id, dependencies in graph.items()
    }
    for operation_id, referenced_operation_ids in graph.items():
        for referenced_operation_id in referenced_operation_ids:
            children[referenced_operation_id].append(operation_id)
    heap = [
        (display_index[operation_id], operation_id)
        for operation_id, count in remaining_dependencies.items()
        if count == 0
    ]
    heapq.heapify(heap)
    result: list[OperationRecord] = []
    while heap:
        _, operation_id = heapq.heappop(heap)
        result.append(operation_by_id[operation_id])
        for child_id in children[operation_id]:
            remaining_dependencies[child_id] -= 1
            if remaining_dependencies[child_id] == 0:
                heapq.heappush(
                    heap, (display_index[child_id], child_id)
                )
    if len(result) != len(ordered):
        raise OrderingError("提交集合无法生成依赖顺序")
    return tuple(result)
