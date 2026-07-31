"""真实浏览器中的 DOM XSS 与重复点击回归测试。"""
from __future__ import annotations

import json
import socket
import time
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route

from web import app as web_app


@pytest.fixture
def live_confirmation_url(cfg) -> Iterator[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        cfg.web_port = int(listener.getsockname()[1])

    web_app._server = None
    web_app._server_thread = None
    web_app._actual_port = None
    url = web_app.ensure_started()
    try:
        yield web_app.create_confirmation_url(url)
    finally:
        if web_app._server is not None:
            web_app._server.should_exit = True
        if web_app._server_thread is not None:
            web_app._server_thread.join(timeout=5)
        web_app._server = None
        web_app._server_thread = None
        web_app._actual_port = None


def _abort_tailwind(route: Route) -> None:
    route.abort()


def test_not_applied_replaces_attention_card_with_recovery_batch(
    page: Page, live_confirmation_url: str
):
    failed_batch_id = "batch-" + "a" * 32
    failed_task_id = "task-" + "b" * 32
    recovery_batch_id = "batch-" + "c" * 32
    recovery_task_id = "task-" + "d" * 32
    task = {
        "task_id": failed_task_id,
        "batch_id": failed_batch_id,
        "operation_type": "add_comment",
        "risk_level": "low",
        "issue_key": "PROJ-1",
        "payload": {"body": "恢复评论"},
        "description": "恢复评论",
        "created_at": 1,
        "source": "",
        "state": "indeterminate",
        "execution_reserved_at": 1,
    }
    resolved = False

    def queue_response(route: Route) -> None:
        if resolved:
            route.fulfill(json={
                "batches": [{
                    "batch_id": recovery_batch_id,
                    "nonce": "A1B2C3D4",
                    "created_at": 2,
                    "kind": "recovery",
                    "recovery_from": failed_batch_id,
                    "tasks": [{
                        **task,
                        "task_id": recovery_task_id,
                        "batch_id": recovery_batch_id,
                        "state": "pending",
                    }],
                }],
                "attention_required": [],
                "execution_busy": False,
            })
        else:
            route.fulfill(json={
                "batches": [],
                "attention_required": [task],
                "execution_busy": False,
            })

    def resolve_response(route: Route) -> None:
        nonlocal resolved
        resolved = True
        route.fulfill(json={
            "batch_id": failed_batch_id,
            "task_id": failed_task_id,
            "state": "pending",
            "resolution": "not_applied",
            "execution_id": "exec-1",
            "recovery_task_id": recovery_task_id,
            "recovery_batch_id": recovery_batch_id,
        })

    page.route("https://cdn.tailwindcss.com/**", _abort_tailwind)
    page.route("**/api/queue/all", queue_response)
    page.route("**/api/indeterminate/resolve", resolve_response)
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(live_confirmation_url, wait_until="domcontentloaded")

    page.get_by_role("button", name="确认未生效").click()
    label = page.locator(".recovery-label:not(.hidden)")
    label.wait_for()

    assert label.text_content() == "人工对账恢复批次 · 仅包含确认未生效的恢复任务"
    assert page.locator("#attention").get_attribute("class").find("hidden") >= 0
    assert page.locator(".nonce").input_value() == ""
    assert page.locator(".font-mono.text-lg").text_content() == "A1B2C3D4"


def test_api_values_remain_inert_in_real_browser_dom(
    page: Page, live_confirmation_url: str
):
    vectors = [
        "plain & text",
        "double\" and single' quotes",
        "<b>angle brackets</b>",
        '\"><img src=x onerror="globalThis.__xss=1">',
        "</script><script>globalThis.__xss=1</script>",
    ]
    batch_id = 'batch-\"><img src=x onerror="globalThis.__xss=1">'
    task_ids = [f"task-{vector}" for vector in vectors]
    batches = [{
        "batch_id": batch_id,
        "nonce": "A1B2C3D4",
        "created_at": 1,
        "tasks": [
            {
                "task_id": task_id,
                "batch_id": batch_id,
                "operation_type": vector,
                "risk_level": "low",
                "issue_key": vector,
                "payload": {"body": vector},
                "description": vector,
                "created_at": 1,
                "source": vector,
            }
            for task_id, vector in zip(task_ids, vectors, strict=True)
        ],
    }]

    page.route("https://cdn.tailwindcss.com/**", _abort_tailwind)
    page.route(
        "**/api/queue/all",
        lambda route: route.fulfill(json={"batches": batches}),
    )
    page.goto(live_confirmation_url, wait_until="domcontentloaded")
    page.locator(".risk-low").first.wait_for()

    assert page.evaluate("globalThis.__xss") is None
    assert page.locator("img").count() == 0
    assert page.locator("[onerror]").count() == 0
    assert page.locator(".risk-low").evaluate_all(
        "rows => rows.map(row => row.dataset.taskId)"
    ) == task_ids
    assert page.locator("section").first.get_attribute("data-batch-id") == batch_id
    rendered_descriptions = page.locator(".risk-low .text-sm.mt-1").all_text_contents()
    assert rendered_descriptions == vectors
    rendered_payloads = page.locator(".risk-low pre").all_text_contents()
    assert [json.loads(payload)["body"] for payload in rendered_payloads] == vectors


def test_rapid_repeated_click_sends_only_one_commit(
    page: Page, live_confirmation_url: str
):
    batch_id = "batch-" + "a" * 32
    task_id = "task-" + "b" * 32
    batches = [{
        "batch_id": batch_id,
        "nonce": "A1B2C3D4",
        "created_at": 1,
        "tasks": [{
            "task_id": task_id,
            "batch_id": batch_id,
            "operation_type": "add_comment",
            "risk_level": "low",
            "issue_key": "PROJ-1",
            "payload": {"body": "safe"},
            "description": "safe",
            "created_at": 1,
            "source": "",
        }],
    }]
    commit_requests = 0

    def handle_commit(route: Route) -> None:
        nonlocal commit_requests
        commit_requests += 1
        time.sleep(0.15)
        route.fulfill(json={
            "batch_id": batch_id,
            "results": [],
            "ok_count": 1,
            "fail_count": 0,
        })

    page.route("https://cdn.tailwindcss.com/**", _abort_tailwind)
    page.route(
        "**/api/queue/all",
        lambda route: route.fulfill(json={"batches": batches}),
    )
    page.route("**/api/commit", handle_commit)
    page.goto(live_confirmation_url, wait_until="domcontentloaded")
    page.locator("input.nonce").fill("A1B2C3D4")

    page.locator("button", has_text="全部确认").evaluate(
        "button => { button.click(); button.click(); }"
    )
    page.locator(".result").get_by_text("✅ 成功 1 · 失败 0").wait_for()

    assert commit_requests == 1


def test_busy_state_disables_all_batches_and_recovers(
    page: Page, live_confirmation_url: str
):
    batch_id = "batch-" + "a" * 32
    task_id = "task-" + "b" * 32
    busy = True
    batches = [{
        "batch_id": batch_id,
        "nonce": "A1B2C3D4",
        "created_at": 1,
        "tasks": [{
            "task_id": task_id,
            "batch_id": batch_id,
            "operation_type": "add_comment",
            "risk_level": "low",
            "issue_key": "PROJ-1",
            "payload": {"body": "safe"},
            "description": "safe",
            "created_at": 1,
            "source": "",
        }],
    }]

    def handle_queue(route: Route) -> None:
        route.fulfill(json={"batches": batches, "execution_busy": busy})

    page.route("https://cdn.tailwindcss.com/**", _abort_tailwind)
    page.route("**/api/queue/all", handle_queue)
    page.goto(live_confirmation_url, wait_until="domcontentloaded")
    page.locator("#execution-busy").wait_for(state="visible")

    card = page.locator(f'section[data-batch-id="{batch_id}"]')
    assert card.locator("input.nonce").is_disabled()
    assert card.locator("input.chk").is_disabled()
    assert card.locator("button").evaluate_all(
        "buttons => buttons.every(button => button.disabled)"
    )

    busy = False
    page.evaluate("fetchQueue()")
    page.locator("#execution-busy").wait_for(state="hidden")

    assert card.locator("input.nonce").is_enabled()
    assert card.locator("input.chk").is_enabled()
    assert card.locator("button", has_text="全部确认").is_enabled()


def test_nonce_input_survives_polling_and_is_isolated_by_batch(
    page: Page, live_confirmation_url: str
):
    first_batch_id = "batch-" + "a" * 32
    second_batch_id = "batch-" + "b" * 32

    def make_task(batch_id: str, suffix: str) -> dict:
        return {
            "task_id": "task-" + suffix * 32,
            "batch_id": batch_id,
            "operation_type": "add_comment",
            "risk_level": "low",
            "issue_key": "PROJ-1",
            "payload": {"body": suffix},
            "description": suffix,
            "created_at": 1,
            "source": "",
        }

    batches = [
        {
            "batch_id": first_batch_id,
            "nonce": "A1B2C3D4",
            "created_at": 1,
            "tasks": [make_task(first_batch_id, "a")],
        },
        {
            "batch_id": second_batch_id,
            "nonce": "B1C2D3E4",
            "created_at": 2,
            "tasks": [make_task(second_batch_id, "b")],
        },
    ]

    page.route("https://cdn.tailwindcss.com/**", _abort_tailwind)
    page.route(
        "**/api/queue/all",
        lambda route: route.fulfill(json={"batches": batches}),
    )
    page.goto(live_confirmation_url, wait_until="domcontentloaded")

    first_card = page.locator(f'section[data-batch-id="{first_batch_id}"]')
    second_card = page.locator(f'section[data-batch-id="{second_batch_id}"]')
    first_input = first_card.locator("input.nonce")
    second_input = second_card.locator("input.nonce")
    first_input.fill("A1B2")
    second_input.fill("B1C2")
    first_input.focus()
    first_input.evaluate("input => input.setSelectionRange(2, 2)")

    batches[0]["tasks"].append(make_task(first_batch_id, "c"))
    page.wait_for_timeout(3200)

    assert first_input.input_value() == "A1B2"
    assert second_input.input_value() == "B1C2"
    assert first_card.locator(".risk-low").count() == 2
    assert first_input.evaluate("input => document.activeElement === input")
    assert first_input.evaluate("input => input.selectionStart") == 2

    batches.pop(0)
    page.wait_for_timeout(3200)
    assert page.locator(f'section[data-batch-id="{first_batch_id}"]').count() == 0
    assert second_input.input_value() == "B1C2"

    batches.insert(0, {
        "batch_id": first_batch_id,
        "nonce": "A1B2C3D4",
        "created_at": 1,
        "tasks": [make_task(first_batch_id, "a")],
    })
    page.wait_for_timeout(3200)
    recreated_input = page.locator(
        f'section[data-batch-id="{first_batch_id}"] input.nonce'
    )
    assert recreated_input.input_value() == ""
    assert second_input.input_value() == "B1C2"
