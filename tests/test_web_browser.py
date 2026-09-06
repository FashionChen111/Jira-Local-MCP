"""Real-browser regressions for safe rendering and the read-only automatic queue."""

from __future__ import annotations

import socket
import time
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from web import app as web_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _stop() -> None:
    if web_app._server is not None:
        web_app._server.should_exit = True
    if web_app._server_thread is not None:
        web_app._server_thread.join(timeout=5)
    web_app._server = None
    web_app._server_thread = None
    web_app._actual_port = None


def _operation(
    operation_id: str,
    *,
    operation_type: str = "add_comment",
    issue_key: str | None = "PROJ-1",
    target_issue_key: str | None = None,
    risk_level: str = "low",
    payload: dict | None = None,
    description: str = "comment",
    dependencies: list[str] | None = None,
    dependency_states: dict[str, str] | None = None,
    submittable: bool = True,
    block_reason: str | None = None,
) -> dict:
    return {
        "operation_id": operation_id,
        "operation_type": operation_type,
        "issue_key": issue_key,
        "target_issue_key": target_issue_key,
        "risk_level": risk_level,
        "payload": payload or {"body": "follow up"},
        "description": description,
        "source": "test",
        "created_at": 1,
        "attempt_no": 1,
        "dependency_operation_ids": dependencies or [],
        "dependency_states": dependency_states or {},
        "submittable": submittable,
        "submission_block_reason": block_reason,
    }


def _state(groups: list[dict], standalone: list[dict] | None = None) -> dict:
    return {
        "queue": {"groups": groups, "standalone": standalone or []},
        "execution": {
            "staged": [],
            "executing": [],
            "indeterminate": [],
            "held": [],
        },
        "revisions": {"queue": 2, "execution": 0, "history": 0},
    }


def _pending_state(producer_id: str, child_id: str) -> dict:
    producer = _operation(
        producer_id,
        operation_type="create_issue",
        issue_key=None,
        risk_level="medium",
        payload={"project_key": "PROJ", "issue_type": "Task", "summary": "new"},
        description="create",
    )
    child = _operation(
        child_id,
        issue_key=producer_id,
        dependencies=[producer_id],
        dependency_states={producer_id: "queued"},
        description="comment pending",
    )
    return _state(
        [
            {
                "group_key": producer_id,
                "kind": "pending_issue",
                "producer_state": "queued",
                "operations": [producer, child],
            }
        ]
    )


def _serve(page, cfg, monkeypatch, state: dict) -> str:
    _stop()
    cfg.web_port = _free_port()
    monkeypatch.setattr(web_app.execution_worker, "start_worker", lambda: None)
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
    return base


def test_malicious_values_render_inertly_and_queue_has_no_sort_controls(
    page, cfg, monkeypatch
) -> None:
    operation_id = "op-" + "a" * 32
    malicious = '<img src=x onerror="globalThis.__xss=1">'
    state = _state(
        [
            {
                "group_key": "PROJ-1",
                "kind": "jira_issue",
                "producer_state": None,
                "operations": [
                    _operation(
                        operation_id,
                        payload={"body": malicious},
                        description=malicious,
                    )
                ],
            }
        ]
    )
    requested: list[str] = []
    page.on("request", lambda request: requested.append(request.url))
    try:
        base = _serve(page, cfg, monkeypatch, state)
        page.goto(web_app.create_confirmation_url(base))
        card = page.locator(f'[data-operation-id="{operation_id}"]')
        card.wait_for()
        assert page.evaluate("globalThis.__xss") is None
        assert page.locator("img").count() == 0
        assert malicious in card.text_content()
        assert card.locator("button").count() == 0
        assert "转为零散" not in page.locator("body").text_content()
        assert not any("/api/queue/reorder" in url for url in requested)
    finally:
        _stop()


def test_pending_selection_automatically_submits_dependency_closure(
    page, cfg, monkeypatch
) -> None:
    producer_id = "op-" + "1" * 32
    child_id = "op-" + "2" * 32
    state = _pending_state(producer_id, child_id)
    submitted: list[dict] = []
    try:
        base = _serve(page, cfg, monkeypatch, state)

        def submit_route(route):
            submitted.append(route.request.post_data_json)
            route.fulfill(status=202, json={"accepted_operation_ids": [producer_id, child_id]})

        page.route("**/api/queue/submit", submit_route)
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
        page.locator("#submit-selected").click()
        deadline = time.monotonic() + 5
        while not submitted and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert set(submitted[0]["operation_ids"]) == {producer_id, child_id}
    finally:
        _stop()


