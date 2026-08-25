"""Submission, execution journal, recovery, and resolution persistence."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any, Literal

from core import risk_engine
from core.operations.errors import (
    ConfirmationRequired,
    ExecutionBlocked,
    RevisionConflict,
    StoreError,
)
from core.operations.issue_references import (
    PRODUCER_OPS,
    is_issue_key,
    producer_state,
    resolved_reference_copy,
)
from core.operations.models import ExecutionClaim
from core.operations.persistence import (
    append_event,
    bump_revisions,
    execution_record_public,
    get_revisions,
    operation_from_row,
    operation_values,
    revision,
    staging_public,
)
from core.operations.queue_store import QueueStore
from core.storage.database import (
    canonical_json,
    new_id,
    open_database,
    transaction,
    utc_ms,
)


class ExecutionStore:
    """Internal execution component sharing queue helpers within one transaction."""

    def __init__(self, queue_store: QueueStore) -> None:
        self._queue = queue_store

    @staticmethod
    def read_state(connection: sqlite3.Connection) -> dict[str, Any]:
        staging_rows = connection.execute(
            "SELECT * FROM execution_staging ORDER BY stage_order"
        ).fetchall()
        execution = {
            name: [] for name in ("staged", "executing", "indeterminate", "held")
        }
        for row in staging_rows:
            execution[row["state"]].append(staging_public(row))
        return execution

    @staticmethod
    def _selection_submission_risks(
        rows: list[sqlite3.Row],
    ) -> dict[str, str]:
        risks = {row["operation_id"]: row["risk_level"] for row in rows}
        counts: dict[str, int] = {}
        for row in rows:
            operation = operation_from_row(row)
            issue_key = operation.effective_issue_key()
            if (
                row["operation_type"] in ("update_field", "update_custom_field")
                and issue_key
            ):
                counts[issue_key] = counts.get(issue_key, 0) + 1
        hot = {key for key, count in counts.items() if count >= 5}
        for row in rows:
            operation = operation_from_row(row)
            if (
                row["operation_type"] in ("update_field", "update_custom_field")
                and operation.effective_issue_key() in hot
            ):
                risks[row["operation_id"]] = "high"
        return risks

    @staticmethod
    def _dependency_order(
        selected: list[sqlite3.Row],
    ) -> list[sqlite3.Row]:
        """Stable topological order with the persisted display order as tie-breaker."""
        row_by_id = {row["operation_id"]: row for row in selected}
        pending = list(selected)
        emitted: set[str] = set()
        ordered: list[sqlite3.Row] = []
        while pending:
            ready = next(
                (
                    row
                    for row in pending
                    if not (
                        set(operation_from_row(row).unresolved_dependency_ids())
                        & set(row_by_id)
                        - emitted
                    )
                ),
                None,
            )
            if ready is None:
                raise StoreError("operation 依赖关系存在循环")
            pending.remove(ready)
            emitted.add(ready["operation_id"])
            ordered.append(ready)
        return ordered

    def _validate_submission_selection(
        self,
        connection: sqlite3.Connection,
        expected_queue_revision: int,
        operation_ids: list[str],
        high_risk_confirmed: bool,
    ) -> tuple[list[sqlite3.Row], dict[str, str]]:
        if not operation_ids or len(operation_ids) != len(set(operation_ids)):
            raise StoreError("提交 operation ID 必须非空且不得重复")
        if revision(connection, "queue") != expected_queue_revision:
            raise RevisionConflict("队列已更新，请刷新后重新确认")
        if connection.execute(
            "SELECT 1 FROM execution_staging WHERE state IN ('indeterminate','held') LIMIT 1"
        ).fetchone():
            raise ExecutionBlocked("存在不确定或冻结任务，处置完成前不能提交新任务")
        ordered = self._queue.display_order(connection)
        row_by_id = {row["operation_id"]: row for row in ordered}
        if any(operation_id not in row_by_id for operation_id in operation_ids):
            raise StoreError("提交包含未知或已处理的 operation")
        selected_set = set(operation_ids)
        selected = [row for row in ordered if row["operation_id"] in selected_set]
        for row in selected:
            operation = operation_from_row(row)
            payload = operation.effective_payload()
            risk_engine.assert_not_delete(
                row["operation_type"], payload  # type: ignore[arg-type]
            )
            for dependency_id in operation.unresolved_dependency_ids():
                state = producer_state(connection, dependency_id)
                if state.state == "queued" and dependency_id not in selected_set:
                    raise StoreError(
                        f"提交缺少依赖的创建/克隆 operation: {dependency_id}"
                    )
                if state.state in {
                    "failed",
                    "abandoned",
                    "dependency_failed",
                    "unknown",
                }:
                    raise StoreError(
                        f"依赖的创建/克隆 operation 不可用: {dependency_id}"
                    )
        selected = self._dependency_order(selected)
        submission_risks = self._selection_submission_risks(selected)
        if (
            any(level == "high" for level in submission_risks.values())
            and not high_risk_confirmed
        ):
            raise ConfirmationRequired("本次提交包含高风险操作，需要二次确认")
        return selected, submission_risks

    def submit(
        self,
        expected_queue_revision: int,
        operation_ids: list[str],
        high_risk_confirmed: bool,
    ) -> tuple[list[str], dict[str, int]]:
        with transaction() as connection:
            selected, submission_risks = self._validate_submission_selection(
                connection, expected_queue_revision, operation_ids, high_risk_confirmed
            )
            max_stage = connection.execute(
                "SELECT COALESCE(MAX(stage_order),0) FROM execution_staging"
            ).fetchone()[0]
            submitted_at = utc_ms()
            scopes: set[str] = set()
            for offset, row in enumerate(selected, 1):
                operation = operation_from_row(row)
                connection.execute(
                    """INSERT INTO execution_staging(
                      operation_id,operation_type,issue_key,risk_level,payload_json,issue_references_json,
                      description,source,created_at,attempt_no,stage_order,state,submission_risk_level,
                      execution_id,submitted_at,started_at,held_reason,blocked_by_execution_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'staged',?,NULL,?,NULL,NULL,NULL)""",
                    operation_values(operation)
                    + (
                        int(max_stage) + offset,
                        submission_risks[row["operation_id"]],
                        submitted_at,
                    ),
                )
                scopes.add(row["layout_scope"])
                append_event(
                    connection,
                    row["operation_id"],
                    "dequeue",
                    "submit_for_execution",
                    {
                        "stage_order": int(max_stage) + offset,
                        "submission_risk_level": submission_risks[
                            row["operation_id"]
                        ],
                    },
                )
                connection.execute(
                    "DELETE FROM queue_items WHERE operation_id=?",
                    (row["operation_id"],),
                )
            self._queue.rebuild_scopes(connection, scopes)
            bump_revisions(connection, "queue", "execution")
            return [row["operation_id"] for row in selected], get_revisions(connection)

    def validate_submission(
        self,
        expected_queue_revision: int,
        operation_ids: list[str],
        high_risk_confirmed: bool,
    ) -> None:
        """Validate before Jira ping; submit revalidates inside its transaction."""
        connection = open_database("operations")
        try:
            self._validate_submission_selection(
                connection, expected_queue_revision, operation_ids, high_risk_confirmed
            )
        finally:
            connection.close()

    def _resolve_producer_references(
        self,
        connection: sqlite3.Connection,
        producer_operation_id: str,
        issue_key: str,
    ) -> tuple[bool, bool]:
        """Persist one producer result across queue and staging snapshots."""
        queue_changed = False
        staging_changed = False
        affected_scopes: set[str] = set()
        queue_rows = connection.execute("SELECT * FROM queue_items").fetchall()
        for row in queue_rows:
            operation = operation_from_row(row)
            references, fields = resolved_reference_copy(
                operation.issue_references, producer_operation_id, issue_key
            )
            if not fields:
                continue
            updated = replace(operation, issue_references=references)
            layout_kind, category, scope = self._queue.layout_for_operation(updated)
            old_scope = row["layout_scope"]
            if (
                layout_kind == row["layout_kind"]
                and category == row["category"]
                and scope == old_scope
            ):
                connection.execute(
                    "UPDATE queue_items SET issue_references_json=? WHERE operation_id=?",
                    (canonical_json(references), row["operation_id"]),
                )
            else:
                maximum = connection.execute(
                    "SELECT COALESCE(MAX(position),0) FROM queue_items WHERE layout_scope=?",
                    (scope,),
                ).fetchone()[0]
                connection.execute(
                    """UPDATE queue_items SET issue_references_json=?,layout_kind=?,category=?,
                       layout_scope=?,position=?,manual_after_operation_id=NULL WHERE operation_id=?""",
                    (
                        canonical_json(references),
                        layout_kind,
                        category,
                        scope,
                        int(maximum) + 1,
                        row["operation_id"],
                    ),
                )
            affected_scopes.update((old_scope, scope))
            append_event(
                connection,
                row["operation_id"],
                "internal",
                "issue_reference_resolved",
                {
                    "producer_operation_id": producer_operation_id,
                    "issue_key": issue_key,
                    "fields": fields,
                    "location": "queue",
                },
            )
            queue_changed = True

        staging_rows = connection.execute(
            "SELECT * FROM execution_staging"
        ).fetchall()
        for row in staging_rows:
            operation = operation_from_row(row)
            references, fields = resolved_reference_copy(
                operation.issue_references, producer_operation_id, issue_key
            )
            if not fields:
                continue
            connection.execute(
                "UPDATE execution_staging SET issue_references_json=? WHERE operation_id=?",
                (canonical_json(references), row["operation_id"]),
            )
            append_event(
                connection,
                row["operation_id"],
                "internal",
                "issue_reference_resolved",
                {
                    "producer_operation_id": producer_operation_id,
                    "issue_key": issue_key,
                    "fields": fields,
                    "location": "staging",
                },
            )
            staging_changed = True
        if queue_changed:
            self._queue.rebuild_scopes(connection, affected_scopes)
        return queue_changed, staging_changed

    @staticmethod
    def _terminalize_dependency_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        producer_operation_id: str,
    ) -> None:
        """Record a staged dependant as not attempted and remove it from staging."""
        now = utc_ms()
        execution_id = new_id("exec")
        operation = operation_from_row(row)
        summary = (
            "未调用 Jira：依赖的创建/克隆 operation 未产生可用事务 "
            f"({producer_operation_id})"
        )
        connection.execute(
            """INSERT INTO execution_records(
              execution_id,operation_id,attempt_no,operation_type,issue_key,risk_level,
              submission_risk_level,payload_json,issue_references_json,executed_issue_key,
              executed_payload_json,description,source,created_at,started_at,finished_at,
              outcome,summary,error,new_issue_key,elapsed_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,?,?,'dependency_failed',?,?,NULL,0)""",
            (
                execution_id,
                operation.operation_id,
                operation.attempt_no,
                operation.operation_type,
                operation.issue_key,
                operation.risk_level,
                row["submission_risk_level"],
                canonical_json(operation.payload),
                canonical_json(operation.issue_references),
                operation.description,
                operation.source,
                operation.created_at,
                now,
                now,
                summary,
                f"dependency_failed: {producer_operation_id}",
            ),
        )
        append_event(
            connection,
            operation.operation_id,
            "internal",
            "dependency_failed",
            {"producer_operation_id": producer_operation_id},
        )
        connection.execute(
            "DELETE FROM execution_staging WHERE operation_id=?",
            (operation.operation_id,),
        )

    def _terminalize_staged_dependants(
        self, connection: sqlite3.Connection, producer_operation_id: str
    ) -> int:
        rows = connection.execute(
            "SELECT * FROM execution_staging WHERE state IN ('staged','held') ORDER BY stage_order"
        ).fetchall()
        affected = 0
        for row in rows:
            operation = operation_from_row(row)
            if producer_operation_id not in operation.unresolved_dependency_ids():
                continue
            self._terminalize_dependency_row(
                connection, row, producer_operation_id
            )
            affected += 1
        return affected

    def claim_next(self) -> ExecutionClaim | None:
        with transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM execution_staging WHERE state='indeterminate' LIMIT 1"
            ).fetchone():
                return None
            while True:
                row = connection.execute(
                    "SELECT * FROM execution_staging ORDER BY stage_order LIMIT 1"
                ).fetchone()
                if row is None or row["state"] != "staged":
                    return None
                operation = operation_from_row(row)
                unresolved = operation.unresolved_dependency_ids()
                if not unresolved:
                    break
                changed = False
                for dependency_id in unresolved:
                    state = producer_state(connection, dependency_id)
                    if state.state == "success" and is_issue_key(state.issue_key):
                        queue_changed, staging_changed = (
                            self._resolve_producer_references(
                                connection, dependency_id, str(state.issue_key)
                            )
                        )
                        if queue_changed:
                            bump_revisions(connection, "queue")
                        if staging_changed:
                            bump_revisions(connection, "execution")
                        changed = True
                        break
                    if state.terminal_failure:
                        self._terminalize_dependency_row(
                            connection, row, dependency_id
                        )
                        bump_revisions(connection, "execution", "history")
                        changed = True
                        break
                if changed:
                    continue
                return None
            execution_id = new_id("exec")
            started_at = utc_ms()
            connection.execute(
                "UPDATE execution_staging SET state='executing',execution_id=?,started_at=? WHERE operation_id=? AND state='staged'",
                (execution_id, started_at, row["operation_id"]),
            )
            operation = operation_from_row(row)
            concrete = operation.concrete_copy()
            for field_name in concrete.issue_references:
                if not is_issue_key(concrete.payload.get(field_name)):
                    raise StoreError(
                        f"执行前引用字段 {field_name} 未解析为真实 Jira Key"
                    )
            connection.execute(
                """INSERT INTO execution_records(
                  execution_id,operation_id,attempt_no,operation_type,issue_key,risk_level,submission_risk_level,
                  payload_json,issue_references_json,executed_issue_key,executed_payload_json,
                  description,source,created_at,started_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    execution_id,
                    operation.operation_id,
                    operation.attempt_no,
                    operation.operation_type,
                    operation.issue_key,
                    operation.risk_level,
                    row["submission_risk_level"],
                    canonical_json(operation.payload),
                    canonical_json(operation.issue_references),
                    concrete.issue_key,
                    canonical_json(concrete.payload),
                    operation.description,
                    operation.source,
                    operation.created_at,
                    started_at,
                ),
            )
            bump_revisions(connection, "execution")
            return ExecutionClaim(
                execution_id=execution_id,
                operation=concrete,
                submission_risk_level=row["submission_risk_level"],
                stage_order=row["stage_order"],
                submitted_at=row["submitted_at"],
                started_at=started_at,
            )

    def complete_execution(
        self,
        execution_id: str,
        outcome: Literal["success", "failed"],
        summary: str | None,
        error: str | None,
        new_issue_key: str | None,
        elapsed_ms: int,
    ) -> None:
        with transaction() as connection:
            record = connection.execute(
                """SELECT operation_id,operation_type,outcome
                   FROM execution_records WHERE execution_id=?""",
                (execution_id,),
            ).fetchone()
            if record is None:
                raise StoreError("execution record 不存在")
            if record["outcome"] is not None:
                if record["outcome"] == outcome:
                    connection.execute(
                        "DELETE FROM execution_staging WHERE execution_id=?",
                        (execution_id,),
                    )
                    return
                raise StoreError("execution record 已有不同终态")
            is_producer = record["operation_type"] in PRODUCER_OPS
            if is_producer and outcome == "success" and not is_issue_key(
                new_issue_key
            ):
                raise StoreError("创建/克隆成功结果必须包含合法的新事务 Key")
            connection.execute(
                """UPDATE execution_records SET finished_at=?,outcome=?,summary=?,error=?,new_issue_key=?,elapsed_ms=?
                   WHERE execution_id=? AND outcome IS NULL""",
                (
                    utc_ms(),
                    outcome,
                    summary,
                    error,
                    new_issue_key,
                    elapsed_ms,
                    execution_id,
                ),
            )
            queue_changed = False
            if is_producer and outcome == "success":
                queue_changed, _ = self._resolve_producer_references(
                    connection, record["operation_id"], str(new_issue_key)
                )
            elif is_producer and outcome == "failed":
                self._terminalize_staged_dependants(
                    connection, record["operation_id"]
                )
            connection.execute(
                "DELETE FROM execution_staging WHERE execution_id=?", (execution_id,)
            )
            bump_revisions(
                connection,
                "execution",
                "history",
                *(["queue"] if queue_changed else []),
            )

    def recover_interrupted(self) -> int:
        with transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_staging WHERE state='executing' ORDER BY stage_order"
            ).fetchall()
            recovered = 0
            history_changed = False
            execution_changed = False
            for row in rows:
                record = connection.execute(
                    "SELECT outcome FROM execution_records WHERE execution_id=?",
                    (row["execution_id"],),
                ).fetchone()
                if record is not None and record["outcome"] is not None:
                    connection.execute(
                        "DELETE FROM execution_staging WHERE operation_id=?",
                        (row["operation_id"],),
                    )
                    execution_changed = True
                    continue
                if record is None:
                    raise StoreError(
                        "executing staging 缺少 execution record，拒绝自动恢复"
                    )
                now = utc_ms()
                connection.execute(
                    "UPDATE execution_records SET outcome='indeterminate',finished_at=? WHERE execution_id=? AND outcome IS NULL",
                    (now, row["execution_id"]),
                )
                connection.execute(
                    "UPDATE execution_staging SET state='indeterminate' WHERE operation_id=?",
                    (row["operation_id"],),
                )
                connection.execute(
                    """UPDATE execution_staging SET state='held',held_reason='blocked_by_indeterminate',
                       blocked_by_execution_id=? WHERE stage_order>? AND state='staged'""",
                    (row["execution_id"], row["stage_order"]),
                )
                recovered += 1
                history_changed = True
                execution_changed = True
            if execution_changed:
                bump_revisions(connection, "execution")
            if history_changed:
                bump_revisions(connection, "history")
            return recovered

    def resolve_indeterminate(
        self,
        operation_id: str,
        resolution: str,
        high_risk_confirmed: bool,
        new_issue_key: str | None = None,
    ) -> dict[str, Any]:
        with transaction() as connection:
            row = connection.execute(
                "SELECT * FROM execution_staging WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None or row["state"] != "indeterminate":
                raise StoreError("仅允许处置 indeterminate operation")
            if resolution not in ("succeeded", "not_applied", "abandoned"):
                raise StoreError("不支持的人工处置结论")
            is_producer = row["operation_type"] in PRODUCER_OPS
            if is_producer and resolution == "succeeded" and not is_issue_key(
                new_issue_key
            ):
                raise StoreError("创建/克隆标记为已生效时必须填写真实 issue Key")
            if not is_producer and new_issue_key is not None:
                raise StoreError("只有创建/克隆 operation 可以补录新事务 Key")
            if resolution != "succeeded" and new_issue_key is not None:
                raise StoreError("仅已生效处置可以提供新事务 Key")
            if (
                resolution == "not_applied"
                and row["submission_risk_level"] == "high"
                and not high_risk_confirmed
            ):
                raise ConfirmationRequired("高风险 operation 重新入队前需要二次确认")
            now = utc_ms()
            connection.execute(
                """UPDATE execution_records SET resolution=?,resolved_at=?,
                   new_issue_key=COALESCE(?,new_issue_key)
                   WHERE execution_id=? AND outcome='indeterminate'""",
                (resolution, now, new_issue_key, row["execution_id"]),
            )
            connection.execute(
                "DELETE FROM execution_staging WHERE operation_id=?", (operation_id,)
            )
            queue_changed = False
            if is_producer and resolution == "succeeded":
                resolved_queue, _resolved_staging = (
                    self._resolve_producer_references(
                        connection, operation_id, str(new_issue_key)
                    )
                )
                queue_changed = resolved_queue
            elif is_producer and resolution == "abandoned":
                self._terminalize_staged_dependants(connection, operation_id)
            if resolution == "not_applied":
                operation = replace(
                    operation_from_row(row), attempt_no=row["attempt_no"] + 1
                )
                self._queue.insert_queue(
                    connection,
                    operation,
                    "requeued_not_applied",
                    {"previous_execution_id": row["execution_id"]},
                )
                queue_changed = True
            bump_revisions(
                connection,
                "execution",
                "history",
                *(["queue"] if queue_changed else []),
            )
            return {
                "operation_id": operation_id,
                "execution_id": row["execution_id"],
                "resolution": resolution,
                "new_issue_key": new_issue_key,
            }

    def resolve_held(
        self, operation_ids: list[str], action: str
    ) -> dict[str, Any]:
        if not operation_ids or len(operation_ids) > 1000:
            raise StoreError("held resolution requires 1 to 1000 operation IDs")
        if len(set(operation_ids)) != len(operation_ids):
            raise StoreError("held resolution must not contain duplicate operation IDs")
        if action not in ("resume", "requeue", "abandon"):
            raise StoreError("不支持的 held 处置动作")
        with transaction() as connection:
            placeholders = ",".join("?" for _ in operation_ids)
            rows = connection.execute(
                f"SELECT * FROM execution_staging WHERE operation_id IN ({placeholders})",
                operation_ids,
            ).fetchall()
            if len(rows) != len(operation_ids) or any(
                row["state"] != "held" for row in rows
            ):
                raise StoreError("held 处置包含未知或非 held operation")
            if action == "resume":
                selected_ids = set(operation_ids)
                for row in rows:
                    operation = operation_from_row(row)
                    for dependency_id in operation.unresolved_dependency_ids():
                        state = producer_state(connection, dependency_id)
                        if state.state == "success":
                            continue
                        dependency_row = connection.execute(
                            "SELECT state,stage_order FROM execution_staging "
                            "WHERE operation_id=?",
                            (dependency_id,),
                        ).fetchone()
                        dependency_will_run_first = bool(
                            dependency_row is not None
                            and dependency_row["stage_order"] < row["stage_order"]
                            and (
                                dependency_row["state"] in {"staged", "executing"}
                                or (
                                    dependency_row["state"] == "held"
                                    and dependency_id in selected_ids
                                )
                            )
                        )
                        if dependency_will_run_first:
                            continue
                        raise StoreError(
                            "held operation 的创建/克隆依赖尚未解析；"
                            "请将生产者一并恢复且保持在其之前，"
                            "或选择重新入队: "
                            f"{dependency_id}"
                        )
            queue_changed = False
            for row in rows:
                if action == "resume":
                    connection.execute(
                        "UPDATE execution_staging SET state='staged',held_reason=NULL,blocked_by_execution_id=NULL WHERE operation_id=?",
                        (row["operation_id"],),
                    )
                    append_event(
                        connection,
                        row["operation_id"],
                        "internal",
                        "held_resumed",
                        {},
                    )
                elif action == "requeue":
                    connection.execute(
                        "DELETE FROM execution_staging WHERE operation_id=?",
                        (row["operation_id"],),
                    )
                    self._queue.insert_queue(
                        connection,
                        operation_from_row(row),
                        "held_requeued",
                        {"blocked_by_execution_id": row["blocked_by_execution_id"]},
                    )
                    queue_changed = True
                else:
                    append_event(
                        connection,
                        row["operation_id"],
                        "internal",
                        "held_abandoned",
                        {"blocked_by_execution_id": row["blocked_by_execution_id"]},
                    )
                    connection.execute(
                        "DELETE FROM execution_staging WHERE operation_id=?",
                        (row["operation_id"],),
                    )
            bump_revisions(
                connection, "execution", *(["queue"] if queue_changed else [])
            )
            return {"operation_ids": operation_ids, "action": action}

    def execution_history(
        self,
        start_at: int | None,
        end_at: int | None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        connection = open_database("operations")
        try:
            clauses = ["outcome IS NOT NULL"]
            params: list[Any] = []
            if start_at is not None:
                clauses.append("started_at>=?")
                params.append(start_at)
            if end_at is not None:
                clauses.append("started_at<?")
                params.append(end_at)
            where = " AND ".join(clauses)
            total = connection.execute(
                f"SELECT COUNT(*) FROM execution_records WHERE {where}", params
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM execution_records WHERE {where} ORDER BY started_at DESC,execution_id DESC LIMIT ? OFFSET ?",
                params + [page_size, (page - 1) * page_size],
            ).fetchall()
            return {
                "items": [execution_record_public(row) for row in rows],
                "total": int(total),
                "page": page,
                "page_size": page_size,
            }
        finally:
            connection.close()

    def read_execution_log(self, limit: int) -> list[dict[str, Any]]:
        connection = open_database("operations")
        try:
            rows = connection.execute(
                "SELECT * FROM execution_records WHERE outcome IS NOT NULL ORDER BY started_at DESC,execution_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [execution_record_public(row) for row in rows]
        finally:
            connection.close()
