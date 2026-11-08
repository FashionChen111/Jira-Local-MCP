"""Authenticated strict Web API and HTTP security behavior."""
from __future__ import annotations

from core.operations.dependencies import orchestration as dependency_orchestration
from core.operations.models import OperationRecord
from web import app as web_app


def _comment(store, risk="low"):
    if risk == "high":
        return store.queue.enqueue(
            "transition_issue", "PROJ-1",
            {"transition_id": "10001", "fields": {}, "comment": None},
            "close", "test",
        )
    return store.queue.enqueue(
        "add_comment", "PROJ-1", {"body": "x"}, "comment", "test"
    )


def test_api_requires_browser_session(cfg):
    from fastapi.testclient import TestClient
    client = TestClient(web_app.create_app())
    assert client.get("/api/state").status_code == 403
    assert client.post(
        "/api/queue/cancel",
        json={
            "expected_queue_revision": 0,
            "operation_ids": ["op-" + "a" * 32],
        },
    ).status_code == 403


def test_bootstrap_is_single_use_and_cookie_is_hardened(cfg):
    from fastapi.testclient import TestClient
    from web.review_sessions import default_manager
    default_manager.clear()
    client = TestClient(web_app.create_app())
    url = web_app.create_confirmation_url("http://testserver")
    first = client.get(url, follow_redirects=False)
    assert first.status_code == 303
    cookie = first.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get(url, follow_redirects=False).status_code == 403


