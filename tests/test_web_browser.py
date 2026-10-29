"""Real-browser regressions for safe rendering and the read-only automatic queue."""

from __future__ import annotations

import socket
from pathlib import Path
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
        payload={"project_key": "PROJ", "issue_type_id": "10001", "summary": "new"},
        description="create",
    )
    child = _operation(
        child_id,
        issue_key=producer_id,
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


def test_warning_clone_history_and_review_metadata_render_as_text(page, cfg, monkeypatch, tmp_path):
    malicious = '<img src=x onerror="window.injected=true">'
    clone = _operation("op-" + "a" * 32, operation_type="clone_issue", payload={"field_overrides": {"customfield_1": "value"}})
    worklog = _operation("op-" + "b" * 32, operation_type="add_worklog", payload={"adjust_estimate": "new", "new_estimate": "3h"})
    transition = _operation("op-" + "c" * 32, operation_type="transition_issue", payload={"transition_id": "21"})
    history = {**clone, "outcome": "warning", "new_issue_key": "PROJ-20", "started_at": 1,
               "attachment_results": [
                   {"source_attachment_id": "1", "requested_filename": malicious, "status": "failed", "error": malicious, "attachment": None},
                   {"source_attachment_id": "2", "requested_filename": "later.txt", "status": "unattempted", "error": None, "attachment": None}]}
    try:
        base = _serve(page, cfg, monkeypatch, _state([], [clone, worklog, transition]))
        page.unroute("**/api/execution-history**")
        page.route("**/api/execution-history**", lambda route: route.fulfill(json={"items": [history], "total": 1, "page": 1, "page_size": 50}))
        page.goto(web_app.create_confirmation_url(base))
        expect(page.locator(".status.warning")).to_have_text("成功（有异常）")
        expect(page.get_by_text("已创建事务：PROJ-20", exact=True)).to_be_visible()
        expect(page.get_by_text("估算影响：重设剩余估算 3h", exact=True)).to_be_visible()
        expect(page.get_by_text("transition_id：21", exact=True)).to_be_visible()
        expect(page.get_by_text("克隆覆盖字段：customfield_1", exact=True).first).to_be_visible()
        expect(page.get_by_text("附件处理：later.txt（源 ID 2）：未尝试", exact=True)).to_be_visible()
        assert page.locator("img").count() == 0
        assert page.evaluate("window.injected") is None
        screenshot = tmp_path / "warning-clone-review.png"
        page.screenshot(path=str(screenshot), full_page=True)
    finally:
        _stop()


def test_attachment_recovery_submits_confirmed_identity(page, cfg, monkeypatch):
    operation = {
        **_operation("op-" + "a" * 32, operation_type="add_attachment",
                     payload={"file_path": "C:\\fixtures\\a.jpg"}),
        "state": "indeterminate", "execution_id": "exec-test",
        "submission_risk_level": "medium", "stage_order": 1,
    }
    state = _state([])
    state["execution"]["indeterminate"] = [operation]
    answers = iter(["55", "a-final.jpg"])
    try:
        base = _serve(page, cfg, monkeypatch, state)
        page.route("**/api/execution/indeterminate/resolve",
                   lambda route: route.fulfill(json={"attachment_results": []}))
        page.on("dialog", lambda dialog: dialog.accept(next(answers)))
        page.goto(web_app.create_confirmation_url(base))
        with page.expect_request("**/api/execution/indeterminate/resolve") as request:
            page.get_by_role("button", name="已生效", exact=True).click()
        body = request.value.post_data_json
        assert body["attachment"] == {"id": "55", "filename": "a-final.jpg"}
        assert "attachment_result" not in body
    finally:
        _stop()


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


@pytest.mark.parametrize("reverse_dependencies", [False, True])
def test_pending_selection_automatically_submits_dependency_closure(
    page, cfg, monkeypatch, reverse_dependencies
) -> None:
    producer_id = "op-" + "1" * 32
    child_id = "op-" + "2" * 32
    state = _pending_state(producer_id, child_id)
    attachment_id = "op-" + "3" * 32
    operations = state["queue"]["groups"][0]["operations"]
    operations.insert(1, _operation(
        attachment_id, operation_type="add_attachment", issue_key=producer_id,
        dependency_states={producer_id: "queued"},
    ))
    dependency_ids = [producer_id, attachment_id]
    if reverse_dependencies:
        dependency_ids.reverse()
    operations[-1]["dependency_states"] = {
        operation_id: "queued" for operation_id in dependency_ids
    }
    submitted: list[dict] = []
    try:
        base = _serve(page, cfg, monkeypatch, state)

        def submit_route(route):
            submitted.append(route.request.post_data_json)
            route.fulfill(status=202, json={
                "accepted_operation_ids": [producer_id, attachment_id, child_id]
            })

        page.route("**/api/queue/submit", submit_route)
        page.goto(web_app.create_confirmation_url(base))
        producer_box = page.locator(
            f'[data-operation-id="{producer_id}"] input[type="checkbox"]'
        )
        child_box = page.locator(
            f'[data-operation-id="{child_id}"] input[type="checkbox"]'
        )
        attachment_box = page.locator(
            f'[data-operation-id="{attachment_id}"] input[type="checkbox"]'
        )
        expect(producer_box).to_be_checked()
        expect(child_box).to_be_checked()
        producer_box.uncheck()
        expect(producer_box).not_to_be_checked()
        expect(child_box).not_to_be_checked()
        expect(attachment_box).not_to_be_checked()
        child_box.check()
        expect(child_box).to_be_checked()
        expect(producer_box).to_be_checked()
        expect(attachment_box).to_be_checked()
        dependency_label = page.locator(
            f'[data-operation-id="{child_id}"] .dependency'
        )
        for operation_id in dependency_ids:
            expect(dependency_label).to_contain_text(f"{operation_id}: queued")
        page.locator("#submit-selected").click()
        deadline = time.monotonic() + 5
        while not submitted and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert set(submitted[0]["operation_ids"]) == {
            producer_id, attachment_id, child_id
        }
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
        payload={"transition_id": "10001", "fields": {}, "comment": None},
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


@pytest.mark.parametrize("outcome", ["initial_decline", "scope_decline", "escape", "success", "revision_conflict"])
def test_cancel_scope_confirmation_and_original_revision(page, cfg, monkeypatch, outcome):
    producer_id = "op-" + "a" * 32
    child_id = "op-" + "b" * 32
    state = _pending_state(producer_id, child_id)
    requests: list[dict] = []
    dialogs: list[str] = []

    def on_dialog(dialog):
        dialogs.append(dialog.message)
        if outcome == "initial_decline":
            dialog.dismiss()
        else:
            dialog.accept()

    try:
        base = _serve(page, cfg, monkeypatch, state)

        def cancel_route(route):
            requests.append(route.request.post_data_json)
            if len(requests) == 1:
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
        page.locator("#cancel-selected").click()
        scope = page.get_by_role("dialog", name="确认取消范围")
        if outcome == "initial_decline":
            assert requests == []
            expect(scope).not_to_be_visible()
        else:
            expect(scope).to_be_visible()
            expect(scope.locator(".cancel-scope-label")).to_have_text(["原先选择", "新增依赖"])
            expect(scope.locator("#cancel-scope-summary")).to_have_text("原先选择 1 项，新增依赖 1 项，共 2 项。")
            expect(scope.locator("#cancel-scope-back")).to_be_focused()
            expect(scope).to_contain_text(f"待创建操作 {producer_id}")
            assert scope.locator("input, pre").count() == 0
            # Calling another entry while a confirmation is pending sends nothing.
            page.evaluate("cancelOperations(['ignored'], 99)")
            if outcome == "revision_conflict":
                state["revisions"]["queue"] = 3
                state["queue"]["groups"][0]["operations"][1]["description"] = "轮询更新后的说明"
                page.evaluate("loadState(true)")
                expect(scope).to_contain_text("comment pending")
                expect(scope).not_to_contain_text("轮询更新后的说明")
            if outcome == "scope_decline":
                scope.get_by_role("button", name="返回", exact=True).click()
            elif outcome == "escape":
                page.keyboard.press("Escape")
            else:
                # Two synchronous clicks must still produce just one submission.
                scope.locator("#cancel-scope-confirm").evaluate("node => { node.click(); node.click(); }")
            expect(scope).not_to_be_visible()
            if outcome in ("scope_decline", "escape"):
                expect(page.locator("#cancel-selected")).to_be_focused()
                assert len(requests) == 1
            elif outcome == "revision_conflict":
                expect(page.locator("#queue-message")).to_contain_text("队列已更新")
                assert len(requests) == 2
            else:
                expect(page.locator("#queue-message")).to_contain_text("已从本地审核队列取消")
                expect(page.locator(f'[data-operation-id="{producer_id}"]')).to_have_count(0)
                assert len(requests) == 2
            assert requests[0] == {"operation_ids": [producer_id], "expected_queue_revision": 2}
            if len(requests) == 2:
                assert requests[1] == {
                    "operation_ids": [producer_id, child_id], "expected_queue_revision": 2,
                }
        assert len(dialogs) == 1
    finally:
        _stop()


def test_browser_cancels_real_queue_dependency_scope(page, queue, cfg, monkeypatch, tmp_path):
    producer = queue.queue.enqueue(
        "create_issue", None,
        {"project_key": "PROJ", "issue_type_id": "10001", "summary": "审核附件上传流程"},
        "创建任务：审核附件上传流程", "test",
    )
    attachment_path = tmp_path / "review.txt"
    attachment_path.write_text("review evidence", encoding="utf-8")
    attachment = queue.queue.enqueue(
        "add_attachment", producer.operation_id, {"file_path": str(attachment_path)},
        "上传附件：review.txt", "test",
    )
    child = queue.queue.enqueue(
        "add_comment", producer.operation_id,
        {"body": "请核对附件 [^{{attachment:" + attachment.operation_id + "}}]"},
        "添加评论：请核对附件并记录审核结果", "test",
    )
    requests: list[dict] = []
    _stop()
    cfg.web_port = _free_port()
    monkeypatch.setattr(web_app.execution_worker, "start_worker", lambda: None)

    def on_request(request):
        if request.url.endswith("/api/queue/cancel"):
            requests.append(request.post_data_json)

    try:
        base = web_app.ensure_started()
        page.on("request", on_request)
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(web_app.create_confirmation_url(base))
        page.locator(f'[data-operation-id="{child.operation_id}"] input[type="checkbox"]').uncheck()
        page.locator(f'[data-operation-id="{attachment.operation_id}"] input[type="checkbox"]').uncheck()
        page.locator("#cancel-selected").click()
        scope = page.get_by_role("dialog", name="确认取消范围")
        expect(scope.locator(".cancel-scope-label")).to_have_text(["原先选择", "新增依赖", "新增依赖"])
        screenshots = Path(".pytest-tmp/screenshots")
        screenshots.mkdir(parents=True, exist_ok=True)
        page.set_viewport_size({"width": 1280, "height": 900})
        page.screenshot(path=str(screenshots / "cancel-scope-desktop.png"))
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(screenshots / "cancel-scope-mobile.png"))
        assert scope.evaluate("node => node.scrollWidth <= node.clientWidth")
        scope.get_by_role("button", name="确认取消全部 3 项").click()
        expect(page.locator("#queue-message")).to_contain_text("已从本地审核队列取消")
        assert queue.query.total_active() == 0
        assert len(requests) == 2
        assert requests[0]["operation_ids"] == [producer.operation_id]
        assert set(requests[1]["operation_ids"]) == {
            producer.operation_id, attachment.operation_id, child.operation_id,
        }
        assert all(request["expected_queue_revision"] == 3 for request in requests)
        assert all("cascade_confirmed" not in request for request in requests)
    finally:
        _stop()


