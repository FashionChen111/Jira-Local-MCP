"""MCP capability surface, append responses, and protocol log persistence."""
from __future__ import annotations

import inspect
import json
import re
from types import SimpleNamespace

import anyio
import mcp.types as mcp_types
import pytest

import server
from core.storage import mcp_logs
from core.storage.database import open_database
from core.storage.mcp_logs import record_exchange


def _tool_names():
    return {tool.name for tool in server.mcp._tool_manager.list_tools()}


def test_capability_surface_has_only_v2_review_tools(cfg):
    names = _tool_names()
    assert len(names) == 18
    assert "jira_show_queue" in names
    assert "jira_get_execution_log" in names
    assert "jira_show_pending_queue" not in names
    assert "jira_get_commit_log" not in names
    assert not any(any(word in name for word in ("commit", "cancel", "resolve", "delete")) for name in names)


def _assert_complete_chinese_description(
    name,
    description,
    parameters=(),
    required_sections=(),
):
    assert isinstance(description, str) and description.strip(), name
    assert len(re.findall(r"[\u4e00-\u9fff]", description)) >= 10, name
    assert "返回：\n" in description, name
    assert not any(heading in description for heading in ("Args:\n", "Returns:\n", "Note:\n")), name
    for section in required_sections:
        assert f"{section}：\n" in description, name

    if parameters:
        assert "参数：\n" in description, name
        for parameter_name in parameters:
            assert re.search(
                rf"^\s{{4,}}{re.escape(parameter_name)}:",
                description,
                re.MULTILINE,
            ), (name, parameter_name)


def test_all_mounted_tool_docstrings_are_complete_clear_and_chinese(cfg):
    for tool in server.mcp._tool_manager.list_tools():
        parameters = inspect.signature(tool.fn).parameters
        assert tool.fn.__doc__ == tool.description, tool.name
        _assert_complete_chinese_description(
            tool.name,
            tool.description,
            parameters,
            required_sections=("副作用",),
        )


def test_all_mounted_resource_and_prompt_docstrings_are_complete_and_chinese(cfg):
    resources = server.mcp._resource_manager.list_resources()
    resource_templates = server.mcp._resource_manager.list_templates()
    prompts = server.mcp._prompt_manager.list_prompts()
    assert len(resources) == 4
    assert not resource_templates
    assert len(prompts) == 4

    for resource in resources + resource_templates:
        name = (
            str(resource.uri)
            if hasattr(resource, "uri")
            else resource.uri_template
        )
        parameters = inspect.signature(resource.fn).parameters
        assert resource.fn.__doc__ == resource.description, name
        _assert_complete_chinese_description(
            name,
            resource.description,
            parameters,
            required_sections=("边界",),
        )
    for prompt in prompts:
        parameters = inspect.signature(prompt.fn).parameters
        assert prompt.fn.__doc__ == prompt.description, prompt.name
        _assert_complete_chinese_description(
            prompt.name,
            prompt.description,
            parameters,
            required_sections=("边界",),
        )


def test_all_mounted_resource_and_prompt_content_is_nonempty_chinese(cfg):
    capabilities = (
        server.mcp._resource_manager.list_resources()
        + server.mcp._prompt_manager.list_prompts()
    )
    for capability in capabilities:
        assert not inspect.signature(capability.fn).parameters
        content = capability.fn()
        assert isinstance(content, str) and content.strip()
        assert re.search(r"[\u4e00-\u9fff]", content)


def test_append_response_exposes_only_operation_identity(queue):
    response = server.jira_comment_add_append("PROJ-1", "hello")
    assert "Operation ID: op-" in response
    assert "尚未执行" in response
    assert "hello" not in response and "描述" not in response
    assert "批次" not in response and "task" not in response.lower() and "nonce" not in response.lower()


def test_show_queue_opens_even_when_empty(cfg, monkeypatch):
    from web import app as web_app
    monkeypatch.setattr(web_app, "ensure_started", lambda: "http://127.0.0.1:8766")
    monkeypatch.setattr(web_app, "create_confirmation_url", lambda base: base + "/bootstrap/SECRET")
    monkeypatch.setattr(server.webbrowser, "open", lambda url: True)
    response = server.jira_show_queue()
    assert "0 条" in response
    assert "SECRET" not in response and "127.0.0.1" not in response


def test_execution_log_reads_terminal_records(queue):
    operation = queue.enqueue("add_comment", "PROJ-1", {"issue_key": "PROJ-1", "body": "x"}, "comment", "test")
    queue.submit(1, [operation.operation_id], False)
    claim = queue.claim_next()
    queue.complete_execution(claim.execution_id, "success", "done", None, None, 1)
    response = server.jira_get_execution_log(10)
    assert operation.operation_id in response and '"outcome": "success"' in response


def test_business_exchange_keeps_business_content_exact(cfg):
    request = '{"method":"tools/call","params":{"body":"BUSINESS_LITERAL"}}'
    response = '{"content":[{"type":"text","text":"RESPONSE_LITERAL"}]}'
    record_exchange(request, response, 100, 125, 25)
    connection = open_database("mcp_logs")
    row = connection.execute(
        """SELECT request_received_at,response_returned_at,duration_ms,
                  request_json,response_json FROM mcp_business_exchanges"""
    ).fetchone()
    connection.close()
    assert row["request_json"] == request
    assert row["response_json"] == response
    assert (row["request_received_at"], row["response_returned_at"], row["duration_ms"]) == (
        100, 125, 25,
    )


