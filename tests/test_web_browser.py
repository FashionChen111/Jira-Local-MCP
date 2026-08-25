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


def _pending_state(producer_id: str, child_id: str) -> dict:
    producer = {
        "operation_id": producer_id,
        "operation_type": "create_issue",
        "issue_key": None,
        "risk_level": "medium",
        "payload": {
            "project_key": "PROJ",
            "issue_type": "Task",
            "summary": "new",
        },
        "issue_references": {},
        "unresolved_dependency_operation_ids": [],
        "description": "create",
        "source": "test",
        "created_at": 1,
        "attempt_no": 1,
        "layout_kind": "planned",
        "category": "producer",
        "position": 1,
        "manual_after_operation_id": None,
        "submittable": True,
    }
    child = {
        "operation_id": child_id,
        "operation_type": "add_comment",
        "issue_key": producer_id,
        "risk_level": "low",
        "payload": {"issue_key": producer_id, "body": "follow up"},
        "issue_references": {
            "issue_key": {
                "operation_id": producer_id,
                "resolved_issue_key": None,
            }
        },
        "unresolved_dependency_operation_ids": [producer_id],
        "description": "comment pending",
        "source": "test",
        "created_at": 2,
        "attempt_no": 1,
        "layout_kind": "planned",
        "category": "comment",
        "position": 2,
        "manual_after_operation_id": None,
        "submittable": True,
    }
    return {
        "queue": {
            "groups": [],
            "planned_groups": [
                {
                    "producer_operation_id": producer_id,
                    "producer_state": "queued",
                    "operations": [producer, child],
                }
            ],
            "standalone": [],
        },
        "execution": {
            "staged": [],
            "executing": [],
            "indeterminate": [],
            "held": [],
        },
        "revisions": {"queue": 2, "execution": 0, "history": 0},
    }


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


def test_pending_group_visibly_links_selection_and_submits_dependency_closure(
    page, cfg, monkeypatch
):
    _stop()
    cfg.web_port = _free_port()
    monkeypatch.setattr(web_app.execution_worker, "start_worker", lambda: None)
    producer_id = "op-" + "c" * 32
    child_id = "op-" + "d" * 32
    producer = {
        "operation_id": producer_id,
        "operation_type": "create_issue",
        "issue_key": None,
        "risk_level": "medium",
        "payload": {"project_key": "PROJ", "issue_type": "Task", "summary": "new"},
        "issue_references": {},
        "unresolved_dependency_operation_ids": [],
        "description": "create",
        "source": "test",
        "created_at": 1,
        "attempt_no": 1,
        "layout_kind": "planned",
        "category": "producer",
        "position": 1,
        "manual_after_operation_id": None,
        "submittable": True,
    }
    child = {
        "operation_id": child_id,
        "operation_type": "add_comment",
        "issue_key": producer_id,
        "risk_level": "low",
        "payload": {"issue_key": producer_id, "body": "follow up"},
        "issue_references": {
            "issue_key": {
                "operation_id": producer_id,
                "resolved_issue_key": None,
            }
        },
        "unresolved_dependency_operation_ids": [producer_id],
        "description": "comment pending",
        "source": "test",
        "created_at": 2,
        "attempt_no": 1,
        "layout_kind": "planned",
        "category": "comment",
        "position": 2,
        "manual_after_operation_id": None,
        "submittable": True,
    }
    state = {
        "queue": {
            "groups": [],
            "planned_groups": [{
                "producer_operation_id": producer_id,
                "producer_state": "queued",
                "operations": [producer, child],
            }],
            "standalone": [],
        },
        "execution": {"staged": [], "executing": [], "indeterminate": [], "held": []},
        "revisions": {"queue": 2, "execution": 0, "history": 0},
    }
    submitted = []
    try:
        base = web_app.ensure_started()
        page.route(
            "**/api/state",
            lambda route: route.fulfill(json=state, headers={"ETag": 'W/"q2-e0-h0"'}),
        )
        page.route(
            "**/api/execution-history**",
            lambda route: route.fulfill(json={"items": [], "total": 0, "page": 1, "page_size": 50}),
        )
        page.route(
            "**/api/queue/submit",
            lambda route: (
                submitted.append(route.request.post_data_json),
                route.fulfill(status=202, json={"accepted_operation_ids": [producer_id, child_id]}),
            )[-1],
        )
        page.goto(web_app.create_confirmation_url(base))
        producer_box = page.locator(
            f'[data-operation-id="{producer_id}"] input[type="checkbox"]'
        )
        child_box = page.locator(
            f'[data-operation-id="{child_id}"] input[type="checkbox"]'
        )
        expect(producer_box).to_be_checked()
        expect(child_box).to_be_checked()

        producer_box.uncheck()
        expect(producer_box).not_to_be_checked()
        expect(child_box).not_to_be_checked()
        child_box.check()
        expect(child_box).to_be_checked()
        expect(producer_box).to_be_checked()
        assert producer_box.get_attribute("title") == "因依赖关系自动勾选"

        page.locator("#submit-selected").click()
        deadline = time.monotonic() + 5
        while not submitted and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert set(submitted[0]["operation_ids"]) == {producer_id, child_id}
    finally:
        _stop()