def test_full_parameters_combine_both_top_level_keys_with_payload(
    page, cfg, monkeypatch
) -> None:
    operation_id = "op-" + "3" * 32
    state = _state(
        [],
        [
            _operation(
                operation_id,
                operation_type="add_issuelink",
                issue_key="PROJ-1",
                target_issue_key="PROJ-2",
                payload={"link_type": "Relates"},
            )
        ],
    )
    try:
        base = _serve(page, cfg, monkeypatch, state)
        page.goto(web_app.create_confirmation_url(base))
        card = page.locator(f'[data-operation-id="{operation_id}"]')
        card.locator("summary").click()
        expect(card.locator("pre")).to_contain_text('"issue_key": "PROJ-1"')
        expect(card.locator("pre")).to_contain_text(
            '"target_issue_key": "PROJ-2"'
        )
        expect(card.locator("pre")).to_contain_text('"link_type": "Relates"')
        assert "field_references" not in card.locator("pre").text_content()
    finally:
        _stop()


def test_high_risk_confirmation_and_history_pagination(page, cfg, monkeypatch) -> None:
    operation_id = "op-" + "4" * 32
    operation = _operation(
        operation_id,
        operation_type="transition_issue",
        risk_level="high",
        payload={"transition_name": "Done", "fields": {}, "comment": None},
    )
    state = _state(
        [
            {
                "group_key": "PROJ-1",
                "kind": "jira_issue",
                "producer_state": None,
                "operations": [operation],
            }
        ]
    )
    submitted: list[dict] = []
    history_pages: list[int] = []
    try:
        base = _serve(page, cfg, monkeypatch, state)

        def history_route(route):
            page_number = int(parse_qs(urlparse(route.request.url).query)["page"][0])
            history_pages.append(page_number)
            route.fulfill(
                json={"items": [], "total": 51, "page": page_number, "page_size": 50}
            )

        def submit_route(route):
            body = route.request.post_data_json
            submitted.append(body)
            if body["high_risk_confirmed"]:
                route.fulfill(status=202, json={"accepted_operation_ids": [operation_id]})
            else:
                route.fulfill(
                    status=409,
                    json={"code": "confirmation_required", "error": "confirm"},
                )

        page.unroute("**/api/execution-history**")
        page.route("**/api/execution-history**", history_route)
        page.route("**/api/queue/submit", submit_route)
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(web_app.create_confirmation_url(base))
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


def test_blocked_dependency_is_disabled_but_can_be_cancelled(
    page, cfg, monkeypatch
) -> None:
    operation_id = "op-" + "5" * 32
    requests: list[dict] = []
    producer_id = "op-" + "6" * 32
    operation = _operation(
        operation_id,
        issue_key=producer_id,
        dependencies=[producer_id],
        dependency_states={producer_id: "failed"},
        submittable=False,
        block_reason=f"依赖的创建/克隆事务已失败：{producer_id}",
    )
    state = _state(
        [
            {
                "group_key": producer_id,
                "kind": "pending_issue",
                "producer_state": "failed",
                "operations": [operation],
            }
        ]
    )
    try:
        base = _serve(page, cfg, monkeypatch, state)
        page.goto(web_app.create_confirmation_url(base))
        card = page.locator(f'[data-operation-id="{operation_id}"]')
        expect(card.locator('input[type="checkbox"]')).to_be_disabled()
        expect(card.locator(".dependency-error")).to_contain_text("已失败")
        expect(card.get_by_role("button", name="取消此项")).to_be_visible()

        def cancel_route(route):
            requests.append(route.request.post_data_json)
            state["queue"]["groups"] = []
            route.fulfill(json={"cancelled_operation_ids": [operation_id], "queue_revision": 3})

        page.route("**/api/queue/cancel", cancel_route)
        card.get_by_role("button", name="取消此项").click()
        expect(page.locator("#queue-message")).to_contain_text("已从本地审核队列取消")
        assert requests == [{"operation_ids": [operation_id], "expected_queue_revision": 2}]
    finally:
        _stop()