def test_business_exchange_redacts_only_configured_credentials(cfg):
    record_exchange(
        '{"method":"tools/call","params":{"body":"SECRET_TOKEN"}}',
        '{"content":[{"type":"text","text":"SECRET_TOKEN"}]}',
        100,
        125,
        25,
    )
    connection = open_database("mcp_logs")
    row = connection.execute(
        "SELECT request_json,response_json FROM mcp_business_exchanges"
    ).fetchone()
    connection.close()
    assert "SECRET_TOKEN" not in row["request_json"]
    assert "SECRET_TOKEN" not in row["response_json"]


def test_only_business_handlers_record_exact_sdk_content(cfg):
    request_types = (
        mcp_types.CallToolRequest,
        mcp_types.ReadResourceRequest,
        mcp_types.GetPromptRequest,
    )
    list_handler_calls = []

    async def business_handler(_request):
        return mcp_types.ServerResult(
            mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text="原始响应 SECRET")]
            )
        )

    async def list_handler(request):
        list_handler_calls.append(request)
        return mcp_types.ServerResult(mcp_types.ListToolsResult(tools=[]))

    handlers = {request_type: business_handler for request_type in request_types}
    handlers[mcp_types.ListToolsRequest] = list_handler
    fake_server = SimpleNamespace(
        _mcp_server=SimpleNamespace(request_handlers=handlers)
    )
    server._install_business_exchange_logging(fake_server)

    requests = (
        mcp_types.CallToolRequest.model_validate(
            {"method": "tools/call", "params": {"name": "demo", "arguments": {"secret": "原始请求 SECRET"}}}
        ),
        mcp_types.ReadResourceRequest.model_validate(
            {"method": "resources/read", "params": {"uri": "skill://demo"}}
        ),
        mcp_types.GetPromptRequest.model_validate(
            {"method": "prompts/get", "params": {"name": "demo", "arguments": {"value": "原始请求 SECRET"}}}
        ),
    )

    async def exercise() -> None:
        for request in requests:
            await handlers[type(request)](request)
        list_request = mcp_types.ListToolsRequest.model_validate(
            {"method": "tools/list"}
        )
        await handlers[mcp_types.ListToolsRequest](list_request)

    anyio.run(exercise)
    connection = open_database("mcp_logs")
    rows = connection.execute(
        """SELECT request_received_at,response_returned_at,duration_ms,
                  request_json,response_json
           FROM mcp_business_exchanges ORDER BY rowid"""
    ).fetchall()
    connection.close()
    assert len(rows) == 3
    assert [json.loads(row["request_json"])["method"] for row in rows] == [
        "tools/call", "resources/read", "prompts/get",
    ]
    assert "原始请求 SECRET" in rows[0]["request_json"]
    assert all("原始响应 SECRET" in row["response_json"] for row in rows)
    assert all(row["response_returned_at"] >= row["request_received_at"] for row in rows)
    assert all(row["duration_ms"] >= 0 for row in rows)
    assert len(list_handler_calls) == 1


def test_business_handler_records_the_error_returned_by_sdk(cfg):
    async def failing_handler(_request):
        raise ValueError("原始业务错误")

    handlers = {
        mcp_types.GetPromptRequest: failing_handler,
        mcp_types.CallToolRequest: failing_handler,
        mcp_types.ReadResourceRequest: failing_handler,
    }
    fake_server = SimpleNamespace(
        _mcp_server=SimpleNamespace(request_handlers=handlers)
    )
    server._install_business_exchange_logging(fake_server)
    request = mcp_types.GetPromptRequest.model_validate(
        {"method": "prompts/get", "params": {"name": "missing"}}
    )

    async def exercise() -> None:
        with pytest.raises(ValueError, match="原始业务错误"):
            await handlers[mcp_types.GetPromptRequest](request)

    anyio.run(exercise)
    connection = open_database("mcp_logs")
    row = connection.execute(
        "SELECT request_json,response_json FROM mcp_business_exchanges"
    ).fetchone()
    connection.close()
    assert json.loads(row["request_json"])["method"] == "prompts/get"
    assert json.loads(row["response_json"]) == {
        "code": 0,
        "message": "原始业务错误",
    }


def test_business_log_failure_is_strictly_best_effort(cfg, monkeypatch):
    def fail_transaction(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(mcp_logs, "transaction", fail_transaction)
    monkeypatch.setattr(mcp_logs, "get_logger", lambda: (_ for _ in ()).throw(RuntimeError("stderr unavailable")))
    mcp_logs.record_exchange('{"method":"tools/call"}', '{"content":[]}', 100, 125, 25)


@pytest.mark.parametrize("value", [0, -1, True, "1"])
def test_execution_log_rejects_invalid_limit(cfg, value):
    assert server.jira_get_execution_log(value).startswith("查询执行日志失败")
