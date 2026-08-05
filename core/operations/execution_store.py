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
    parse_json,
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
            if (
                row["operation_type"] in ("update_field", "update_custom_field")
                and row["issue_key"]
            ):
                counts[row["issue_key"]] = counts.get(row["issue_key"], 0) + 1
        hot = {key for key, count in counts.items() if count >= 5}
        for row in rows:
            if (
                row["operation_type"] in ("update_field", "update_custom_field")
                and row["issue_key"] in hot
            ):
                risks[row["operation_id"]] = "high"
        return risks

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
            payload = parse_json(row["payload_json"])
            risk_engine.assert_not_delete(
                row["operation_type"], payload  # type: ignore[arg-type]
            )
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
                      operation_id,operation_type,issue_key,risk_level,payload_json,description,source,created_at,
                      attempt_no,stage_order,state,submission_risk_level,execution_id,submitted_at,started_at,
                      held_reason,blocked_by_execution_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,'staged',?,NULL,?,NULL,NULL,NULL)""",
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

    def claim_next(self) -> ExecutionClaim | None:
        with transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM execution_staging WHERE state='indeterminate' LIMIT 1"
            ).fetchone():
                return None
            row = connection.execute(
                "SELECT * FROM execution_staging ORDER BY stage_order LIMIT 1"
            ).fetchone()
            if row is None or row["state"] != "staged":
                return None
            execution_id = new_id("exec")
            started_at = utc_ms()
            connection.execute(
                "UPDATE execution_staging SET state='executing',execution_id=?,started_at=? WHERE operation_id=? AND state='staged'",
                (execution_id, started_at, row["operation_id"]),
            )
            operation = operation_from_row(row)
            connection.execute(
                """INSERT INTO execution_records(
                  execution_id,operation_id,attempt_no,operation_type,issue_key,risk_level,submission_risk_level,
                  payload_json,description,source,created_at,started_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    execution_id,
                    operation.operation_id,
                    operation.attempt_no,
                    operation.operation_type,
                    operation.issue_key,
                    operation.risk_level,
                    row["submission_risk_level"],
                    canonical_json(operation.payload),
                    operation.description,
                    operation.source,
                    operation.created_at,
                    started_at,
                ),
            )
            bump_revisions(connection, "execution")
            return ExecutionClaim(
                execution_id=execution_id,
                operation=operation,
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
                "SELECT operation_id,outcome FROM execution_records WHERE execution_id=?",
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
            connection.execute(
                "DELETE FROM execution_staging WHERE execution_id=?", (execution_id,)
            )
            bump_revisions(connection, "execution", "history")

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
        self, operation_id: str, resolution: str, high_risk_confirmed: bool
    ) -> dict[str, Any]:
        with transaction() as connection:
            row = connection.execute(
                "SELECT * FROM execution_staging WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None or row["state"] != "indeterminate":
                raise StoreError("仅允许处置 indeterminate operation")
            if resolution not in ("succeeded", "not_applied", "abandoned"):
                raise StoreError("不支持的人工处置结论")
            if (
                resolution == "not_applied"
                and row["submission_risk_level"] == "high"
                and not high_risk_confirmed
            ):
                raise ConfirmationRequired("高风险 operation 重新入队前需要二次确认")
            now = utc_ms()
            connection.execute(
                "UPDATE execution_records SET resolution=?,resolved_at=? WHERE execution_id=? AND outcome='indeterminate'",
                (resolution, now, row["execution_id"]),
            )
            connection.execute(
                "DELETE FROM execution_staging WHERE operation_id=?", (operation_id,)
            )
            queue_changed = False
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
