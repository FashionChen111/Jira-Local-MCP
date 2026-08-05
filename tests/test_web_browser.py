"""Real-browser inert rendering regression for attacker-controlled Jira values."""
from __future__ import annotations

import socket
import time
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect

from web import app as web_app


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _stop():
    if web_app._server is not None:
        web_app._server.should_exit = True
    if web_app._server_thread is not None:
        web_app._server_thread.join(timeout=5)
    web_app._server = None
    web_app._server_thread = None
    web_app._actual_port = None


def test_malicious_business_values_render_inertly(page, cfg, monkeypatch):
    _stop()
    cfg.web_port = _free_port()
    monkeypatch.setattr(web_app.execution_worker, "start_worker", lambda: None)
    try:
        base = web_app.ensure_started()
        operation_id = "op-" + "a" * 32
        malicious = '<img src=x onerror="globalThis.__xss=1">'
        state = {
            "queue": {"groups": [{"issue_key": "PROJ-1", "operations": [{
                "operation_id": operation_id, "operation_type": "add_comment", "issue_key": "PROJ-1",
                "risk_level": "low", "payload": {"body": malicious}, "description": malicious,
                "source": "test", "created_at": 1, "attempt_no": 1, "layout_kind": "grouped",
                "category": "comment", "position": 1,
            }]}], "standalone": []},
            "execution": {"staged": [], "executing": [], "indeterminate": [], "held": []},
            "revisions": {"queue": 1, "execution": 0, "history": 0},
        }
        page.route("**/api/state", lambda route: route.fulfill(json=state, headers={"ETag": 'W/"q1-e0-h0"'}))
        page.route("**/api/execution-history**", lambda route: route.fulfill(json={"items": [], "total": 0, "page": 1, "page_size": 50}))
        page.goto(web_app.create_confirmation_url(base))
        page.locator(f'[data-operation-id="{operation_id}"]').wait_for()
        assert page.evaluate("globalThis.__xss") is None
        assert page.locator("img").count() == 0
        assert malicious in page.locator(f'[data-operation-id="{operation_id}"]').text_content()
    finally:
        _stop()


def test_review_panels_high_risk_confirmation_and_history_pagination(page, cfg, monkeypatch):
    _stop()
    cfg.web_port = _free_port()
    monkeypatch.setattr(web_app.execution_worker, "start_worker", lambda: None)
    operation_id = "op-" + "b" * 32
    state = {
        "queue": {"groups": [{"issue_key": "PROJ-1", "operations": [{
            "operation_id": operation_id, "operation_type": "transition_issue",
            "issue_key": "PROJ-1", "risk_level": "high",
            "payload": {"issue_key": "PROJ-1", "transition_name": "Done"},
            "description": "transition", "source": "test", "created_at": 1,
            "attempt_no": 1, "layout_kind": "grouped", "category": "workflow",
            "position": 1, "manual_after_operation_id": None,
        }]}], "standalone": []},
        "execution": {"staged": [], "executing": [], "indeterminate": [], "held": []},
        "revisions": {"queue": 1, "execution": 0, "history": 0},
    }
    submitted = []
    history_pages = []

    def history_route(route):
        query = parse_qs(urlparse(route.request.url).query)
        history_pages.append(int(query["page"][0]))
        route.fulfill(json={"items": [], "total": 51, "page": int(query["page"][0]), "page_size": 50})

    def submit_route(route):
        body = route.request.post_data_json
        submitted.append(body)
        if not body["high_risk_confirmed"]:
            route.fulfill(status=409, json={"code": "confirmation_required", "error": "confirm"})
        else:
            route.fulfill(status=202, json={"accepted_operation_ids": [operation_id]})

    try:
        base = web_app.ensure_started()
        page.route("**/api/state", lambda route: route.fulfill(json=state, headers={"ETag": 'W/"q1-e0-h0"'}))
        page.route("**/api/execution-history**", history_route)
        page.route("**/api/queue/submit", submit_route)
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(web_app.create_confirmation_url(base))
        page.locator(f'[data-operation-id="{operation_id}"]').wait_for()
        assert page.locator("section.panel").count() == 4
        expect(page.locator("#history-page")).to_have_text("1 / 2")
        page.locator("#history-next").click()
        expect(page.locator("#history-page")).to_have_text("2 / 2")
        assert history_pages[-1] == 2
        page.locator("#submit-selected").click()
        deadline = time.monotonic() + 5
        while len(submitted) < 2 and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert [item["high_risk_confirmed"] for item in submitted] == [False, True]
    finally:
        _stop()