@pytest.mark.parametrize("outcome", ["initial_decline", "scope_decline", "success", "revision_conflict"])
def test_cancel_scope_confirmation_and_original_revision(page, cfg, monkeypatch, outcome):
    producer_id = "op-" + "a" * 32
    child_id = "op-" + "b" * 32
    state = _pending_state(producer_id, child_id)
    requests: list[dict] = []
    dialogs: list[str] = []

    def on_dialog(dialog):
        dialogs.append(dialog.message)
        if outcome == "initial_decline" or (outcome == "scope_decline" and len(dialogs) == 2):
            dialog.dismiss()
        else:
            dialog.accept()

    try:
        base = _serve(page, cfg, monkeypatch, state)

        def cancel_route(route):
            requests.append(route.request.post_data_json)
            if len(requests) == 1:
                if outcome == "revision_conflict":
                    # Polling can replace global state while cancellation awaits a response.
                    state["revisions"]["queue"] = 3
                    page.evaluate("state.revisions.queue = 3")
                route.fulfill(status=409, json={
                    "code": "dependency_scope_incomplete", "error": "范围不完整",
                    "affected_operation_ids": [producer_id, child_id],
                    "missing_operation_ids": [child_id],
                })
            elif outcome == "revision_conflict":
                route.fulfill(status=409, json={"code": "revision_conflict", "error": "队列已更新"})
            else:
                state["queue"]["groups"] = []
                state["revisions"]["queue"] = 3
                route.fulfill(json={"cancelled_operation_ids": [producer_id, child_id], "queue_revision": 3})

        page.route("**/api/queue/cancel", cancel_route)
        page.on("dialog", on_dialog)
        page.goto(web_app.create_confirmation_url(base))
        page.locator(f'[data-operation-id="{child_id}"] input[type="checkbox"]').uncheck()
        expect(page.locator(f'[data-operation-id="{producer_id}"] input[type="checkbox"]')).to_be_checked()
        if outcome == "initial_decline":
            page.locator("#cancel-selected").click()
        else:
            with page.expect_event("dialog", predicate=lambda dialog: "队列依赖" in dialog.message):
                page.locator("#cancel-selected").click()
        if outcome == "initial_decline":
            assert len(dialogs) == 1 and requests == []
        else:
            if outcome == "scope_decline":
                expect(page.locator(f'[data-operation-id="{producer_id}"]')).to_be_visible()
            elif outcome == "revision_conflict":
                expect(page.locator("#queue-message")).to_contain_text("队列已更新")
                page.wait_for_function("state.revisions.queue === 3")
            else:
                expect(page.locator("#queue-message")).to_contain_text("已从本地审核队列取消")
                expect(page.locator(f'[data-operation-id="{producer_id}"]')).to_have_count(0)
            assert len(dialogs) == 2
            assert requests[0] == {"operation_ids": [producer_id], "expected_queue_revision": 2}
            if outcome == "scope_decline":
                assert len(requests) == 1
            else:
                assert requests[1] == {
                    "operation_ids": [producer_id, child_id], "expected_queue_revision": 2,
                }
                assert len(requests) == 2
    finally:
        _stop()


def test_browser_cancels_real_queue_dependency_scope(page, queue, cfg, monkeypatch):
    producer = queue.queue.enqueue(
        "create_issue", None,
        {"project_key": "PROJ", "issue_type": "Task", "summary": "review"}, "create", "test",
    )
    child = queue.queue.enqueue(
        "add_comment", producer.operation_id, {"body": "comment"}, "comment", "test",
    )
    requests: list[dict] = []
    dialogs: list[str] = []
    _stop()
    cfg.web_port = _free_port()
    monkeypatch.setattr(web_app.execution_worker, "start_worker", lambda: None)

    def on_request(request):
        if request.url.endswith("/api/queue/cancel"):
            requests.append(request.post_data_json)

    def on_dialog(dialog):
        dialogs.append(dialog.message)
        dialog.accept()

    try:
        base = web_app.ensure_started()
        page.on("request", on_request)
        page.on("dialog", on_dialog)
        page.goto(web_app.create_confirmation_url(base))
        page.locator(f'[data-operation-id="{child.operation_id}"] input[type="checkbox"]').uncheck()
        page.locator("#cancel-selected").click()
        expect(page.locator("#queue-message")).to_contain_text("已从本地审核队列取消")
        expect(page.locator(f'[data-operation-id="{producer.operation_id}"]')).to_have_count(0)
        assert queue.query.total_active() == 0
        assert len(dialogs) == len(requests) == 2
        assert requests[0]["operation_ids"] == [producer.operation_id]
        assert set(requests[1]["operation_ids"]) == {producer.operation_id, child.operation_id}
        assert all(request["expected_queue_revision"] == 2 for request in requests)
        assert all("cascade_confirmed" not in request for request in requests)
    finally:
        _stop()


def test_first_api_403_disables_controls_and_stops_polling(
    page, cfg, monkeypatch
) -> None:
    producer_id = "op-" + "7" * 32
    child_id = "op-" + "8" * 32
    state = _pending_state(producer_id, child_id)
    state_requests: list[str] = []
    _stop()
    cfg.web_port = _free_port()
    monkeypatch.setattr(web_app.execution_worker, "start_worker", lambda: None)
    try:
        base = web_app.ensure_started()

        def state_route(route):
            state_requests.append(route.request.url)
            if len(state_requests) == 1:
                route.fulfill(json=state, headers={"ETag": 'W/"q2-e0-h0"'})
            else:
                route.fulfill(status=403, json={"error": "审核会话无效"})

        page.route("**/api/state", state_route)
        page.route(
            "**/api/execution-history**",
            lambda route: route.fulfill(
                json={"items": [], "total": 0, "page": 1, "page_size": 50}
            ),
        )
        page.goto(web_app.create_confirmation_url(base))
        page.locator(f'[data-operation-id="{child_id}"]').wait_for()
        page.evaluate("loadState(true)")
        expect(page.locator("#queue-message")).to_contain_text("审核会话已失效")
        expect(page.locator("#submit-selected")).to_be_disabled()
        expect(page.locator("#cancel-selected")).to_be_disabled()
        expect(page.locator("#history-refresh")).to_be_disabled()
        request_count = len(state_requests)
        page.wait_for_timeout(1500)
        assert len(state_requests) == request_count
    finally:
        _stop()
