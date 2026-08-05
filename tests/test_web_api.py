"""Authenticated strict Web API and HTTP security behavior."""
from __future__ import annotations

from core.operations import get_store
from web import app as web_app


def _comment(store, risk="low"):
    if risk == "high":
        return store.enqueue(
            "transition_issue", "PROJ-1",
            {"issue_key": "PROJ-1", "transition_name": "Close", "fields": {}},
            "close", "test",
        )
    return store.enqueue("add_comment", "PROJ-1", {"issue_key": "PROJ-1", "body": "x"}, "comment", "test")


def test_api_requires_browser_session(cfg):
    from fastapi.testclient import TestClient
    client = TestClient(web_app.create_app())
    assert client.get("/api/state").status_code == 403
    assert client.post("/api/queue/cancel", json={"operation_ids": ["op-" + "a" * 32]}).status_code == 403


def test_bootstrap_is_single_use_and_cookie_is_hardened(cfg):
    from fastapi.testclient import TestClient
    web_app._bootstrap_tokens.clear()
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
    assert queue.get_state()["queue"]["groups"]
    assert not queue.get_state()["execution"]["staged"]


def test_submit_revalidates_revision_after_ping(web_client, queue, monkeypatch):
    operation = _comment(queue)

    def ping():
        queue.enqueue(
            "add_comment", "PROJ-2", {"issue_key": "PROJ-2", "body": "concurrent"},
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
    assert queue.total_active() == 2


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


def test_revision_conflict_refresh_contract(web_client, queue):
    operation = _comment(queue)
    response = web_client.post(
        "/api/queue/reorder",
        json={"expected_queue_revision": 0, "groups": [], "standalone_operation_ids": [operation.operation_id]},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "revision_conflict"


def test_cancel_and_history_pagination(web_client, queue):
    operation = _comment(queue)
    cancelled = web_client.post("/api/queue/cancel", json={"operation_ids": [operation.operation_id]})
    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled_operation_ids"] == [operation.operation_id]
    history = web_client.get("/api/execution-history?page=1&page_size=50")
    assert history.status_code == 200
    assert history.json() == {"items": [], "total": 0, "page": 1, "page_size": 50}


def test_template_has_only_local_assets_and_no_html_sink(web_client):
    page = web_client.get("/queue")
    assert page.status_code == 200
    assert "cdn.tailwindcss.com" not in page.text
    assert '/static/confirm.js' in page.text
    script = open("web/static/confirm.js", encoding="utf-8").read()
    assert "innerHTML" not in script
