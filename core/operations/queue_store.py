"""Queued-operation persistence and layout management."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from typing import Any

from core import risk_engine
from core.operations.errors import (
    DependencyConfirmationRequired,
    RevisionConflict,
    StoreError,
)
from core.operations.issue_references import (
    PRODUCER_OPS,
    IssueReferenceError,
    build_issue_references,
    first_unresolved_producer,
    producer_state,
)
from core.operations.models import OperationRecord
from core.operations.ordering import (
    CATEGORY_BY_OPERATION,
    default_group_order,
    derive_manual_anchors,
    natural_issue_key,
)
from core.operations.persistence import (
    append_event,
    bump_revisions,
    operation_from_row,
    operation_values,
    queue_public,
    revision,
)
from core.storage.database import new_id, transaction, utc_ms


class QueueStore:
    """Internal queue component; connection-scoped helpers never commit."""

    @staticmethod
    def ordered_scope_rows(
        connection: sqlite3.Connection, scope: str
    ) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM queue_items WHERE layout_scope=? ORDER BY position", (scope,)
        ).fetchall()

    @staticmethod
    def queue_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return connection.execute("SELECT * FROM queue_items").fetchall()

    @staticmethod
    def set_scope_order(
        connection: sqlite3.Connection, scope: str, operation_ids: list[str]
    ) -> None:
        if not operation_ids:
            return
        maximum = connection.execute(
            "SELECT COALESCE(MAX(position),0) FROM queue_items WHERE layout_scope=?",
            (scope,),
        ).fetchone()[0]
        offset = int(maximum) + len(operation_ids) + 1
        connection.execute(
            "UPDATE queue_items SET position=position+? WHERE layout_scope=?",
            (offset, scope),
        )
        for position, operation_id in enumerate(operation_ids, 1):
            connection.execute(
                "UPDATE queue_items SET position=? WHERE operation_id=?",
                (position, operation_id),
            )

    def rebuild_group(self, connection: sqlite3.Connection, scope: str) -> None:
        rows = self.ordered_scope_rows(connection, scope)
        if not rows:
            return
        row_by_id = {row["operation_id"]: row for row in rows}
        anchors: dict[str, str | None] = {
            operation_id: row["manual_after_operation_id"]
            for operation_id, row in row_by_id.items()
        }
        for operation_id, anchor in list(anchors.items()):
            if anchor not in (None, "") and (
                anchor not in row_by_id or anchor == operation_id
            ):
                anchors[operation_id] = None

        completed: set[str] = set()
        for start in row_by_id:
            chain: list[str] = []
            positions: dict[str, int] = {}
            current: str | None = start
            while (
                current is not None
                and current not in completed
                and current not in positions
            ):
                positions[current] = len(chain)
                chain.append(current)
                parent = anchors[current]
                current = parent if parent not in (None, "") else None
            if current is not None and current in positions:
                for operation_id in chain[positions[current] :]:
                    anchors[operation_id] = None
            completed.update(chain)

        old_order = {row["operation_id"]: row["position"] for row in rows}
        children: dict[str, list[str]] = {}
        for operation_id, anchor in anchors.items():
            if anchor is not None:
                children.setdefault(anchor, []).append(operation_id)
        for values in children.values():
            values.sort(
                key=lambda operation_id: (
                    old_order[operation_id],
                    row_by_id[operation_id]["created_at"],
                    operation_id,
                )
            )

        ordered_ids: list[str] = []
        visited: set[str] = set()

        def append_children(parent: str) -> None:
            stack = list(reversed(children.get(parent, [])))
            while stack:
                operation_id = stack.pop()
                if operation_id in visited:
                    continue
                visited.add(operation_id)
                ordered_ids.append(operation_id)
                stack.extend(reversed(children.get(operation_id, [])))

        append_children("")
        automatic_rows = default_group_order(
            row for row in rows if anchors[row["operation_id"]] is None
        )
        for row in automatic_rows:
            operation_id = row["operation_id"]
            if operation_id not in visited:
                visited.add(operation_id)
                ordered_ids.append(operation_id)
            append_children(operation_id)

        remaining = [row for row in rows if row["operation_id"] not in visited]
        for row in default_group_order(remaining):
            anchors[row["operation_id"]] = None
            ordered_ids.append(row["operation_id"])

        for operation_id, anchor in anchors.items():
            if row_by_id[operation_id]["manual_after_operation_id"] != anchor:
                connection.execute(
                    "UPDATE queue_items SET manual_after_operation_id=? WHERE operation_id=?",
                    (anchor, operation_id),
                )
        self.set_scope_order(connection, scope, ordered_ids)

    def rebuild_scopes(
        self, connection: sqlite3.Connection, scopes: set[str]
    ) -> None:
        for scope in scopes:
            if scope:
                self.rebuild_group(connection, scope)
            else:
                remaining = [
                    row["operation_id"]
                    for row in self.ordered_scope_rows(connection, scope)
                ]
                self.set_scope_order(connection, scope, remaining)

    @staticmethod
    def layout_for_operation(
        operation: OperationRecord,
    ) -> tuple[str, str | None, str]:
        """Return layout kind, category, and scope for the current ref state."""
        if operation.operation_type in PRODUCER_OPS:
            return "planned", "producer", operation.operation_id
        pending_producer = first_unresolved_producer(
            operation.operation_type, operation.issue_references
        )
        category = CATEGORY_BY_OPERATION.get(operation.operation_type)
        if pending_producer:
            return "planned", category or "other", pending_producer
        effective_key = operation.effective_issue_key()
        if category is not None and effective_key is not None:
            return "grouped", category, effective_key
        return "standalone", None, ""

    def insert_queue(
        self,
        connection: sqlite3.Connection,
        operation: OperationRecord,
        action_code: str,
        event_data: dict[str, Any],
    ) -> None:
        layout_kind, category, scope = self.layout_for_operation(operation)
        rows = self.ordered_scope_rows(connection, scope)
        connection.execute(
            """INSERT INTO queue_items(
              operation_id,operation_type,issue_key,risk_level,payload_json,issue_references_json,
              description,source,created_at,attempt_no,layout_kind,category,layout_scope,position,
              manual_after_operation_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            operation_values(operation)
            + (layout_kind, category, scope, len(rows) + 1),
        )
        if layout_kind != "standalone":
            self.rebuild_group(connection, scope)
        payload = {"operation": asdict(operation), **event_data}
        append_event(
            connection, operation.operation_id, "enqueue", action_code, payload
        )

    def enqueue(
        self,
        operation_type: str,
        issue_key: str | None,
        payload: dict[str, Any],
        description: str,
        source: str = "",
    ) -> OperationRecord:
        with transaction() as connection:
            risk_engine.assert_not_delete(operation_type, payload)
            risk_engine.validate_operation_target(operation_type, issue_key)
            try:
                references = build_issue_references(
                    connection, operation_type, payload
                )
            except IssueReferenceError as exc:
                raise risk_engine.RiskError(str(exc)) from exc
            risk_level, _ = risk_engine.classify_risk(operation_type, payload)
            operation = OperationRecord(
                operation_id=new_id("op"),
                operation_type=operation_type,
                issue_key=issue_key,
                risk_level=risk_level,
                payload=payload,
                description=description,
                source=source,
                created_at=utc_ms(),
                issue_references=references,
                attempt_no=1,
            )
            self.insert_queue(
                connection, operation, "mcp_append", {"source": source}
            )
            bump_revisions(connection, "queue")
        return operation

    def display_order(self, connection: sqlite3.Connection) -> list[sqlite3.Row]:
        rows = self.queue_rows(connection)
        groups: dict[str, list[sqlite3.Row]] = {}
        planned: dict[str, list[sqlite3.Row]] = {}
        standalone: list[sqlite3.Row] = []
        for row in rows:
            if row["layout_kind"] == "grouped":
                groups.setdefault(row["layout_scope"], []).append(row)
            elif row["layout_kind"] == "planned":
                planned.setdefault(row["layout_scope"], []).append(row)
            else:
                standalone.append(row)
        ordered: list[sqlite3.Row] = []
        for issue_key in sorted(groups, key=natural_issue_key):
            ordered.extend(
                sorted(groups[issue_key], key=lambda item: item["position"])
            )
        for producer_id in sorted(planned):
            ordered.extend(
                sorted(planned[producer_id], key=lambda item: item["position"])
            )
        ordered.extend(sorted(standalone, key=lambda item: item["position"]))
        return ordered

    def read_state(self, connection: sqlite3.Connection) -> dict[str, Any]:
        ordered = self.display_order(connection)
        grouped: dict[str, list[dict[str, Any]]] = {}
        planned: dict[str, list[dict[str, Any]]] = {}
        standalone: list[dict[str, Any]] = []
        for row in ordered:
            public = queue_public(row)
            statuses: dict[str, str] = {}
            blocked_reason: str | None = None
            for field_name, reference in public["issue_references"].items():
                if reference.get("resolved_issue_key"):
                    statuses[field_name] = "resolved"
                    continue
                state = producer_state(
                    connection, str(reference["operation_id"])
                ).state
                statuses[field_name] = state
                if state in {"failed", "abandoned", "dependency_failed", "unknown"}:
                    blocked_reason = (
                        f"依赖的创建/克隆 operation 已{state}: "
                        f"{reference['operation_id']}"
                    )
            public["dependency_statuses"] = statuses
            public["submittable"] = blocked_reason is None
            public["submission_block_reason"] = blocked_reason
            if row["layout_kind"] == "grouped":
                grouped.setdefault(row["layout_scope"], []).append(public)
            elif row["layout_kind"] == "planned":
                planned.setdefault(row["layout_scope"], []).append(public)
            else:
                standalone.append(public)
        return {
            "groups": [
                {"issue_key": key, "operations": grouped[key]}
                for key in sorted(grouped, key=natural_issue_key)
            ],
            "planned_groups": [
                {
                    "producer_operation_id": producer_id,
                    "producer_state": producer_state(
                        connection, producer_id
                    ).state,
                    "operations": planned[producer_id],
                }
                for producer_id in sorted(planned)
            ],
            "standalone": standalone,
        }

    def reorder(
        self,
        expected_queue_revision: int,
        groups: list[dict[str, Any]],
        standalone_operation_ids: list[str],
        planned_groups: list[dict[str, Any]] | None = None,
    ) -> int:
        planned_groups = planned_groups or []
        with transaction() as connection:
            if revision(connection, "queue") != expected_queue_revision:
                raise RevisionConflict("队列已更新，请刷新后重新确认排序")
            rows = self.queue_rows(connection)
            row_by_id = {row["operation_id"]: row for row in rows}
            submitted: list[str] = []
            seen_group_keys: set[str] = set()
            for group in groups:
                issue_key = group["issue_key"]
                ids = list(group["operation_ids"])
                if issue_key in seen_group_keys:
                    raise StoreError("issue 分组不得重复")
                seen_group_keys.add(issue_key)
                for operation_id in ids:
                    row = row_by_id.get(operation_id)
                    if (
                        row is None
                        or row["layout_kind"] != "grouped"
                        or row["layout_scope"] != issue_key
                    ):
                        raise StoreError("分组中包含未知、零散或跨 issue operation")
                submitted.extend(ids)
            seen_planned_ids: set[str] = set()
            for group in planned_groups:
                producer_id = group["producer_operation_id"]
                ids = list(group["operation_ids"])
                if producer_id in seen_planned_ids:
                    raise StoreError("待创建事务分组不得重复")
                seen_planned_ids.add(producer_id)
                for operation_id in ids:
                    row = row_by_id.get(operation_id)
                    if (
                        row is None
                        or row["layout_kind"] != "planned"
                        or row["layout_scope"] != producer_id
                    ):
                        raise StoreError(
                            "待创建分组包含未知、零散或跨生产者 operation"
                        )
                producer_rows = [
                    operation_id
                    for operation_id in ids
                    if row_by_id[operation_id]["operation_type"] in PRODUCER_OPS
                    and operation_id == producer_id
                ]
                if producer_rows and ids[0] != producer_id:
                    raise StoreError("创建/克隆 operation 必须固定在计划分组首位")
                submitted.extend(ids)
            for operation_id in standalone_operation_ids:
                row = row_by_id.get(operation_id)
                if row is None or row["layout_kind"] == "planned":
                    raise StoreError("零散列表包含未知或待创建分组 operation")
                submitted.append(operation_id)
            if len(submitted) != len(set(submitted)):
                raise StoreError("operation ID 不得重复")
            if set(submitted) != set(row_by_id):
                raise StoreError("排序布局必须完整包含当前队列")

            temporary_position = len(rows) + 1
            for operation_id in standalone_operation_ids:
                row = row_by_id[operation_id]
                if row["layout_kind"] == "grouped":
                    connection.execute(
                        """UPDATE queue_items SET layout_kind='standalone',category=NULL,layout_scope='',
                           position=?,manual_after_operation_id=NULL WHERE operation_id=?""",
                        (temporary_position, operation_id),
                    )
                    temporary_position += 1
            for group in groups:
                scope = group["issue_key"]
                submitted_ids = list(group["operation_ids"])
                scope_rows = self.ordered_scope_rows(connection, scope)
                anchors = derive_manual_anchors(scope_rows, submitted_ids)
                for operation_id, anchor in anchors.items():
                    connection.execute(
                        "UPDATE queue_items SET manual_after_operation_id=? WHERE operation_id=?",
                        (anchor, operation_id),
                    )
                self.rebuild_group(connection, scope)
            for group in planned_groups:
                scope = group["producer_operation_id"]
                submitted_ids = list(group["operation_ids"])
                scope_rows = self.ordered_scope_rows(connection, scope)
                anchors = derive_manual_anchors(scope_rows, submitted_ids)
                if scope in anchors:
                    anchors[scope] = None
                for operation_id, anchor in anchors.items():
                    connection.execute(
                        "UPDATE queue_items SET manual_after_operation_id=? WHERE operation_id=?",
                        (anchor, operation_id),
                    )
                self.rebuild_group(connection, scope)
            self.set_scope_order(connection, "", list(standalone_operation_ids))
            for operation_id in submitted:
                append_event(
                    connection,
                    operation_id,
                    "internal",
                    "reorder",
                    {"layout_saved": True},
                )
            bump_revisions(connection, "queue")
            return revision(connection, "queue")

    def cancel(
        self,
        expected_queue_revision: int,
        operation_ids: list[str],
        cascade_confirmed: bool,
    ) -> tuple[list[str], int]:
        if not operation_ids or len(operation_ids) != len(set(operation_ids)):
            raise StoreError("取消 operation ID 必须非空且不得重复")
        with transaction() as connection:
            if revision(connection, "queue") != expected_queue_revision:
                raise RevisionConflict("队列已更新，请刷新后重新确认取消范围")
            rows = self.queue_rows(connection)
            row_by_id = {row["operation_id"]: row for row in rows}
            if any(operation_id not in row_by_id for operation_id in operation_ids):
                raise StoreError("取消请求包含未知或已处理的 operation")
            requested = set(operation_ids)
            expanded = set(requested)
            changed = True
            while changed:
                changed = False
                for row in rows:
                    operation = operation_from_row(row)
                    if row["operation_id"] in expanded:
                        continue
                    referenced = {
                        str(reference["operation_id"])
                        for reference in operation.issue_references.values()
                    }
                    if referenced & expanded:
                        expanded.add(row["operation_id"])
                        changed = True
            cascaded = sorted(expanded - requested)
            if cascaded and not cascade_confirmed:
                raise DependencyConfirmationRequired(
                    f"取消生产者将同时取消 {len(cascaded)} 个依赖 operation",
                    sorted(expanded),
                )
            ordered_ids = [
                row["operation_id"]
                for row in self.display_order(connection)
                if row["operation_id"] in expanded
            ]
            scopes: set[str] = set()
            for operation_id in ordered_ids:
                row = row_by_id[operation_id]
                scopes.add(row["layout_scope"])
                append_event(
                    connection,
                    operation_id,
                    "dequeue",
                    "cascade_cancelled"
                    if operation_id not in requested
                    else "cancelled_by_user",
                    {"requested_operation_ids": operation_ids},
                )
                connection.execute(
                    "DELETE FROM queue_items WHERE operation_id=?", (operation_id,)
                )
            self.rebuild_scopes(connection, scopes)
            bump_revisions(connection, "queue")
            return ordered_ids, revision(connection, "queue")
