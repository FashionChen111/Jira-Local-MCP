"""Progressive MCP reads and dependency-closed local cancellation contracts."""

from __future__ import annotations

import json

import anyio
import pytest

import server
from core import jira_client, mcp_tools
from core.operations.repository import queue as queue_repository
from core.operations.repository import execution as execution_repository
from core.operations.repository import metadata as repository_metadata
from core.operations.dependencies import orchestration as dependency_orchestration
from core.storage.database import open_database


@pytest.fixture(autouse=True)
def forbid_jira_and_browser(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Local queue tools must not access Jira or start Web")

    monkeypatch.setattr(jira_client, "get_client", unexpected)
    from web import app as web_app
    monkeypatch.setattr(web_app, "ensure_started", unexpected)


def _comment(queue, key="PROJ-1", body="body"):
    return queue.queue.enqueue("add_comment", key, {"body": body}, "comment", "test")


def _stored_state():
    connection = open_database("operations")
    try:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in ("queue_items", "queue_events", "execution_staging", "revisions")
        }
    finally:
        connection.close()


def test_empty_snapshots_and_missing_detail(queue):
    before = _stored_state()
    for read in (mcp_tools.jira_list_queue, mcp_tools.jira_list_execution_state):
        assert json.loads(read()) == {
            "items": [], "total": 0, "page": 1, "page_size": 50,
            "revisions": {"queue": 0, "execution": 0, "history": 0},
        }
    missing = "op-" + "f" * 32
    details = json.loads(mcp_tools.jira_get_queue_operations([missing]))
    assert details["items"] == []
    assert details["missing_operation_ids"] == [missing]
    assert _stored_state() == before


def test_queue_pages_follow_web_order_and_details_preserve_records(queue, web_client):
    for index in range(55, 0, -1):
        _comment(queue, f"PROJ-{index}", "完整正文\n" * 3000)
    queue.queue.enqueue(
        "add_issuelink", "PROJ-1", {"link_type": "Relates"}, "link", "test", "PROJ-2",
    )
    web_state = web_client.get("/api/state").json()
    expected = [op for group in web_state["queue"]["groups"] for op in group["operations"]]
    expected.extend(web_state["queue"]["standalone"])
    first = json.loads(mcp_tools.jira_list_queue())
    second = json.loads(mcp_tools.jira_list_queue(page=2))
    assert first["total"] == second["total"] == 56
    assert len(first["items"]) == 50 and len(second["items"]) == 6
    assert first["revisions"] == second["revisions"] == web_state["revisions"]
    assert [op["operation_id"] for op in first["items"] + second["items"]] == [
        op["operation_id"] for op in expected
    ]
    for overview, detail in zip(first["items"] + second["items"], expected):
        assert set(overview) == {
            "operation_id", "operation_type", "issue_key", "target_issue_key",
            "description", "risk_level", "created_at",
        }
        assert all(detail[key] == value for key, value in overview.items())
    missing = "op-" + "f" * 32
    ids = [expected[-1]["operation_id"], missing, expected[0]["operation_id"]]
    details = json.loads(mcp_tools.jira_get_queue_operations(ids))
    assert details["items"] == [expected[-1], expected[0]]
    assert details["missing_operation_ids"] == [missing]
    assert details["items"][1]["payload"]["body"] == "完整正文\n" * 3000
    assert json.loads(mcp_tools.jira_list_queue(page=3))["items"] == []
    assert len(json.loads(mcp_tools.jira_list_queue(page_size=200))["items"]) == 56


@pytest.mark.parametrize("page,page_size", [(0, 50), (-1, 50), (True, 50), (1, 0), (1, 201), (1, False)])
def test_invalid_pagination_returns_json_without_mutation(queue, page, page_size):
    before = _stored_state()
    for read in (mcp_tools.jira_list_queue, mcp_tools.jira_list_execution_state):
        assert json.loads(read(page, page_size))["code"] == "invalid_request"
    assert _stored_state() == before