@pytest.mark.parametrize("failure", ["first_request", "confirmed_request", "session_expired"])
def test_cancel_failures_release_flow_without_retry(page, cfg, monkeypatch, failure):
    producer_id, child_id = "op-" + "a" * 32, "op-" + "b" * 32
    state = _pending_state(producer_id, child_id)
    requests: list[dict] = []
    try:
        base = _serve(page, cfg, monkeypatch, state)

        def cancel_route(route):
            requests.append(route.request.post_data_json)
            if failure == "first_request" or len(requests) >= 2:
                route.abort("failed")
            else:
                route.fulfill(status=409, json={
                    "code": "dependency_scope_incomplete",
                    "affected_operation_ids": [producer_id, child_id],
                    "missing_operation_ids": [child_id],
                })

        page.route("**/api/queue/cancel", cancel_route)
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(web_app.create_confirmation_url(base))
        page.locator(f'[data-operation-id="{child_id}"] input').uncheck()
        page.locator("#cancel-selected").click()
        scope = page.get_by_role("dialog", name="确认取消范围")
        if failure != "first_request":
            expect(scope).to_be_visible()
            if failure == "session_expired":
                page.route("**/api/state", lambda route: route.fulfill(status=403, json={"error": "expired"}))
                page.evaluate("loadState(true)")
            else:
                scope.locator("#cancel-scope-confirm").click()
        expect(scope).not_to_be_visible()
        expect(page.locator("#queue-message")).to_contain_text(
            "审核会话已失效" if failure == "session_expired" else "取消请求失败",
        )
        if failure != "session_expired":
            expect(page.locator("#cancel-selected")).to_be_enabled()
        assert len(requests) == (2 if failure == "confirmed_request" else 1)
        if failure == "session_expired":
            expect(page.locator("#cancel-selected")).to_be_disabled()
            page.evaluate("cancelOperations(['ignored'], 2)")
            assert len(requests) == 1
        else:
            expect(page.locator("#cancel-selected")).to_be_enabled()
            # Recovery of the entry permits a new explicit attempt, without auto-retrying.
            page.locator("#cancel-selected").click()
            expect(page.locator("#queue-message")).to_contain_text("取消请求失败")
            expect(page.locator("#cancel-selected")).to_be_enabled()
            assert len(requests) == (3 if failure == "confirmed_request" else 2)
    finally:
        _stop()


