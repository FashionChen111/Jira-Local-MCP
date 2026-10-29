"""Scoped metadata contracts, SDK projections and read-only boundaries."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from jira.exceptions import JIRAError
from pydantic import JsonValue, TypeAdapter, ValidationError
from requests import Response

from core import jira_metadata as meta
from core.mcp_tools import jira_query


def at(value: object, *path: str | int) -> object:
    for key in path:
        if isinstance(key, str):
            assert isinstance(value, dict)
            value = value[key]
        else:
            assert isinstance(value, list)
            value = value[key]
    return value


def array(value: object) -> list:
    assert isinstance(value, list)
    return value


@pytest.fixture
def client(monkeypatch):
    value = Mock(spec=["project_issue_fields", "editmeta", "transitions", "projects", "project", "project_issue_types",
                       "fields", "search_users", "search_allowed_users_for_issue", "search_assignable_users_for_issues",
                       "myself", "user", "my_permissions", "attachment_meta", "_get_json", "issue_link_types", "issue"])
    monkeypatch.setattr(meta, "get_client", lambda: value)
    return value


@pytest.mark.parametrize("context", [
    {"kind": "edit", "issue_key": "op-" + "a" * 32},
    {"kind": "create", "project_key": "P"},
    {"kind": "create", "project_key": "P", "issue_type_id": "Task"},
    {"kind": "edit", "issue_key": "P-1", "project_key": "P"},
    {"kind": "transition", "issue_key": "P-1"},
    {"kind": "catalog"},
])
def test_context_rejects_ambiguous_or_future_scope(context):
    with pytest.raises(ValidationError):
        TypeAdapter(meta.WriteContext).validate_python(context)


def test_create_filtered_empty_page_preserves_source_cursor(client):
    class Page(list):
        total = 3
    client.project_issue_fields.side_effect = [
        Page([{"fieldId": "summary", "name": "Summary", "required": False}]),
        Page([{"fieldId": "customfield_1", "name": "Required", "required": True}]),
    ]
    context = meta.CreateContext(kind="create", project_key="P", issue_type_id="1")
    first = meta.fields(context, None, True, 0, 1)
    assert first["items"] == []
    assert first["page"] == {"mode": "server", "start_at": 0, "max_results": 1,
                             "total": None, "source_total": 3, "has_more": True, "next_start_at": 1}
    second = meta.fields(context, None, True, 1, 1)
    assert at(second, "items", 0, "field_id") == "customfield_1"
    assert client.project_issue_fields.call_args.kwargs == {"startAt": 1, "maxResults": 1}


def test_field_summary_then_cascade_detail_preserves_ancestry(client):
    client.editmeta.return_value = {"fields": {"customfield_1": {
        "name": "Area", "schema": {"type": "option", "custom": "cascadingselect"}, "required": True,
        "operations": ["set"], "allowedValues": [
            {"id": "10", "value": "parent", "self": "url", "children": [
                {"id": "11", "value": "child"}, {"id": "12", "value": "other"}]}]}}}
    context = meta.EditContext(kind="edit", issue_key="P-1")
    summary = meta.fields(context, None, False, 0, 50)
    assert "allowedValues" not in meta.object_value(at(summary, "items", 0))
    complete = meta.field_detail(context, "customfield_1", None, 0, 100)["item"]
    assert len(array(at(complete, "allowedValues", 0, "children"))) == 2
    child = meta.field_detail(context, "customfield_1", "child", 0, 1)["item"]
    assert at(child, "allowedValues") == [{"id": "10", "value": "parent", "children": [{"id": "11", "value": "child"}]}]
    assert at(child, "options_page", "has_more") is False
    middle = meta.field_detail(context, "customfield_1", None, 1, 1)["item"]
    assert at(middle, "allowedValues") == at(child, "allowedValues")
    assert at(middle, "options_page", "next_start_at") == 2
    missing = meta.field_detail(context, "customfield_999", None, 0, 100)
    assert missing["item"] is None
    client.fields.assert_not_called()


def test_transition_field_scope_and_catalog_are_separate(client):
    client.transitions.return_value = [{"id": "2", "fields": {"resolution": {"name": "Resolution"}}}]
    context = meta.TransitionContext(kind="transition", issue_key="P-1", transition_id="2")
    assert at(meta.fields(context, None, False, 0, 50), "items", 0, "field_id") == "resolution"
    client.transitions.assert_called_once_with("P-1", id="2", expand="transitions.fields")
    with pytest.raises(ValueError):
        meta.field_detail(meta.CatalogContext(kind="catalog"), "x", "a", 0, 100)


def test_user_scopes_never_fall_back_to_global_candidates(client):
    client.search_users.return_value = [{"name": "alice", "key": "U1", "displayName": "Alice", "html": "<b>A</b>"}]
    result = meta.search_users("ali", meta.DirectoryScope(), 2, 20)
    assert result["items"] == [{"name": "alice", "key": "U1", "displayName": "Alice", "mention": "[~alice]"}]
    assert at(result, "page", "has_more") is None
    client.search_assignable_users_for_issues.return_value = []
    meta.assignable_users(meta.IssueContext(kind="issue", issue_key="P-1"), "ali", 0, 20)
    client.search_assignable_users_for_issues.assert_called_once_with(username="ali", issueKey="P-1", startAt=0, maxResults=20)
    client.search_allowed_users_for_issue.return_value = []
    meta.search_users("ali", meta.IssueContext(kind="issue", issue_key="P-1"), 0, 20)
    client.search_allowed_users_for_issue.assert_called_once_with("ali", issueKey="P-1", startAt=0, maxResults=20)
    client.editmeta.return_value = {"fields": {"customfield_1": {"schema": {"type": "user"}}}}
    client.issue.return_value = {"fields": {"project": {"id": "10"}, "issuetype": {"id": "20"}}}
    client._get_json.return_value = {"users": {"users": [{"name": "alice", "html": "<b>Alice</b>"}], "total": 1}, "groups": {"groups": []}}
    scope = meta.FieldScope(kind="field", context=meta.EditContext(kind="edit", issue_key="P-1"), field_id="customfield_1")
    limitation = meta.search_users("ali", scope, 0, 20)
    assert limitation["candidate_validation"] == "unverified" and "尚未" in str(limitation["message"])
    assert at(limitation, "page", "mode") == "suggestions"
    assert at(limitation, "page", "next_start_at") is None
    client.issue.assert_called_once_with("P-1", fields="project,issuetype")
    client._get_json.assert_called_once_with("groupuserpicker", params={"fieldId": "customfield_1", "projectId": "10", "issueTypeId": "20", "query": "ali", "maxResults": 20})
    assert client.search_users.call_count == 1
    with pytest.raises(ValueError):
        meta.search_users("ali", scope, 1, 20)


def test_jira_errors_preserve_http_body_and_redact_only_secrets(cfg):
    response = Response()
    response.status_code = 403
    response._content = json.dumps({"errors": {"field": "SECRET_TOKEN forbidden"}, "errorMessages": ["denied"]}).encode()
    error = meta.error_response(JIRAError(status_code=403, response=response))
    assert error == {"source": "jira", "status_code": 403, "body": {"errors": {"field": "*** forbidden"}, "errorMessages": ["denied"]}}
    assert meta.error_response(ValueError("bad"))["source"] == "tool"


def test_response_budget_never_truncates_identity():
    rows: list[JsonValue] = [{"id": str(i), "name": "界" * 2000} for i in range(20)]
    result = meta.listing(rows, "mock", None, 0, 50)
    assert meta.byte_size(result) <= meta.MAX_RESPONSE_BYTES
    assert at(result, "page", "has_more") is True
    assert at(result, "page", "next_start_at") == len(array(result["items"]))
    assert all(len(row["name"]) == 2000 for row in array(result["items"]))
    with pytest.raises(ValueError, match="预算"):
        meta.listing([{"id": "x", "name": "界" * 20000}], "mock", None, 0, 50)


def test_issue_read_preserves_current_values_and_passes_exact_fields(queue, monkeypatch):
    values = {"assignee": {"name": "alice", "displayName": "Alice"}, "issuetype": {"id": "2", "name": "Bug"},
              "customfield_1": [{"id": "1", "value": "A"}, {"id": "2", "value": "B"}],
              "customfield_2": {"id": "3", "child": {"id": "4"}},
              "timetracking": {"remainingEstimateSeconds": 3600},
              "issuelinks": [{"type": {"id": "1", "inward": "is blocked by", "outward": "blocks"}, "outwardIssue": {"key": "P-2"}}]}
    client = Mock(spec=["issue"])
    client.issue.return_value = SimpleNamespace(raw={"key": "P-1", "fields": {**values, "description": "unrelated"}})
    monkeypatch.setattr(jira_query.jira_client, "get_client", lambda: client)
    queue.queue.enqueue("add_comment", "P-1", {"body": "pending"}, "comment")
    result = json.loads(jira_query.jira_query_issue("P-1", list(values)))
    assert result["fields"] == values
    assert "queue_notice" in result and result["key"] == "P-1"
    client.issue.assert_called_once_with("P-1", fields=",".join(values))
    assert json.loads(jira_query.jira_query_issue("P-1", []))["source"] == "tool"
    assert client.issue.call_count == 1


def test_remaining_metadata_topics_are_project_scoped_and_read_only(client):
    client.attachment_meta.return_value = {"enabled": True, "uploadLimit": 12345}
    assert at(meta.server_settings("attachments"), "item", "upload_limit_bytes") == 12345
    client._get_json.return_value = {"issueLinkingEnabled": True, "subTasksEnabled": False, "unrelated": "x"}
    assert meta.server_settings("features")["item"] == {"issueLinkingEnabled": True, "subTasksEnabled": False}
    client.projects.return_value = [{"id": "1", "key": "P", "name": "Project"}]
    assert at(meta.projects("P", 0, 20), "items", 0, "key") == "P"
    client.project.return_value = {"issueTypes": [{"id": "2", "name": "Task", "subtask": False}]}
    assert at(meta.issue_types("P", "configured", 0, 50), "items", 0, "id") == "2"
    client.project.assert_called_once_with("P")
    client.issue_link_types.return_value = [{"id": "3", "name": "Blocks", "inward": "blocked by", "outward": "blocks"}]
    assert at(meta.link_types(None, 0, 50), "items", 0, "outward") == "blocks"
    client.issue_link_types.assert_called_once_with(force=True)
    client.my_permissions.return_value = {"permissions": {"EDIT_ISSUES": {"key": "EDIT_ISSUES", "havePermission": True}}}
    result = meta.permissions(meta.IssueContext(kind="issue", issue_key="P-1"), ["EDIT_ISSUES", "UNKNOWN"])
    assert result["item"] == [{"key": "EDIT_ISSUES", "havePermission": True}, {"key": "UNKNOWN", "unknown": True}]
    client.my_permissions.assert_called_once_with(issueKey="P-1")
    client.myself.return_value = {"name": "me", "key": "U", "timeZone": "Asia/Shanghai"}
    assert at(meta.user(None), "item", "mention") == "[~me]"