@pytest.mark.parametrize("ids", [[], ["bad"], ["op-" + "g" * 32], ["op-" + "a" * 32] * 2])
def test_invalid_id_lists_do_not_change_queue(queue, ids):
    _comment(queue)
    before = _stored_state()
    assert json.loads(mcp_tools.jira_get_queue_operations(ids))["code"] == "invalid_request"
    assert json.loads(mcp_tools.jira_cancel_queue_operations(ids, 1))["code"] == "state_conflict"
    assert _stored_state() == before


def test_batch_limits_and_revision_validation(queue):
    before = _stored_state()
    ids = [f"op-{index:032x}" for index in range(1001)]
    assert len(json.loads(mcp_tools.jira_get_queue_operations(ids[:200]))["missing_operation_ids"]) == 200
    assert json.loads(mcp_tools.jira_get_queue_operations(ids[:201]))["code"] == "invalid_request"
    assert json.loads(mcp_tools.jira_cancel_queue_operations(ids, 0))["code"] == "state_conflict"
    for revision in (-1, True):
        assert json.loads(mcp_tools.jira_cancel_queue_operations(ids[:1], revision))["code"] == "state_conflict"
    assert _stored_state() == before


def test_snapshot_keeps_records_and_revisions_together_during_submission(queue, monkeypatch):
    operation = _comment(queue)
    original = queue_repository.queue_operations
    submitted = False

    def read_then_submit(connection):
        nonlocal submitted
        records = original(connection)
        assert connection.in_transaction
        if not submitted:
            submitted = True
            # A separate connection commits after this reader established its snapshot.
            queue.submission.submit(1, [operation.operation_id], False)
        return records

    monkeypatch.setattr(queue_repository, "queue_operations", read_then_submit)
    snapshot = queue.query.get_state()
    assert snapshot.revisions == {"queue": 1, "execution": 0, "history": 0}
    assert snapshot.queue.groups[0].operations[0].operation_id == operation.operation_id
    assert not snapshot.execution.staged
    current = queue.query.get_state()
    assert current.revisions["queue"] == 2
    assert current.execution.staged[0].operation_id == operation.operation_id
    assert not current.queue.groups


def test_execution_snapshots_cover_lifecycle_without_claiming_success(queue):
    operations = [_comment(queue, f"PROJ-{index}") for index in (3, 2, 1)]
    ids, _ = queue.submission.submit(3, [op.operation_id for op in operations], False)
    staged = json.loads(mcp_tools.jira_list_execution_state(page_size=2))
    assert staged["total"] == 3
    assert [item["operation_id"] for item in staged["items"]] == ids[:2]
    assert all(item["state"] == "staged" and "payload" not in item for item in staged["items"])
    assert json.loads(mcp_tools.jira_list_execution_state(page=2, page_size=2))["items"][0]["operation_id"] == ids[2]
    claim = queue.execution.claim_next()
    assert claim is not None
    active = json.loads(mcp_tools.jira_list_execution_state())
    assert [item["state"] for item in active["items"]] == ["executing", "staged", "staged"]
    assert active["items"][0]["execution_id"] == claim.execution_id
    queue.execution.complete_execution(claim.execution_id, "success", "done", None, None, 1)
    remaining = json.loads(mcp_tools.jira_list_execution_state())
    assert remaining["total"] == 2
    assert remaining["revisions"]["execution"] > active["revisions"]["execution"]
    assert json.loads(mcp_tools.jira_get_execution_log())[0]["operation_id"] == claim.operation.operation_id
    assert queue.execution.claim_next() is not None
    queue.execution.recover_interrupted()
    interrupted = json.loads(mcp_tools.jira_list_execution_state())
    assert [item["state"] for item in interrupted["items"]] == ["indeterminate", "held"]
    assert interrupted["items"][1]["held_reason"]
    assert json.loads(mcp_tools.jira_get_queue_operations(ids))["missing_operation_ids"] == ids
    before = _stored_state()
    rejected = json.loads(mcp_tools.jira_cancel_queue_operations(ids, interrupted["revisions"]["queue"]))
    assert rejected["code"] == "state_conflict"
    assert _stored_state() == before


