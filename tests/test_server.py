"""MCP 确认页工具测试。"""
from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import server
from web import app as web_app


def test_show_pending_queue_only_opens_all_batches(queue, monkeypatch):
    task = queue.enqueue(
        "add_comment",
        "PROJ-1",
        {"issue_key": "PROJ-1", "body": "x"},
        "d",
    )
    opened: list[str] = []
    monkeypatch.setattr(web_app, "ensure_started", lambda: "http://127.0.0.1:8766")
    monkeypatch.setattr(
        web_app,
        "create_confirmation_url",
        lambda base: f"{base}/bootstrap/private-capability",
    )
    monkeypatch.setattr(web_app, "confirmation_session_ttl_minutes", lambda: 23)
    monkeypatch.setattr(server.webbrowser, "open", opened.append)

    result = server.jira_show_pending_queue()

    assert list(inspect.signature(server.jira_show_pending_queue).parameters) == []
    assert queue.get_batch(task.batch_id).state == "presented"
    assert opened == ["http://127.0.0.1:8766/bootstrap/private-capability"]
    assert "127.0.0.1" not in result
    assert "private-capability" not in result
    assert "已在默认浏览器打开确认页面" in result
    assert "页面打开后 23 分钟内有效" in result


def test_show_pending_queue_reports_browser_open_failure_without_leaking_url(
    queue, monkeypatch
):
    queue.enqueue(
        "add_comment",
        "PROJ-1",
        {"issue_key": "PROJ-1", "body": "x"},
        "d",
    )
    private_url = "http://127.0.0.1:8766/bootstrap/private-capability"
    monkeypatch.setattr(web_app, "ensure_started", lambda: "http://127.0.0.1:8766")
    monkeypatch.setattr(web_app, "create_confirmation_url", lambda _base: private_url)
    monkeypatch.setattr(server.webbrowser, "open", lambda _url: False)

    result = server.jira_show_pending_queue()

    assert result.startswith("打开确认页面失败")
    assert private_url not in result


def test_limit_tools_publish_positive_unbounded_schema():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}

    for name in (
        "jira_query_comments",
        "jira_query_worklogs",
        "jira_get_commit_log",
    ):
        limit_schema = tools[name].inputSchema["properties"]["limit"]
        assert limit_schema["minimum"] == 1
        assert "maximum" not in limit_schema


@pytest.mark.parametrize("limit", [0, -1, True, False])
@pytest.mark.parametrize(
    ("tool", "args", "error_prefix"),
    [
        (server.jira_query_comments, ("PROJ-1",), "查询评论失败"),
        (server.jira_query_worklogs, ("PROJ-1",), "查询工作日志失败"),
        (server.jira_get_commit_log, (), "查询提交日志失败"),
    ],
)
def test_limit_tools_reject_invalid_values_before_jira_call(
    queue, monkeypatch, limit, tool, args, error_prefix
):
    get_client = MagicMock()
    monkeypatch.setattr(server.jira_client, "get_client", get_client)

    result = tool(*args, limit=limit)

    assert result.startswith(error_prefix)
    assert "limit 必须是大于等于 1 的整数" in result
    get_client.assert_not_called()


def test_jira_query_limits_above_available_return_all(queue, monkeypatch):
    client = MagicMock()
    client.comments.return_value = [
        SimpleNamespace(
            author=SimpleNamespace(displayName=f"user-{i}"),
            created=f"created-{i}",
            body=f"comment-{i}",
        )
        for i in range(16)
    ]
    client.worklogs.return_value = [
        SimpleNamespace(
            author=SimpleNamespace(displayName=f"user-{i}"),
            started=f"started-{i}",
            timeSpent=f"{i + 1}m",
            comment=f"worklog-{i}",
        )
        for i in range(16)
    ]
    monkeypatch.setattr(server.jira_client, "get_client", lambda: client)

    comments = json.loads(server.jira_query_comments("PROJ-1", limit=300))
    worklogs = json.loads(server.jira_query_worklogs("PROJ-1", limit=300))

    assert len(comments) == 16
    assert len(worklogs) == 16


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (server.jira_query_issue, ("PROJ-1",)),
        (server.jira_query_comments, ("PROJ-1",)),
        (server.jira_query_worklogs, ("PROJ-1",)),
        (server.jira_query_attachments, ("PROJ-1",)),
        (server.jira_jql_search, ("project = PROJ",)),
    ],
)
def test_jira_query_errors_redact_configured_sensitive_values(
    cfg, monkeypatch, tool, args
):
    cfg.jira.username = "sensitive-user"
    cfg.jira.password = "sensitive-password"
    detail = (
        "HTTP 401 diagnostic detail "
        "url=https://jira.example.com/rest/api/2/issue "
        "user=sensitive-user password=sensitive-password token=SECRET_TOKEN"
    )
    client = MagicMock()
    client.issue.side_effect = RuntimeError(detail)
    client.comments.side_effect = RuntimeError(detail)
    client.worklogs.side_effect = RuntimeError(detail)
    client.search_issues.side_effect = RuntimeError(detail)
    monkeypatch.setattr(server.jira_client, "get_client", lambda: client)

    result = tool(*args)

    assert "RuntimeError" in result
    assert "HTTP 401 diagnostic detail" in result
    assert "rest/api/2/issue" in result
    assert "***" in result
    for sensitive in cfg.sensitive_values():
        assert sensitive not in result


def test_get_commit_log_redacts_configured_sensitive_values_on_read(
    cfg, monkeypatch
):
    cfg.jira.username = "sensitive-user"
    cfg.jira.password = "sensitive-password"
    logs = [{
        "status": "failed",
        "error": (
            "JIRAError HTTP 403 url=https://jira.example.com/rest/api/2 "
            "user=sensitive-user password=sensitive-password token=SECRET_TOKEN"
        ),
    }]
    monkeypatch.setattr(server.jira_executor, "read_commit_log", lambda _limit: logs)

    result = server.jira_get_commit_log()

    assert "JIRAError HTTP 403" in result
    assert "rest/api/2" in result
    assert "***" in result
    for sensitive in cfg.sensitive_values():
        assert sensitive not in result