def test_state_supports_etag_304_and_security_headers(web_client, queue):
    _comment(queue)
    first = web_client.get("/api/state")
    assert first.status_code == 200
    etag = first.headers["etag"]
    second = web_client.get("/api/state", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert "default-src 'none'" in first.headers["content-security-policy"]
    assert first.headers["x-frame-options"] == "DENY"
    assert first.headers["x-content-type-options"] == "nosniff"


def test_issue_fields_reads_only_requested_fields(web_client, monkeypatch):
    class FakeJira:
        def issue(self, issue_key, fields):
            assert issue_key == "PROJ-9"
            assert fields == "summary,customfield_123"
            return type(
                "Issue",
                (),
                {
                    "raw": {
                        "key": issue_key,
                        "fields": {
                            "summary": "Old summary",
                            "customfield_123": {"value": "Old value", "self": "hidden"},
                            "unused": "not requested",
                        },
                    }
                },
            )()

    monkeypatch.setattr(web_app.jira_client, "get_client", lambda: FakeJira())
    response = web_client.post(
        "/api/issue-fields",
        json={
            "issue_key": "PROJ-9",
            "fields": ["summary", "customfield_123"],
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "key": "PROJ-9",
        "fields": {
            "summary": "Old summary",
            "customfield_123": {"value": "Old value"},
        },
    }


def test_issue_fields_rejects_non_field_ids(web_client):
    response = web_client.post(
        "/api/issue-fields",
        json={"issue_key": "PROJ-9", "fields": ["summary,*"]},
    )
    assert response.status_code == 422


def test_issue_preview_reads_all_editable_fields_without_writing(web_client, monkeypatch):
    class FakeJira:
        def editmeta(self, issue_key):
            assert issue_key == "PROJ-9"
            return {
                "fields": {
                    "summary": {
                        "name": "Summary",
                        "schema": {"type": "string", "system": "summary"},
                    },
                    "customfield_123": {
                        "name": "Count",
                        "schema": {"type": "number"},
                    },
                }
            }

        def issue(self, issue_key, fields):
            assert issue_key == "PROJ-9"
            assert fields == "summary,customfield_123"
            return type(
                "Issue",
                (),
                {
                    "raw": {
                        "key": issue_key,
                        "fields": {
                            "summary": "Old summary",
                            "customfield_123": 3,
                        },
                    }
                },
            )()

    monkeypatch.setattr(web_app.jira_client, "get_client", lambda: FakeJira())
    response = web_client.post(
        "/api/issue-preview", json={"issue_key": "PROJ-9"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "key": "PROJ-9",
        "fields": [
            {
                "field_id": "summary",
                "name": "Summary",
                "schema": {"type": "string"},
                "current_value": "Old summary",
                "has_current_value": True,
            },
            {
                "field_id": "customfield_123",
                "name": "Count",
                "schema": {"type": "number"},
                "current_value": 3,
                "has_current_value": True,
            },
        ],
    }


def test_issue_preview_rejects_invalid_issue_key(web_client):
    response = web_client.post(
        "/api/issue-preview", json={"issue_key": "invalid"}
    )
    assert response.status_code == 422


def test_issue_transition_returns_current_and_target_status(web_client, monkeypatch):
    class FakeJira:
        def issue(self, issue_key, fields):
            assert issue_key == "VODCER-233"
            assert fields == "status"
            return type(
                "Issue",
                (),
                {
                    "raw": {
                        "key": issue_key,
                        "fields": {"status": {"id": "3", "name": "处理中"}},
                    }
                },
            )()

        def transitions(self, issue):
            return [
                {
                    "id": "21",
                    "name": "任务结束",
                    "to": {"id": "6", "name": "完成"},
                    "fields": {},
                }
            ]

    monkeypatch.setattr(web_app.jira_client, "get_client", lambda: FakeJira())
    response = web_client.post(
        "/api/issue-transition",
        json={"issue_key": "VODCER-233", "transition_id": "21"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "issue_key": "VODCER-233",
        "current_status": "处理中",
        "transition": {
            "id": "21",
            "name": "任务结束",
            "target_status": "完成",
        },
    }


def test_issue_transition_rejects_invalid_id_and_stale_transition(web_client, monkeypatch):
    invalid = web_client.post(
        "/api/issue-transition",
        json={"issue_key": "PROJ-9", "transition_id": "21x"},
    )
    assert invalid.status_code == 422

    class FakeJira:
        def issue(self, issue_key, fields):
            return type("Issue", (), {"raw": {"fields": {"status": {"name": "处理中"}}}})()

        def transitions(self, issue):
            return [{"id": "22", "name": "其他流转", "to": {"name": "待处理"}}]

    monkeypatch.setattr(web_app.jira_client, "get_client", lambda: FakeJira())
    stale = web_client.post(
        "/api/issue-transition",
        json={"issue_key": "PROJ-9", "transition_id": "21"},
    )
    assert stale.status_code == 404
    assert "不再适用于" in stale.json()["error"]


def test_mutation_models_reject_extra_empty_duplicate_and_malformed(web_client):
    valid = "op-" + "a" * 32
    bodies = [
        {"operation_ids": []},
        {"operation_ids": [valid, valid]},
        {"operation_ids": ["bad"]},
        {"operation_ids": [valid], "extra": True},
        {"operation_ids": "not-a-list"},
    ]
    for body in bodies:
        assert web_client.post("/api/queue/cancel", json=body).status_code == 422


def test_submit_validates_before_ping(web_client, queue, monkeypatch):
    called = False
    def ping():
        nonlocal called
        called = True
        return True
    monkeypatch.setattr(web_app.jira_client, "ping", ping)
    response = web_client.post(
        "/api/queue/submit",
        json={
            "expected_queue_revision": 0,
            "operation_ids": ["op-" + "a" * 32],
            "high_risk_confirmed": False,
        },
    )
    assert response.status_code == 409
    assert called is False


def test_ping_failure_keeps_queue(web_client, queue, monkeypatch):
    operation = _comment(queue)
    monkeypatch.setattr(web_app.jira_client, "ping", lambda: False)
    response = web_client.post(
        "/api/queue/submit",
        json={"expected_queue_revision": 1, "operation_ids": [operation.operation_id], "high_risk_confirmed": False},
    )
    assert response.status_code == 503
    assert queue.query.get_state().queue.groups
    assert not queue.query.get_state().execution.staged


def test_submit_revalidates_revision_after_ping(web_client, queue, monkeypatch):
    operation = _comment(queue)

    def ping():
        queue.queue.enqueue(
            "add_comment", "PROJ-2", {"body": "concurrent"},
            "concurrent", "test",
        )
        return True

    monkeypatch.setattr(web_app.jira_client, "ping", ping)
    response = web_client.post(
        "/api/queue/submit",
        json={
            "expected_queue_revision": 1,
            "operation_ids": [operation.operation_id],
            "high_risk_confirmed": False,
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "revision_conflict"
    assert queue.query.total_active() == 2


def test_submit_inspects_once_in_each_required_consistency_boundary(
    web_client, queue, monkeypatch
) -> None:
    operation = _comment(queue)
    inspected_operation_ids: list[str] = []
    original = dependency_orchestration.inspect_dependencies

    def counted(record: OperationRecord):
        inspected_operation_ids.append(record.operation_id)
        return original(record)

    monkeypatch.setattr(
        dependency_orchestration,
        "inspect_dependencies",
        counted,
    )
    monkeypatch.setattr(web_app.jira_client, "ping", lambda: True)
    monkeypatch.setattr(web_app.execution_worker, "wake_worker", lambda: None)

    response = web_client.post(
        "/api/queue/submit",
        json={
            "expected_queue_revision": 1,
            "operation_ids": [operation.operation_id],
            "high_risk_confirmed": False,
        },
    )

    assert response.status_code == 202
    assert inspected_operation_ids == [operation.operation_id] * 2


def test_high_risk_requires_confirmation_then_returns_accepted(web_client, queue, monkeypatch):
    operation = _comment(queue, "high")
    monkeypatch.setattr(web_app.jira_client, "ping", lambda: True)
    monkeypatch.setattr(web_app.execution_worker, "wake_worker", lambda: None)
    body = {"expected_queue_revision": 1, "operation_ids": [operation.operation_id], "high_risk_confirmed": False}
    required = web_client.post("/api/queue/submit", json=body)
    assert required.status_code == 409
    assert required.json()["code"] == "confirmation_required"
    body["high_risk_confirmed"] = True
    accepted = web_client.post("/api/queue/submit", json=body)
    assert accepted.status_code == 202
    assert accepted.json()["accepted_operation_ids"] == [operation.operation_id]


def test_reorder_endpoint_is_not_registered(web_client, queue):
    _comment(queue)
    response = web_client.post(
        "/api/queue/reorder",
        json={"expected_queue_revision": 1, "groups": []},
    )
    assert response.status_code == 404
    assert "/api/queue/reorder" not in web_client.get("/openapi.json").json()["paths"]


def test_cancel_and_history_pagination(web_client, queue):
    operation = _comment(queue)
    cancelled = web_client.post(
        "/api/queue/cancel",
        json={
            "expected_queue_revision": 1,
            "operation_ids": [operation.operation_id],
        },
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled_operation_ids"] == [operation.operation_id]
    history = web_client.get("/api/execution-history?page=1&page_size=50")
    assert history.status_code == 200
    assert history.json() == {"items": [], "total": 0, "page": 1, "page_size": 50}


def test_cancel_producer_requires_explicit_dependency_scope(web_client, queue):
    create = queue.queue.enqueue(
        "create_issue",
        None,
        {"project_key": "PROJ", "issue_type_id": "10001", "summary": "new"},
        "create",
        "test",
    )
    child = queue.queue.enqueue(
        "add_comment",
        create.operation_id,
        {"body": "x"},
        "child",
        "test",
    )
    body = {
        "expected_queue_revision": 2,
        "operation_ids": [create.operation_id],
    }
    required = web_client.post("/api/queue/cancel", json=body)
    assert required.status_code == 409
    assert required.json()["code"] == "dependency_scope_incomplete"
    assert required.json()["missing_operation_ids"] == [child.operation_id]
    assert set(required.json()["affected_operation_ids"]) == {
        create.operation_id,
        child.operation_id,
    }
    body["operation_ids"] = required.json()["affected_operation_ids"]
    cancelled = web_client.post("/api/queue/cancel", json=body)
    assert cancelled.status_code == 200
    assert set(cancelled.json()["cancelled_operation_ids"]) == {
        create.operation_id,
        child.operation_id,
    }


def test_cancel_rejects_obsolete_flag_and_publishes_new_schema(web_client, queue):
    operation = _comment(queue)
    response = web_client.post("/api/queue/cancel", json={
        "operation_ids": [operation.operation_id], "expected_queue_revision": 1,
        "cascade_confirmed": True,
    })
    assert response.status_code == 422
    assert queue.query.total_active() == 1
    schema = web_client.get("/openapi.json").json()["components"]["schemas"]["CancelRequest"]
    assert set(schema["properties"]) == {"operation_ids", "expected_queue_revision"}
    assert schema["additionalProperties"] is False


def test_indeterminate_create_resolution_requires_real_key(web_client, queue):
    create = queue.queue.enqueue(
        "create_issue",
        None,
        {"project_key": "PROJ", "issue_type_id": "10001", "summary": "new"},
        "create",
        "test",
    )
    queue.submission.submit(1, [create.operation_id], False)
    queue.execution.claim_next()
    queue.execution.recover_interrupted()
    body = {
        "operation_id": create.operation_id,
        "resolution": "succeeded",
        "high_risk_confirmed": False,
        "new_issue_key": None,
    }
    missing = web_client.post("/api/execution/indeterminate/resolve", json=body)
    assert missing.status_code == 409
    body["new_issue_key"] = "PROJ-50"
    resolved = web_client.post("/api/execution/indeterminate/resolve", json=body)
    assert resolved.status_code == 200
    assert resolved.json()["new_issue_key"] == "PROJ-50"


def test_indeterminate_attachment_resolution_uses_confirmed_identity(web_client, queue):
    attachment = queue.queue.enqueue(
        "add_attachment",
        "PROJ-1",
        {"file_path": "C:\\fixtures\\a.jpg", "file_name": "a.jpg"},
        "attachment",
        "test",
    )
    queue.submission.submit(1, [attachment.operation_id], False)
    queue.execution.claim_next()
    queue.execution.recover_interrupted()
    body = {
        "operation_id": attachment.operation_id,
        "resolution": "succeeded",
        "high_risk_confirmed": False,
        "new_issue_key": None,
        "attachment": {"id": "80008", "filename": "a-1.jpg"},
    }

    legacy = {**body, "attachment_result": body["attachment"]}
    assert web_client.post("/api/execution/indeterminate/resolve", json=legacy).status_code == 422
    resolved = web_client.post(
        "/api/execution/indeterminate/resolve", json=body
    )

    assert resolved.status_code == 200
    assert resolved.json()["attachment_results"][0]["attachment"] == body["attachment"]


def test_template_has_only_local_assets_and_no_html_sink(web_client):
    page = web_client.get("/queue")
    assert page.status_code == 200
    assert "cdn.tailwindcss.com" not in page.text
    assert '/static/confirm.js' in page.text
    script = open("web/static/confirm.js", encoding="utf-8").read()
    assert "innerHTML" not in script
    assert "/api/queue/reorder" not in script
    assert "saveLayout" not in script
    assert "转为零散" not in script