def test_cancel_requires_transitive_issue_and_attachment_scope(queue, tmp_path, web_client):
    producer = queue.queue.enqueue(
        "create_issue", None,
        {"project_key": "PROJ", "issue_type_id": "10001", "summary": "new"}, "create", "test",
    )
    path = tmp_path / "attachment.txt"
    path.write_text("attachment", encoding="utf-8")
    attachment = queue.queue.enqueue(
        "add_attachment", producer.operation_id,
        {"file_path": str(path), "file_name": None}, "upload", "test",
    )
    child = _comment(queue, producer.operation_id, f"[^{{{{attachment:{attachment.operation_id}}}}}]")
    unrelated = _comment(queue, "PROJ-9")
    before = _stored_state()
    expected = {producer.operation_id, attachment.operation_id, child.operation_id}
    response = json.loads(mcp_tools.jira_cancel_queue_operations([producer.operation_id], 4))
    assert response["code"] == "dependency_scope_incomplete"
    assert set(response["affected_operation_ids"]) == expected
    assert set(response["missing_operation_ids"]) == {attachment.operation_id, child.operation_id}
    assert _stored_state() == before
    web_response = web_client.post("/api/queue/cancel", json={
        "operation_ids": [producer.operation_id], "expected_queue_revision": 4,
    })
    assert web_response.status_code == 409 and web_response.json() == response
    # Selecting an attachment alone must include its consumers, but not its own producer.
    response = json.loads(mcp_tools.jira_cancel_queue_operations([attachment.operation_id], 4))
    assert set(response["affected_operation_ids"]) == {attachment.operation_id, child.operation_id}
    assert response["missing_operation_ids"] == [child.operation_id]
    assert _stored_state() == before
    response = json.loads(mcp_tools.jira_cancel_queue_operations(list(expected), 4))
    assert set(response["cancelled_operation_ids"]) == expected
    assert response["queue_revision"] == 5
    assert json.loads(mcp_tools.jira_list_queue())["items"][0]["operation_id"] == unrelated.operation_id
    connection = open_database("operations")
    try:
        events = connection.execute("SELECT * FROM queue_events WHERE event_type='dequeue'").fetchall()
        assert len(events) == 3
        assert all(set(json.loads(event["data_json"])["requested_operation_ids"]) == expected for event in events)
    finally:
        connection.close()


def test_cancel_stale_mixed_and_repeat_requests_never_partially_write(queue):
    first = _comment(queue)
    second = _comment(queue)
    before = _stored_state()
    assert json.loads(mcp_tools.jira_cancel_queue_operations([first.operation_id], 1))["code"] == "revision_conflict"
    assert _stored_state() == before
    assert json.loads(mcp_tools.jira_cancel_queue_operations([first.operation_id, "op-" + "f" * 32], 2))["code"] == "state_conflict"
    assert _stored_state() == before
    queue.submission.submit(2, [second.operation_id], False)
    before = _stored_state()
    assert json.loads(mcp_tools.jira_cancel_queue_operations([first.operation_id, second.operation_id], 3))["code"] == "state_conflict"
    assert _stored_state() == before
    assert json.loads(mcp_tools.jira_cancel_queue_operations([first.operation_id], 3))["cancelled_operation_ids"] == [first.operation_id]
    before = _stored_state()
    assert json.loads(mcp_tools.jira_cancel_queue_operations([first.operation_id], 4))["code"] == "state_conflict"
    assert _stored_state() == before


def test_mcp_output_redacts_credentials_and_internal_errors(queue, monkeypatch):
    operation = _comment(queue, body="SECRET_TOKEN")
    serialized = mcp_tools.jira_get_queue_operations([operation.operation_id])
    assert "SECRET_TOKEN" not in serialized
    assert json.loads(serialized)["items"]

    def broken(*_args):
        raise RuntimeError("SECRET_TOKEN sensitive failure")

    monkeypatch.setattr(queue.query, "list_queue", broken)
    result = mcp_tools.jira_list_queue()
    assert "SECRET_TOKEN" not in result and "Traceback" not in result
    assert json.loads(result)["code"] == "internal_error"


