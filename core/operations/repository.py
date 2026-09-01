"""The sole SQL and row-codec boundary for the operations database."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from core.operations.models import (
    AttachmentResult,
    DependencyBindingResult,
    DependencyState,
    ExecutionRecord,
    OperationRecord,
    Resolution,
    RiskLevel,
    StagingRecord,
)
from core.operations.rules.dependencies import (
    dependency_operation_ids,
)
from core.operations.rules import attachment_dependencies
from core.operations.rules.identifiers import is_jira_issue_key
from core.storage.database import canonical_json, new_id, parse_json, utc_ms


_OPERATION_COLUMNS = (
    "operation_id,operation_type,issue_key,target_issue_key,risk_level,"
    "payload_json,description,source,created_at,attempt_no"
)
DatabaseConnection = sqlite3.Connection


class OperationRepository:
    """Connection-scoped operations persistence; methods never commit."""

    @staticmethod
    def _payload(row: sqlite3.Row) -> dict[str, Any]:
        payload = parse_json(row["payload_json"])
        if not isinstance(payload, dict):
            raise ValueError("payload_json 必须是 JSON 对象")
        return payload

    @staticmethod
    def _attachment_result(value: str | None) -> AttachmentResult | None:
        if value is None:
            return None
        decoded = parse_json(value)
        return AttachmentResult.model_validate(decoded)

    def operation_from_row(self, row: sqlite3.Row) -> OperationRecord:
        """Decode and strictly validate a self-contained operation row."""

        return OperationRecord.model_validate(
            {
                "operation_id": row["operation_id"],
                "operation_type": row["operation_type"],
                "issue_key": row["issue_key"],
                "target_issue_key": row["target_issue_key"],
                "risk_level": row["risk_level"],
                "payload": self._payload(row),
                "description": row["description"],
                "source": row["source"],
                "created_at": row["created_at"],
                "attempt_no": row["attempt_no"],
            }
        )

    def staging_from_row(self, row: sqlite3.Row) -> StagingRecord:
        """Decode and strictly validate an active staging snapshot."""

        data = self.operation_from_row(row).model_dump(mode="python")
        data.update(
            stage_order=row["stage_order"],
            state=row["state"],
            submission_risk_level=row["submission_risk_level"],
            execution_id=row["execution_id"],
            submitted_at=row["submitted_at"],
            started_at=row["started_at"],
            held_reason=row["held_reason"],
            blocked_by_execution_id=row["blocked_by_execution_id"],
        )
        return StagingRecord.model_validate(data)

    def execution_from_row(self, row: sqlite3.Row) -> ExecutionRecord:
        """Decode and strictly validate a historical execution snapshot."""

        data = self.operation_from_row(row).model_dump(mode="python")
        data.update(
            execution_id=row["execution_id"],
            submission_risk_level=row["submission_risk_level"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            outcome=row["outcome"],
            summary=row["summary"],
            error=row["error"],
            new_issue_key=row["new_issue_key"],
            attachment_result=self._attachment_result(
                row["attachment_result_json"]
            ),
            elapsed_ms=row["elapsed_ms"],
            resolution=row["resolution"],
            resolved_at=row["resolved_at"],
        )
        return ExecutionRecord.model_validate(data)

    @staticmethod
    def _operation_values(operation: OperationRecord) -> tuple[Any, ...]:
        return (
            operation.operation_id,
            operation.operation_type,
            operation.issue_key,
            operation.target_issue_key,
            operation.risk_level,
            canonical_json(operation.payload),
            operation.description,
            operation.source,
            operation.created_at,
            operation.attempt_no,
        )

    @staticmethod
    def operation_snapshot(operation: OperationRecord) -> dict[str, Any]:
        """Serialize one event snapshot through the Pydantic domain model."""

        return operation.model_dump(mode="json")

    @staticmethod
    def revision(connection: sqlite3.Connection, name: str) -> int:
        row = connection.execute(
            "SELECT value FROM revisions WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"缺少 revision：{name}")
        return int(row[0])

    @staticmethod
    def revisions(connection: sqlite3.Connection) -> dict[str, int]:
        rows = connection.execute("SELECT name,value FROM revisions").fetchall()
        return {row["name"]: int(row["value"]) for row in rows}

    @staticmethod
    def bump_revisions(connection: sqlite3.Connection, *names: str) -> None:
        for name in set(names):
            connection.execute(
                "UPDATE revisions SET value=value+1 WHERE name=?", (name,)
            )

    @staticmethod
    def append_event(
        connection: sqlite3.Connection,
        operation_id: str,
        event_type: Literal["enqueue", "dequeue", "internal"],
        action_code: str,
        data: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO queue_events VALUES (?,?,?,?,?,?)",
            (
                new_id("event"),
                utc_ms(),
                operation_id,
                event_type,
                action_code,
                canonical_json(data),
            ),
        )

    def queue_operations(self, connection: sqlite3.Connection) -> list[OperationRecord]:
        rows = connection.execute(
            f"SELECT {_OPERATION_COLUMNS} FROM queue_items"
        ).fetchall()
        return [self.operation_from_row(row) for row in rows]

    def queue_operation(
        self, connection: sqlite3.Connection, operation_id: str
    ) -> OperationRecord | None:
        row = connection.execute(
            f"SELECT {_OPERATION_COLUMNS} FROM queue_items WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        return self.operation_from_row(row) if row is not None else None

    def insert_queue(
        self,
        connection: sqlite3.Connection,
        operation: OperationRecord,
        *,
        action_code: str,
        event_data: dict[str, Any],
    ) -> None:
        connection.execute(
            f"INSERT INTO queue_items({_OPERATION_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
            self._operation_values(operation),
        )
        self.append_event(
            connection,
            operation.operation_id,
            "enqueue",
            action_code,
            {"operation": self.operation_snapshot(operation), **event_data},
        )

    @staticmethod
    def delete_queue(
        connection: sqlite3.Connection, operation_ids: Sequence[str]
    ) -> None:
        if not operation_ids:
            return
        placeholders = ",".join("?" for _ in operation_ids)
        connection.execute(
            f"DELETE FROM queue_items WHERE operation_id IN ({placeholders})",
            tuple(operation_ids),
        )

    def staging_records(
        self,
        connection: sqlite3.Connection,
        states: Iterable[str] | None = None,
    ) -> list[StagingRecord]:
        params: tuple[str, ...] = ()
        where = ""
        if states is not None:
            state_values = tuple(states)
            if not state_values:
                return []
            placeholders = ",".join("?" for _ in state_values)
            where = f" WHERE state IN ({placeholders})"
            params = state_values
        rows = connection.execute(
            f"SELECT * FROM execution_staging{where} ORDER BY stage_order", params
        ).fetchall()
        return [self.staging_from_row(row) for row in rows]

    def staging_record(
        self, connection: sqlite3.Connection, operation_id: str
    ) -> StagingRecord | None:
        row = connection.execute(
            "SELECT * FROM execution_staging WHERE operation_id=?", (operation_id,)
        ).fetchone()
        return self.staging_from_row(row) if row is not None else None

    def staging_records_by_ids(
        self, connection: sqlite3.Connection, operation_ids: Sequence[str]
    ) -> list[StagingRecord]:
        if not operation_ids:
            return []
        placeholders = ",".join("?" for _ in operation_ids)
        rows = connection.execute(
            f"SELECT * FROM execution_staging WHERE operation_id IN ({placeholders}) ORDER BY stage_order",
            tuple(operation_ids),
        ).fetchall()
        return [self.staging_from_row(row) for row in rows]

    @staticmethod
    def maximum_stage_order(connection: sqlite3.Connection) -> int:
        return int(
            connection.execute(
                "SELECT COALESCE(MAX(stage_order),0) FROM execution_staging"
            ).fetchone()[0]
        )

    def insert_staging(
        self,
        connection: sqlite3.Connection,
        operation: OperationRecord,
        *,
        stage_order: int,
        submission_risk_level: RiskLevel,
        submitted_at: int,
    ) -> None:
        connection.execute(
            f"""INSERT INTO execution_staging(
              {_OPERATION_COLUMNS},stage_order,state,submission_risk_level,
              execution_id,submitted_at,started_at,held_reason,blocked_by_execution_id
            ) VALUES (?,?,?,?,?,?,?,?,?, ?,?,'staged',?,NULL,?,NULL,NULL,NULL)""",
            self._operation_values(operation)
            + (stage_order, submission_risk_level, submitted_at),
        )

    @staticmethod
    def delete_staging_by_operation(
        connection: sqlite3.Connection, operation_id: str
    ) -> None:
        connection.execute(
            "DELETE FROM execution_staging WHERE operation_id=?", (operation_id,)
        )

    @staticmethod
    def delete_staging_by_execution(
        connection: sqlite3.Connection, execution_id: str
    ) -> None:
        connection.execute(
            "DELETE FROM execution_staging WHERE execution_id=?", (execution_id,)
        )

    @staticmethod
    def set_staging_state(
        connection: sqlite3.Connection,
        operation_id: str,
        *,
        state: str,
        execution_id: str | None = None,
        started_at: int | None = None,
        held_reason: str | None = None,
        blocked_by_execution_id: str | None = None,
    ) -> None:
        connection.execute(
            """UPDATE execution_staging
               SET state=?,execution_id=?,started_at=?,held_reason=?,blocked_by_execution_id=?
               WHERE operation_id=?""",
            (
                state,
                execution_id,
                started_at,
                held_reason,
                blocked_by_execution_id,
                operation_id,
            ),
        )

    @staticmethod
    def mark_claimed(
        connection: sqlite3.Connection,
        operation_id: str,
        execution_id: str,
        started_at: int,
    ) -> bool:
        cursor = connection.execute(
            """UPDATE execution_staging SET state='executing',execution_id=?,started_at=?
               WHERE operation_id=? AND state='staged'""",
            (execution_id, started_at, operation_id),
        )
        return cursor.rowcount == 1

    def insert_execution(
        self,
        connection: sqlite3.Connection,
        operation: OperationRecord,
        *,
        execution_id: str,
        submission_risk_level: RiskLevel,
        started_at: int,
    ) -> None:
        connection.execute(
            """INSERT INTO execution_records(
              execution_id,operation_id,attempt_no,operation_type,issue_key,target_issue_key,
              risk_level,submission_risk_level,payload_json,description,source,created_at,started_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                execution_id,
                operation.operation_id,
                operation.attempt_no,
                operation.operation_type,
                operation.issue_key,
                operation.target_issue_key,
                operation.risk_level,
                submission_risk_level,
                canonical_json(operation.payload),
                operation.description,
                operation.source,
                operation.created_at,
                started_at,
            ),
        )

    def execution_record(
        self, connection: sqlite3.Connection, execution_id: str
    ) -> ExecutionRecord | None:
        row = connection.execute(
            "SELECT * FROM execution_records WHERE execution_id=?", (execution_id,)
        ).fetchone()
        return self.execution_from_row(row) if row is not None else None

    @staticmethod
    def finish_execution(
        connection: sqlite3.Connection,
        execution_id: str,
        *,
        finished_at: int,
        outcome: Literal["success", "failed"],
        summary: str | None,
        error: str | None,
        new_issue_key: str | None,
        attachment_result: AttachmentResult | None,
        elapsed_ms: int,
    ) -> bool:
        cursor = connection.execute(
            """UPDATE execution_records
               SET finished_at=?,outcome=?,summary=?,error=?,new_issue_key=?,
                   attachment_result_json=?,elapsed_ms=?
               WHERE execution_id=? AND outcome IS NULL""",
            (
                finished_at,
                outcome,
                summary,
                error,
                new_issue_key,
                (
                    canonical_json(attachment_result.model_dump(mode="json"))
                    if attachment_result is not None
                    else None
                ),
                elapsed_ms,
                execution_id,
            ),
        )
        return cursor.rowcount == 1

    @staticmethod
    def mark_execution_indeterminate(
        connection: sqlite3.Connection, execution_id: str, finished_at: int
    ) -> None:
        connection.execute(
            """UPDATE execution_records SET outcome='indeterminate',finished_at=?
               WHERE execution_id=? AND outcome IS NULL""",
            (finished_at, execution_id),
        )

    @staticmethod
    def hold_later_staging(
        connection: sqlite3.Connection,
        *,
        stage_order: int,
        blocked_by_execution_id: str,
    ) -> None:
        connection.execute(
            """UPDATE execution_staging
               SET state='held',held_reason='blocked_by_indeterminate',
                   blocked_by_execution_id=?
               WHERE stage_order>? AND state='staged'""",
            (blocked_by_execution_id, stage_order),
        )

    @staticmethod
    def resolve_execution(
        connection: sqlite3.Connection,
        execution_id: str,
        *,
        resolution: Resolution,
        resolved_at: int,
        new_issue_key: str | None,
        attachment_result: AttachmentResult | None,
    ) -> bool:
        attachment_json = (
            canonical_json(attachment_result.model_dump(mode="json"))
            if attachment_result is not None
            else None
        )
        cursor = connection.execute(
            """UPDATE execution_records
               SET resolution=?,resolved_at=?,new_issue_key=COALESCE(?,new_issue_key),
                   attachment_result_json=COALESCE(?,attachment_result_json)
               WHERE execution_id=? AND outcome='indeterminate' AND resolution IS NULL""",
            (
                resolution,
                resolved_at,
                new_issue_key,
                attachment_json,
                execution_id,
            ),
        )
        return cursor.rowcount == 1

    def terminalize_dependency_failure(
        self,
        connection: sqlite3.Connection,
        staging: StagingRecord,
        *,
        producer_operation_id: str,
    ) -> None:
        """Persist one dependency failure and remove its active staging row."""

        execution_id = new_id("exec")
        occurred_at = utc_ms()
        summary = (
            "未调用 Jira：依赖 operation 未产生可用结果 "
            f"({producer_operation_id})"
        )
        connection.execute(
            """INSERT INTO execution_records(
              execution_id,operation_id,attempt_no,operation_type,issue_key,target_issue_key,
              risk_level,submission_risk_level,payload_json,description,source,created_at,
              started_at,finished_at,outcome,summary,error,new_issue_key,elapsed_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'dependency_failed',?,?,NULL,0)""",
            (
                execution_id,
                staging.operation_id,
                staging.attempt_no,
                staging.operation_type,
                staging.issue_key,
                staging.target_issue_key,
                staging.risk_level,
                staging.submission_risk_level,
                canonical_json(staging.payload),
                staging.description,
                staging.source,
                staging.created_at,
                occurred_at,
                occurred_at,
                summary,
                f"dependency_failed: {producer_operation_id}",
            ),
        )
        self.append_event(
            connection,
            staging.operation_id,
            "internal",
            "dependency_failed",
            {"producer_operation_id": producer_operation_id},
        )
        self.delete_staging_by_operation(connection, staging.operation_id)

    def terminalize_dependants(
        self,
        connection: sqlite3.Connection,
        producer_operation_id: str,
    ) -> int:
        """Terminalize staged or held operations referencing one producer."""

        affected = 0
        for staging in self.staging_records(connection, {"staged", "held"}):
            if producer_operation_id not in dependency_operation_ids(staging):
                continue
            self.terminalize_dependency_failure(
                connection,
                staging,
                producer_operation_id=producer_operation_id,
            )
            affected += 1
        return affected

    def execution_history(
        self,
        connection: sqlite3.Connection,
        *,
        start_at: int | None,
        end_at: int | None,
        page: int,
        page_size: int,
    ) -> tuple[list[ExecutionRecord], int]:
        clauses = ["outcome IS NOT NULL"]
        params: list[Any] = []
        if start_at is not None:
            clauses.append("started_at>=?")
            params.append(start_at)
        if end_at is not None:
            clauses.append("started_at<?")
            params.append(end_at)
        where = " AND ".join(clauses)
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM execution_records WHERE {where}", params
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""SELECT * FROM execution_records WHERE {where}
                ORDER BY started_at DESC,execution_id DESC LIMIT ? OFFSET ?""",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()
        return [self.execution_from_row(row) for row in rows], total

    def execution_log(
        self, connection: sqlite3.Connection, limit: int
    ) -> list[ExecutionRecord]:
        rows = connection.execute(
            """SELECT * FROM execution_records WHERE outcome IS NOT NULL
               ORDER BY started_at DESC,execution_id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [self.execution_from_row(row) for row in rows]

    @staticmethod
    def total_active(connection: sqlite3.Connection) -> int:
        queued = int(connection.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0])
        staged = int(
            connection.execute("SELECT COUNT(*) FROM execution_staging").fetchone()[0]
        )
        return queued + staged

    @staticmethod
    def has_recovery_block(connection: sqlite3.Connection) -> bool:
        return (
            connection.execute(
                """SELECT 1 FROM execution_staging
                   WHERE state IN ('indeterminate','held') LIMIT 1"""
            ).fetchone()
            is not None
        )

    def dependency_states(
        self,
        connection: sqlite3.Connection,
        operation_ids: Iterable[str],
    ) -> dict[str, DependencyState]:
        """Batch-load dependency lifecycle facts with active rows taking precedence."""

        requested = tuple(dict.fromkeys(operation_ids))
        if not requested:
            return {}
        result: dict[str, DependencyState] = {}
        for chunk_start in range(0, len(requested), 400):
            chunk = requested[chunk_start : chunk_start + 400]
            placeholders = ",".join("?" for _ in chunk)
            queue_rows = connection.execute(
                f"""SELECT operation_id,operation_type,issue_key
                    FROM queue_items WHERE operation_id IN ({placeholders})""",
                chunk,
            ).fetchall()
            for row in queue_rows:
                result[row["operation_id"]] = DependencyState(
                    operation_id=row["operation_id"],
                    operation_type=row["operation_type"],
                    state="queued",
                    issue_key=row["issue_key"],
                )

            remaining = tuple(item for item in chunk if item not in result)
            if remaining:
                marks = ",".join("?" for _ in remaining)
                staging_rows = connection.execute(
                    f"""SELECT s.operation_id,s.operation_type,s.issue_key,s.state,
                               r.outcome,r.resolution,r.new_issue_key,
                               r.attachment_result_json
                        FROM execution_staging AS s
                        LEFT JOIN execution_records AS r ON r.execution_id=s.execution_id
                        WHERE s.operation_id IN ({marks})""",
                    remaining,
                ).fetchall()
                for row in staging_rows:
                    if row["outcome"] is not None:
                        result[row["operation_id"]] = self._dependency_state_from_result(
                            row["operation_id"], row
                        )
                    else:
                        result[row["operation_id"]] = DependencyState(
                            operation_id=row["operation_id"],
                            operation_type=row["operation_type"],
                            state=row["state"],
                            issue_key=row["issue_key"],
                        )

            remaining = tuple(item for item in chunk if item not in result)
            if remaining:
                marks = ",".join("?" for _ in remaining)
                history_rows = connection.execute(
                    f"""SELECT operation_id,operation_type,issue_key,outcome,resolution,
                               new_issue_key,attachment_result_json,
                               attempt_no,started_at,execution_id
                        FROM execution_records WHERE operation_id IN ({marks})
                        ORDER BY attempt_no DESC,started_at DESC,execution_id DESC""",
                    remaining,
                ).fetchall()
                for row in history_rows:
                    if row["operation_id"] not in result:
                        result[row["operation_id"]] = self._dependency_state_from_result(
                            row["operation_id"], row
                        )

        for operation_id in requested:
            result.setdefault(
                operation_id,
                DependencyState(
                    operation_id=operation_id,
                    operation_type=None,
                    state="unknown",
                ),
            )
        return result

    def producer_states(
        self,
        connection: sqlite3.Connection,
        operation_ids: Iterable[str],
    ) -> dict[str, DependencyState]:
        """Compatibility wrapper for the former issue-only state loader."""

        return self.dependency_states(connection, operation_ids)

    @staticmethod
    def _dependency_state_from_result(
        operation_id: str, row: sqlite3.Row
    ) -> DependencyState:
        outcome = row["outcome"]
        resolution = row["resolution"]
        if outcome == "success" or (
            outcome == "indeterminate" and resolution == "succeeded"
        ):
            state = "success"
        elif outcome == "indeterminate" and resolution == "not_applied":
            state = "retry_pending"
        elif outcome == "indeterminate" and resolution == "abandoned":
            state = "abandoned"
        elif outcome == "indeterminate":
            state = "indeterminate"
        else:
            state = outcome or "executing"
        return DependencyState(
            operation_id=operation_id,
            operation_type=row["operation_type"],
            state=state,
            issue_key=row["issue_key"],
            new_issue_key=row["new_issue_key"],
            attachment_result=OperationRepository._attachment_result(
                row["attachment_result_json"]
            ),
        )

    def broadcast_issue_resolution(
        self,
        connection: sqlite3.Connection,
        *,
        producer_operation_id: str,
        new_issue_key: str,
    ) -> DependencyBindingResult:
        """Broadcast a producer result to both explicit active target columns."""

        if not is_jira_issue_key(new_issue_key):
            raise ValueError("生产者结果必须包含合法 Jira Key")
        queue = self._matching_target_fields(
            connection, "queue_items", producer_operation_id
        )
        staging = self._matching_target_fields(
            connection, "execution_staging", producer_operation_id
        )
        for table, matched, location in (
            ("queue_items", queue, "queue"),
            ("execution_staging", staging, "staging"),
        ):
            connection.execute(
                f"UPDATE {table} SET issue_key=? WHERE issue_key=?",
                (new_issue_key, producer_operation_id),
            )
            connection.execute(
                f"""UPDATE {table} SET target_issue_key=?
                    WHERE operation_type='add_issuelink' AND target_issue_key=?""",
                (new_issue_key, producer_operation_id),
            )
            for operation_id, fields in matched.items():
                self.append_event(
                    connection,
                    operation_id,
                    "internal",
                    "issue_key_resolved",
                    {
                        "producer_operation_id": producer_operation_id,
                        "new_issue_key": new_issue_key,
                        "fields": fields,
                        "location": location,
                    },
                )
        return DependencyBindingResult(
            queue_operation_ids=tuple(queue),
            staging_operation_ids=tuple(staging),
        )

    def bind_produced_issue(
        self,
        connection: sqlite3.Connection,
        *,
        producer_operation_id: str,
        new_issue_key: str,
    ) -> DependencyBindingResult:
        """Compatibility wrapper for the renamed issue-resolution broadcast."""

        return self.broadcast_issue_resolution(
            connection,
            producer_operation_id=producer_operation_id,
            new_issue_key=new_issue_key,
        )

    def broadcast_attachment_resolution(
        self,
        connection: sqlite3.Connection,
        *,
        producer_operation_id: str,
        attachment_result: AttachmentResult,
    ) -> DependencyBindingResult:
        """Materialize one upload result in all queued and staged consumers."""

        queue_ids: list[str] = []
        staging_ids: list[str] = []
        for table, records, location, target_ids in (
            (
                "queue_items",
                self.queue_operations(connection),
                "queue",
                queue_ids,
            ),
            (
                "execution_staging",
                self.staging_records(connection),
                "staging",
                staging_ids,
            ),
        ):
            for record in records:
                refs = tuple(
                    reference
                    for reference in attachment_dependencies.references(record)
                    if reference.operation_id == producer_operation_id
                )
                if not refs:
                    continue
                materialized = attachment_dependencies.materialize_references(
                    record.as_operation(),
                    {producer_operation_id: attachment_result},
                )
                connection.execute(
                    f"UPDATE {table} SET payload_json=? WHERE operation_id=?",
                    (canonical_json(materialized.payload), record.operation_id),
                )
                paths = tuple(dict.fromkeys(reference.path for reference in refs))
                target_ids.append(record.operation_id)
                self.append_event(
                    connection,
                    record.operation_id,
                    "internal",
                    "attachment_resolved",
                    {
                        "producer_operation_id": producer_operation_id,
                        "attachment_result": attachment_result.model_dump(mode="json"),
                        "paths": paths,
                        "location": location,
                    },
                )
        return DependencyBindingResult(
            queue_operation_ids=tuple(queue_ids),
            staging_operation_ids=tuple(staging_ids),
        )

    @staticmethod
    def _matching_target_fields(
        connection: sqlite3.Connection, table: str, producer_operation_id: str
    ) -> dict[str, list[str]]:
        rows = connection.execute(
            f"""SELECT operation_id,operation_type,issue_key,target_issue_key
                FROM {table}
                WHERE issue_key=? OR (
                    operation_type='add_issuelink' AND target_issue_key=?
                ) ORDER BY created_at,operation_id""",
            (producer_operation_id, producer_operation_id),
        ).fetchall()
        matched: dict[str, list[str]] = {}
        for row in rows:
            fields: list[str] = []
            if row["issue_key"] == producer_operation_id:
                fields.append("issue_key")
            if (
                row["operation_type"] == "add_issuelink"
                and row["target_issue_key"] == producer_operation_id
            ):
                fields.append("target_issue_key")
            matched[row["operation_id"]] = fields
        return matched