def test_indeterminate_create_success_prompts_for_real_issue_key(
    page, cfg, monkeypatch
):
    _stop()
    cfg.web_port = _free_port()
    monkeypatch.setattr(web_app.execution_worker, "start_worker", lambda: None)
    operation_id = "op-" + "e" * 32
    operation = {
        "operation_id": operation_id,
        "operation_type": "create_issue",
        "issue_key": None,
        "risk_level": "medium",
        "submission_risk_level": "medium",
        "payload": {"project_key": "PROJ", "issue_type": "Task", "summary": "new"},
        "issue_references": {},
        "description": "create",
        "source": "test",
        "created_at": 1,
        "attempt_no": 1,
        "stage_order": 1,
        "state": "indeterminate",
        "execution_id": "exec-" + "f" * 32,
        "submitted_at": 1,
        "started_at": 2,
        "held_reason": None,
        "blocked_by_execution_id": None,
    }
    state = {
        "queue": {"groups": [], "planned_groups": [], "standalone": []},
        "execution": {"staged": [], "executing": [], "indeterminate": [operation], "held": []},
        "revisions": {"queue": 1, "execution": 2, "history": 1},
    }
    resolved = []
    try:
        base = web_app.ensure_started()
        page.route(
            "**/api/state",
            lambda route: route.fulfill(json=state, headers={"ETag": 'W/"q1-e2-h1"'}),
        )
        page.route(
            "**/api/execution-history**",
            lambda route: route.fulfill(json={"items": [], "total": 0, "page": 1, "page_size": 50}),
        )

        def resolve_route(route):
            resolved.append(route.request.post_data_json)
            route.fulfill(status=200, json={"operation_id": operation_id, "resolution": "succeeded"})

        page.route("**/api/execution/indeterminate/resolve", resolve_route)
        page.on("dialog", lambda dialog: dialog.accept("PROJ-77"))
        page.goto(web_app.create_confirmation_url(base))
        page.locator(f'[data-operation-id="{operation_id}"] button', has_text="已生效").click()
        deadline = time.monotonic() + 5
        while not resolved and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert resolved[0]["new_issue_key"] == "PROJ-77"
    finally:
        _stop()


def test_failed_pending_dependency_is_disabled_but_can_be_cancelled(
    page, cfg, monkeypatch
):
    _stop()
    cfg.web_port = _free_port()
    monkeypatch.setattr(web_app.execution_worker, "start_worker", lambda: None)
    producer_id = "op-" + "1" * 32
    child_id = "op-" + "2" * 32
    state = _pending_state(producer_id, child_id)
    child = state["queue"]["planned_groups"][0]["operations"][1]
    state["queue"]["planned_groups"][0]["producer_state"] = "failed"
    state["queue"]["planned_groups"][0]["operations"] = [child]
    child["dependency_statuses"] = {"issue_key": "failed"}
    child["submittable"] = False
    child["submission_block_reason"] = "依赖的创建 operation 已失败"
    try:
        base = web_app.ensure_started()
        page.route(
            "**/api/state",
            lambda route: route.fulfill(
                json=state, headers={"ETag": 'W/"q2-e1-h1"'}
            ),
        )
        page.route(
            "**/api/execution-history**",
            lambda route: route.fulfill(
                json={"items": [], "total": 0, "page": 1, "page_size": 50}
            ),
        )
        page.goto(web_app.create_confirmation_url(base))
        card = page.locator(f'[data-operation-id="{child_id}"]')
        expect(card.locator('input[type="checkbox"]')).to_be_disabled()
        expect(card.locator(".dependency-error")).to_contain_text("已失败")
        expect(card.get_by_role("button", name="取消此项")).to_be_visible()
    finally:
        _stop()