def test_cancel_database_failure_rolls_back_events_and_all_rows(queue):
    operations = [_comment(queue), _comment(queue)]
    connection = open_database("operations")
    try:
        connection.execute("""
            CREATE TRIGGER reject_cancel BEFORE DELETE ON queue_items
            BEGIN SELECT RAISE(ABORT, 'forced cancellation failure'); END
        """)
    finally:
        connection.close()
    before = _stored_state()
    result = json.loads(mcp_tools.jira_cancel_queue_operations(
        [operation.operation_id for operation in operations], 2,
    ))
    assert result["code"] == "internal_error"
    assert _stored_state() == before


def test_registered_queue_schemas_and_protocol_results(queue):
    async def inspect_protocol():
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        for name in ("jira_list_queue", "jira_list_execution_state"):
            fields = tools[name].inputSchema["properties"]
            assert fields["page"]["minimum"] == 1
            assert fields["page_size"]["maximum"] == 200
            assert fields["page_size"]["default"] == 50
        cancel = tools["jira_cancel_queue_operations"].inputSchema
        assert set(cancel["properties"]) == {"operation_ids", "expected_queue_revision"}
        assert cancel["properties"]["operation_ids"]["maxItems"] == 1000
        assert tools["jira_get_queue_operations"].inputSchema["properties"]["operation_ids"]["maxItems"] == 200
        result = await server.mcp.call_tool("jira_list_queue", {})
        assert isinstance(result, tuple)
        content, structured = result
        assert isinstance(content, list)
        assert isinstance(structured, dict)
        assert content[0].type == "text"
        assert json.loads(content[0].text)["total"] == 0
        assert json.loads(structured["result"])["total"] == 0

    anyio.run(inspect_protocol)


@pytest.mark.parametrize("read_kind", ["queue", "details", "execution"])
def test_partial_reads_keep_their_own_transaction_snapshot(queue, monkeypatch, read_kind):
    operation = _comment(queue)
    if read_kind == "execution":
        queue.submission.submit(1, [operation.operation_id], False)
    original = repository_metadata.revisions
    changed = False

    def revisions_after_concurrent_write(connection):
        nonlocal changed
        assert connection.in_transaction
        if not changed:
            changed = True
            if read_kind == "execution":
                assert queue.execution.claim_next() is not None
            else:
                queue.submission.submit(1, [operation.operation_id], False)
        return original(connection)

    monkeypatch.setattr(repository_metadata, "revisions", revisions_after_concurrent_write)
    if read_kind == "execution":
        result = queue.query.list_execution_state()
        assert result.total == 1
        assert result.items[0].state == "staged"
        assert result.revisions == {"queue": 2, "execution": 1, "history": 0}
        assert queue.query.list_execution_state().items[0].state == "executing"
    else:
        result = (
            queue.query.list_queue() if read_kind == "queue"
            else queue.query.get_queue_operations([operation.operation_id])
        )
        assert result.items[0].operation_id == operation.operation_id
        assert result.revisions == {"queue": 1, "execution": 0, "history": 0}
        assert not queue.query.get_queue_operations([operation.operation_id]).items