@pytest.mark.parametrize("scenario", ["missing_snapshot", "too_many", "long_list"])
def test_cancel_preview_limits_and_safe_text(page, cfg, monkeypatch, scenario):
    producer_id = "op-" + "a" * 32
    count = 1001 if scenario == "too_many" else 25
    ids = [producer_id] + [f"op-{index:032x}" for index in range(1, count)]
    malicious = '<img src=x onerror="window.injected=true">' + "很长的操作说明" * 30
    operations = [
        _operation(
            item, description=malicious if index else "取消范围审核",
            target_issue_key="PROJ-99",
            dependency_states={producer_id: "queued"} if index else {},
        )
        for index, item in enumerate(ids)
    ]
    state = _state([], operations)
    requests: list[dict] = []
    affected = ids if scenario != "missing_snapshot" else [*ids, "op-" + "f" * 32]
    try:
        base = _serve(page, cfg, monkeypatch, state)

        def cancel_route(route):
            requests.append(route.request.post_data_json)
            route.fulfill(status=409, json={
                "code": "dependency_scope_incomplete",
                "affected_operation_ids": affected, "missing_operation_ids": affected[1:],
            })

        page.route("**/api/queue/cancel", cancel_route)
        page.goto(web_app.create_confirmation_url(base))
        # Select a producer without thousands of individual checkbox interactions.
        expect(page.locator(f'[data-operation-id="{producer_id}"]')).to_be_visible()
        page.evaluate("id => { explicitSelected.clear(); setExplicitSelection(id, true); }", producer_id)
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator("#cancel-selected").click()
        scope = page.get_by_role("dialog", name="确认取消范围")
        if scenario == "missing_snapshot":
            expect(page.locator("#queue-message")).to_contain_text("展示记录不完整")
            expect(scope).not_to_be_visible()
            expect(page.locator("#cancel-selected")).to_be_enabled()
        else:
            expect(scope).to_be_visible()
            expect(scope.locator(".cancel-scope-item")).to_have_count(count)
            expect(scope.locator(".operation-title").nth(1)).to_have_text(malicious)
            expect(scope).to_contain_text("目标事务：PROJ-99")
            assert scope.locator("img, input, pre").count() == 0
            assert page.evaluate("window.injected") is None
            page.set_viewport_size({"width": 390, "height": 844})
            assert scope.evaluate("node => node.scrollWidth <= node.clientWidth")
            assert scope.locator("#cancel-scope-list").evaluate("node => node.scrollHeight > node.clientHeight")
            if scenario == "too_many":
                expect(scope.locator("#cancel-scope-error")).to_contain_text("1000 项限制")
                expect(scope.locator("#cancel-scope-confirm")).to_be_disabled()
            scope.get_by_role("button", name="返回", exact=True).click()
            expect(page.locator("#cancel-selected")).to_be_focused()
        assert len(requests) == 1
    finally:
        _stop()