def test_resolved_reference_refreshes_to_real_key_and_normal_group(
    page, cfg, monkeypatch
):
    _stop()
    cfg.web_port = _free_port()
    monkeypatch.setattr(web_app.execution_worker, "start_worker", lambda: None)
    producer_id = "op-" + "3" * 32
    child_id = "op-" + "4" * 32
    initial = _pending_state(producer_id, child_id)
    initial["queue"]["planned_groups"][0]["operations"] = [
        initial["queue"]["planned_groups"][0]["operations"][1]
    ]
    initial["queue"]["planned_groups"][0]["producer_state"] = "executing"
    resolved_child = dict(initial["queue"]["planned_groups"][0]["operations"][0])
    resolved_child.update(
        issue_key="PROJ-88",
        payload={"issue_key": "PROJ-88", "body": "follow up"},
        issue_references={
            "issue_key": {
                "operation_id": producer_id,
                "resolved_issue_key": "PROJ-88",
            }
        },
        unresolved_dependency_operation_ids=[],
        dependency_statuses={"issue_key": "resolved"},
        layout_kind="grouped",
        submission_block_reason=None,
    )
    resolved = {
        "queue": {
            "groups": [{"issue_key": "PROJ-88", "operations": [resolved_child]}],
            "planned_groups": [],
            "standalone": [],
        },
        "execution": initial["execution"],
        "revisions": {"queue": 3, "execution": 2, "history": 1},
    }
    current = {"state": initial}
    try:
        base = web_app.ensure_started()
        page.route(
            "**/api/state",
            lambda route: route.fulfill(
                json=current["state"], headers={"ETag": 'W/"dynamic"'}
            ),
        )
        page.route(
            "**/api/execution-history**",
            lambda route: route.fulfill(
                json={"items": [], "total": 0, "page": 1, "page_size": 50}
            ),
        )
        page.goto(web_app.create_confirmation_url(base))
        card = page.locator(f'[data-operation-id="{child_id}"]')
        expect(card.locator("pre")).to_contain_text(producer_id)

        current["state"] = resolved
        page.evaluate("loadState(true)")
        expect(page.locator(".issue-group h3")).to_have_text("PROJ-88")
        expect(card.locator("pre")).to_contain_text("PROJ-88")
        expect(card.locator(".dependency")).to_contain_text("PROJ-88")
    finally:
        _stop()


def test_cancelling_producer_shows_dependant_count_and_confirms_cascade(
    page, cfg, monkeypatch
):
    _stop()
    cfg.web_port = _free_port()
    monkeypatch.setattr(web_app.execution_worker, "start_worker", lambda: None)
    producer_id = "op-" + "5" * 32
    child_id = "op-" + "6" * 32
    state = _pending_state(producer_id, child_id)
    requests = []
    dialogs = []

    def cancel_route(route):
        body = route.request.post_data_json
        requests.append(body)
        if body["cascade_confirmed"]:
            route.fulfill(
                status=200,
                json={
                    "cancelled_operation_ids": [producer_id, child_id],
                    "queue_revision": 3,
                },
            )
        else:
            route.fulfill(
                status=409,
                json={
                    "code": "dependency_confirmation_required",
                    "error": "cascade",
                    "affected_operation_ids": [producer_id, child_id],
                },
            )

    def accept_dialog(dialog):
        dialogs.append(dialog.message)
        dialog.accept()

    try:
        base = web_app.ensure_started()
        page.route(
            "**/api/state",
            lambda route: route.fulfill(
                json=state, headers={"ETag": 'W/"q2-e0-h0"'}
            ),
        )
        page.route(
            "**/api/execution-history**",
            lambda route: route.fulfill(
                json={"items": [], "total": 0, "page": 1, "page_size": 50}
            ),
        )
        page.route("**/api/queue/cancel", cancel_route)
        page.on("dialog", accept_dialog)
        page.goto(web_app.create_confirmation_url(base))
        page.locator(
            f'[data-operation-id="{child_id}"] input[type="checkbox"]'
        ).uncheck()
        page.locator("#cancel-selected").click()
        deadline = time.monotonic() + 5
        while len(requests) < 2 and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert [item["cascade_confirmed"] for item in requests] == [False, True]
        assert requests[0]["operation_ids"] == [producer_id]
        assert any("1 个队列依赖" in message for message in dialogs)
    finally:
        _stop()
