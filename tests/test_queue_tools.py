"""Progressive MCP reads and dependency-closed local cancellation contracts."""

from __future__ import annotations

import json

import anyio
import pytest

import server
from core import jira_client, mcp_tools
from core.operations.repository import queue as queue_repository
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
    assert json.loads(server.jira_get_execution_log())[0]["operation_id"] == claim.operation.operation_id
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
        {"project_key": "PROJ", "issue_type": "Task", "summary": "new"}, "create", "test",
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

    def broken():
        raise RuntimeError("SECRET_TOKEN sensitive failure")

    monkeypatch.setattr(queue.query, "get_state", broken)
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