def test_single_cancel_entry_restores_focus_after_polling(page, cfg, monkeypatch):
    producer_id, child_id = "op-" + "a" * 32, "op-" + "b" * 32
    state = _pending_state(producer_id, child_id)
    state["queue"]["groups"][0]["operations"][0]["submittable"] = False
    requests: list[dict] = []
    try:
        base = _serve(page, cfg, monkeypatch, state)

        def cancel_route(route):
            requests.append(route.request.post_data_json)
            route.fulfill(status=409, json={
                "code": "dependency_scope_incomplete",
                "affected_operation_ids": [producer_id, child_id],
                "missing_operation_ids": [child_id],
            })

        page.route("**/api/queue/cancel", cancel_route)
        page.goto(web_app.create_confirmation_url(base))
        trigger = page.locator(f'[data-operation-id="{producer_id}"] .queue-cancel')
        trigger.click()
        scope = page.get_by_role("dialog", name="确认取消范围")
        expect(scope).to_be_visible()
        page.evaluate("loadState(true)")
        expect(trigger).to_be_disabled()
        page.keyboard.press("Escape")
        expect(trigger).to_be_enabled()
        expect(trigger).to_be_focused()
        assert requests == [{"operation_ids": [producer_id], "expected_queue_revision": 2}]
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