@pytest.mark.parametrize("read_kind", ["queue", "details", "execution"])
def test_partial_reads_prepare_only_requested_public_records(queue, monkeypatch, read_kind):
    operations = [_comment(queue, f"PROJ-{index}") for index in range(1, 7)]
    staged_ids, _ = queue.submission.submit(
        6, [item.operation_id for item in operations[:3]], False,
    )
    inspected: list[str] = []
    inspect_original = dependency_orchestration.inspect_dependencies

    def inspect(record):
        inspected.append(record.operation_id)
        return inspect_original(record)

    def forbidden(*_args, **_kwargs):
        pytest.fail("A partial query loaded unrelated full state")

    monkeypatch.setattr(dependency_orchestration, "inspect_dependencies", inspect)
    monkeypatch.setattr(queue.query, "get_state", forbidden)
    monkeypatch.setattr(execution_repository, "staging_records", forbidden)
    if read_kind != "queue":
        monkeypatch.setattr(queue_repository, "queue_operations", forbidden)
    if read_kind == "queue":
        result = queue.query.list_queue(page=2, page_size=1)
        assert result.total == 3
        assert [item.operation_id for item in result.items] == [operations[4].operation_id]
    elif read_kind == "details":
        ids = [operations[5].operation_id, operations[0].operation_id, operations[3].operation_id]
        result = queue.query.get_queue_operations(ids)
        assert [item.operation_id for item in result.items] == [ids[0], ids[2]]
        assert result.missing_operation_ids == (ids[1],)
    else:
        result = queue.query.list_execution_state(page=2, page_size=1)
        assert result.total == 3
        assert [item.operation_id for item in result.items] == [staged_ids[1]]
    assert set(inspected) == {item.operation_id for item in result.items}
    assert len(inspected) == len(result.items)


def test_partial_detail_dependency_facts_share_snapshot(queue, monkeypatch):
    producer = queue.queue.enqueue(
        "create_issue", None,
        {"project_key": "PROJ", "issue_type_id": "10001", "summary": "new"}, "create", "test",
    )
    child = _comment(queue, producer.operation_id)
    original = queue_repository.queue_operations_by_ids

    def read_then_submit(connection, ids):
        records = original(connection, ids)
        queue.submission.submit(2, [producer.operation_id, child.operation_id], False)
        return records

    monkeypatch.setattr(queue_repository, "queue_operations_by_ids", read_then_submit)
    result = queue.query.get_queue_operations([child.operation_id])
    assert result.items[0].dependency_states == {producer.operation_id: "queued"}
    assert result.revisions == {"queue": 2, "execution": 0, "history": 0}


def test_repository_pages_decode_only_selected_rows(queue):
    operations = [_comment(queue, f"PROJ-{index}") for index in (1, 2, 3)]
    ids, _ = queue.submission.submit(3, [op.operation_id for op in operations], False)
    queued = [_comment(queue), _comment(queue)]
    connection = open_database("operations")
    try:
        # Unrelated payload corruption must not be decoded by targeted SQL queries.
        connection.execute(
            "UPDATE execution_staging SET payload_json='[]' WHERE operation_id=?", (ids[0],),
        )
        connection.execute(
            "UPDATE queue_items SET payload_json='[]' WHERE operation_id=?", (queued[0].operation_id,),
        )
    finally:
        connection.close()
    result = queue.query.list_execution_state(page=2, page_size=1)
    assert result.total == 3 and result.items[0].operation_id == ids[1]
    assert not queue.query.list_execution_state(page=4, page_size=1).items
    assert queue.query.get_queue_operations([queued[1].operation_id]).items[0].operation_id == queued[1].operation_id


def test_execution_page_count_and_rows_share_snapshot(queue, monkeypatch):
    operation = _comment(queue)
    queue.submission.submit(1, [operation.operation_id], False)
    claim = queue.execution.claim_next()
    assert claim is not None
    from core.operations.services import query as query_module
    original = query_module.open_database
    changed = False

    def open_with_concurrent_completion(kind):
        connection = original(kind)

        def before_select(statement):
            nonlocal changed
            if statement.startswith("SELECT * FROM execution_staging ORDER BY") and not changed:
                changed = True
                queue.execution.complete_execution(claim.execution_id, "success", "done", None, None, 1)

        connection.set_trace_callback(before_select)
        return connection

    monkeypatch.setattr(query_module, "open_database", open_with_concurrent_completion)
    result = queue.query.list_execution_state()
    assert changed and result.total == 1 and len(result.items) == 1
    assert result.items[0].state == "executing"
    assert not queue.query.list_execution_state().items
