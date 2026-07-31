"""Web API 测试：严格模型、仅展示封存批次、确认与取消。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from core import commit_guard, jira_client, jira_executor
from web import app as web_app


def _enqueue_presented(queue):
    task = queue.enqueue(
        "add_comment", "PROJ-1", {"issue_key": "PROJ-1", "body": "x"}, "d"
    )
    queue.present_pending_batches()
    return task


def _client() -> TestClient:
    client = TestClient(web_app.create_app())
    bootstrap_url = web_app.create_confirmation_url("http://testserver")
    response = client.get(urlparse(bootstrap_url).path)
    assert response.status_code == 200
    return client


def test_confirmation_endpoints_require_browser_session(queue, monkeypatch):
    task = _enqueue_presented(queue)
    ping = MagicMock(return_value=True)
    monkeypatch.setattr(jira_client, "ping", ping)
    client = TestClient(web_app.create_app())

    assert client.get("/queue/all").status_code == 403
    assert client.get("/api/queue/all").status_code == 403
    response = client.post(
        "/api/commit",
        json={"batch_id": task.batch_id, "task_ids": [task.task_id], "nonce": "AAAAAAAA"},
    )
    assert response.status_code == 403
    assert client.post("/api/cancel", json={"batch_id": task.batch_id}).status_code == 403
    assert client.post(
        "/api/indeterminate/resolve",
        json={
            "batch_id": task.batch_id,
            "task_id": task.task_id,
            "resolution": "abandoned",
        },
    ).status_code == 403
    ping.assert_not_called()
    assert queue.get_task(task.batch_id, task.task_id).state == "pending"


def test_bootstrap_capability_is_single_use_and_sets_httponly_cookie():
    client = TestClient(web_app.create_app())
    bootstrap_url = web_app.create_confirmation_url("http://testserver")
    path = urlparse(bootstrap_url).path

    first = client.get(path, follow_redirects=False)
    replay = TestClient(web_app.create_app()).get(path, follow_redirects=False)

    assert first.status_code == 303
    assert first.headers["location"] == "/queue/all"
    cookie = first.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert replay.status_code == 403


def test_expired_browser_session_is_rejected():
    client = _client()
    session_id = client.cookies.get(web_app._SESSION_COOKIE)
    assert session_id is not None
    with web_app._session_lock:
        web_app._sessions[session_id] = 0

    assert client.get("/api/queue/all").status_code == 403


def test_api_queue_returns_only_presented_batches(queue):
    task = queue.enqueue(
        "add_comment", "PROJ-1", {"issue_key": "PROJ-1", "body": "x"}, "d"
    )
    client = _client()
    assert client.get("/api/queue/all").json()["batches"] == []

    queue.present_pending_batches()
    response = client.get("/api/queue/all")
    assert response.status_code == 200
    assert response.json()["batches"][0]["batch_id"] == task.batch_id
    assert response.json()["execution_busy"] is False


def test_api_queue_reports_execution_busy(queue):
    _enqueue_presented(queue)

    with commit_guard.execution_slot():
        response = _client().get("/api/queue/all")

    assert response.status_code == 200
    assert response.json()["execution_busy"] is True


def test_commit_rejects_malformed_or_extra_input(queue):
    task = _enqueue_presented(queue)
    client = _client()
    cases = [
        {"batch_id": "x", "task_ids": [task.task_id], "nonce": "AAAAAAAA"},
        {"batch_id": task.batch_id, "task_ids": "not-a-list", "nonce": "AAAAAAAA"},
        {"batch_id": task.batch_id, "task_ids": [], "nonce": "AAAAAAAA"},
        {"batch_id": task.batch_id, "task_ids": [task.task_id], "nonce": "bad"},
        {"batch_id": task.batch_id, "task_ids": [task.task_id], "nonce": "AAAAAAAA", "extra": 1},
    ]
    for body in cases:
        assert client.post("/api/commit", json=body).status_code == 422


def test_commit_rejects_empty_task_ids_before_jira_access(queue, monkeypatch):
    task = _enqueue_presented(queue)
    ping = MagicMock(return_value=True)
    monkeypatch.setattr(jira_client, "ping", ping)

    response = _client().post(
        "/api/commit",
        json={"batch_id": task.batch_id, "task_ids": [], "nonce": "AAAAAAAA"},
    )

    assert response.status_code == 422
    ping.assert_not_called()
    assert queue.get_task(task.batch_id, task.task_id).state == "pending"


def test_commit_rejects_more_than_1000_task_ids_before_jira_access(
    queue, monkeypatch
):
    task = _enqueue_presented(queue)
    ping = MagicMock(return_value=True)
    monkeypatch.setattr(jira_client, "ping", ping)
    task_ids = [f"task-{index:032x}" for index in range(1001)]

    response = _client().post(
        "/api/commit",
        json={
            "batch_id": task.batch_id,
            "task_ids": task_ids,
            "nonce": "AAAAAAAA",
        },
    )

    assert response.status_code == 422
    ping.assert_not_called()
    assert queue.get_task(task.batch_id, task.task_id).state == "pending"


def test_commit_wrong_nonce_400_without_jira_write(queue, monkeypatch):
    task = _enqueue_presented(queue)
    ping = MagicMock(return_value=True)
    jira = MagicMock()
    get_client = MagicMock(return_value=jira)
    monkeypatch.setattr(jira_client, "ping", ping)
    monkeypatch.setattr(jira_client, "get_client", get_client)

    response = _client().post(
        "/api/commit",
        json={"batch_id": task.batch_id, "task_ids": [task.task_id], "nonce": "AAAAAAAA"},
    )

    assert response.status_code == 400
    ping.assert_called_once_with()
    get_client.assert_called_once_with()
    jira.add_comment.assert_not_called()
    assert queue.get_task(task.batch_id, task.task_id).state == "pending"


def test_commit_rejects_duplicate_task_ids_before_any_jira_write(queue, monkeypatch):
    task = _enqueue_presented(queue)
    batch = queue.get_batch(task.batch_id)
    ping = MagicMock(return_value=True)
    jira = MagicMock()
    get_client = MagicMock(return_value=jira)
    monkeypatch.setattr(jira_client, "ping", ping)
    monkeypatch.setattr(jira_client, "get_client", get_client)

    response = _client().post(
        "/api/commit",
        json={
            "batch_id": task.batch_id,
            "task_ids": [task.task_id, task.task_id],
            "nonce": batch.nonce,
        },
    )

    assert response.status_code == 400
    assert response.json() == {"error": "task_ids 不得重复"}
    ping.assert_called_once_with()
    get_client.assert_called_once_with()
    jira.add_comment.assert_not_called()
    assert queue.get_task(task.batch_id, task.task_id).state == "pending"


def test_commit_rejects_mixed_valid_and_unknown_task_ids(queue, monkeypatch):
    first = queue.enqueue(
        "add_comment", "PROJ-1", {"issue_key": "PROJ-1", "body": "first"}, "first"
    )
    second = queue.enqueue(
        "add_comment", "PROJ-2", {"issue_key": "PROJ-2", "body": "second"}, "second"
    )
    queue.present_pending_batches()
    batch = queue.get_batch(first.batch_id)
    unknown_task_id = "task-" + "f" * 32
    jira = MagicMock()
    monkeypatch.setattr(jira_client, "ping", MagicMock(return_value=True))
    monkeypatch.setattr(jira_client, "get_client", MagicMock(return_value=jira))

    response = _client().post(
        "/api/commit",
        json={
            "batch_id": first.batch_id,
            "task_ids": [first.task_id, unknown_task_id, second.task_id],
            "nonce": batch.nonce,
        },
    )

    assert response.status_code == 400
    assert response.json() == {"error": "提交包含不存在的任务"}
    jira.add_comment.assert_not_called()
    assert queue.get_task(first.batch_id, first.task_id).state == "pending"
    assert queue.get_task(second.batch_id, second.task_id).state == "pending"


def test_commit_rejects_task_id_from_another_batch(queue, monkeypatch):
    first = _enqueue_presented(queue)
    second = _enqueue_presented(queue)
    first_batch = queue.get_batch(first.batch_id)
    jira = MagicMock()
    monkeypatch.setattr(jira_client, "ping", MagicMock(return_value=True))
    monkeypatch.setattr(jira_client, "get_client", MagicMock(return_value=jira))

    response = _client().post(
        "/api/commit",
        json={
            "batch_id": first.batch_id,
            "task_ids": [first.task_id, second.task_id],
            "nonce": first_batch.nonce,
        },
    )

    assert response.status_code == 400
    assert response.json() == {"error": "提交包含不存在的任务"}
    jira.add_comment.assert_not_called()
    assert queue.get_task(first.batch_id, first.task_id).state == "pending"
    assert queue.get_task(second.batch_id, second.task_id).state == "pending"


def test_commit_success_with_nonce(queue, monkeypatch):
    task = _enqueue_presented(queue)
    batch = queue.get_batch(task.batch_id)
    client = MagicMock()
    monkeypatch.setattr(jira_client, "ping", lambda: True)
    monkeypatch.setattr(jira_client, "get_client", lambda: client)

    response = _client().post(
        "/api/commit",
        json={"batch_id": task.batch_id, "task_ids": [task.task_id], "nonce": batch.nonce},
    )
    assert response.status_code == 200
    assert response.json()["ok_count"] == 1


def test_concurrent_commits_execute_each_task_only_once(queue, monkeypatch):
    task = _enqueue_presented(queue)
    batch = queue.get_batch(task.batch_id)
    entered_jira = threading.Event()
    release_jira = threading.Event()

    def delayed_add_comment(*_args):
        entered_jira.set()
        assert release_jira.wait(5)

    jira = MagicMock()
    jira.add_comment.side_effect = delayed_add_comment
    monkeypatch.setattr(jira_client, "ping", MagicMock(return_value=True))
    monkeypatch.setattr(jira_client, "get_client", MagicMock(return_value=jira))
    body = {
        "batch_id": task.batch_id,
        "task_ids": [task.task_id],
        "nonce": batch.nonce,
    }

    def commit():
        with _client() as client:
            return client.post("/api/commit", json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(commit)
        assert entered_jira.wait(5)
        second = pool.submit(commit)
        second_response = second.result(timeout=5)
        release_jira.set()
        first_response = first.result(timeout=5)

    assert sorted([first_response.status_code, second_response.status_code]) == [200, 409]
    assert second_response.json()["error"] == commit_guard.BUSY_MESSAGE
    jira.add_comment.assert_called_once_with("PROJ-1", "x")
    assert queue.get_task(task.batch_id, task.task_id) is None


def test_same_batch_disjoint_commits_are_serialized(queue, monkeypatch):
    first = queue.enqueue(
        "add_comment", "PROJ-1", {"issue_key": "PROJ-1", "body": "first"}, "first"
    )
    second = queue.enqueue(
        "add_comment", "PROJ-2", {"issue_key": "PROJ-2", "body": "second"}, "second"
    )
    queue.present_pending_batches()
    batch = queue.get_batch(first.batch_id)
    entered_jira = threading.Event()
    release_jira = threading.Event()

    def delayed_add_comment(_issue_key, body):
        if body == "first":
            entered_jira.set()
            assert release_jira.wait(5)

    jira = MagicMock()
    jira.add_comment.side_effect = delayed_add_comment
    monkeypatch.setattr(jira_client, "ping", MagicMock(return_value=True))
    monkeypatch.setattr(jira_client, "get_client", MagicMock(return_value=jira))

    def commit(task):
        with _client() as client:
            return client.post(
                "/api/commit",
                json={
                    "batch_id": task.batch_id,
                    "task_ids": [task.task_id],
                    "nonce": batch.nonce,
                },
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(commit, first)
        assert entered_jira.wait(5)
        blocked = commit(second)
        release_jira.set()
        completed = first_future.result(timeout=5)

    assert completed.status_code == 200
    assert blocked.status_code == 409
    assert blocked.json()["error"] == commit_guard.BUSY_MESSAGE
    assert queue.get_task(second.batch_id, second.task_id).state == "pending"

    retry = commit(second)
    assert retry.status_code == 200
    assert jira.add_comment.call_count == 2
    jira.add_comment.assert_any_call("PROJ-1", "first")
    jira.add_comment.assert_any_call("PROJ-2", "second")


def test_different_batches_are_serialized(queue, monkeypatch):
    first = _enqueue_presented(queue)
    second = _enqueue_presented(queue)
    first_batch = queue.get_batch(first.batch_id)
    second_batch = queue.get_batch(second.batch_id)
    entered_jira = threading.Event()
    release_jira = threading.Event()

    def delayed_add_comment(_issue_key, body):
        if body == "x" and not entered_jira.is_set():
            entered_jira.set()
            assert release_jira.wait(5)

    jira = MagicMock()
    jira.add_comment.side_effect = delayed_add_comment
    monkeypatch.setattr(jira_client, "ping", MagicMock(return_value=True))
    monkeypatch.setattr(jira_client, "get_client", MagicMock(return_value=jira))

    def commit(task, nonce):
        with _client() as client:
            return client.post(
                "/api/commit",
                json={
                    "batch_id": task.batch_id,
                    "task_ids": [task.task_id],
                    "nonce": nonce,
                },
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(commit, first, first_batch.nonce)
        assert entered_jira.wait(5)
        blocked = commit(second, second_batch.nonce)
        release_jira.set()
        completed = first_future.result(timeout=5)

    assert completed.status_code == 200
    assert blocked.status_code == 409
    assert queue.get_task(second.batch_id, second.task_id).state == "pending"
    assert commit(second, second_batch.nonce).status_code == 200


def test_cancel_only_allows_presented_batch(queue):
    open_task = queue.enqueue(
        "add_comment", "PROJ-1", {"issue_key": "PROJ-1", "body": "x"}, "d"
    )
    client = _client()
    assert client.post("/api/cancel", json={"batch_id": open_task.batch_id}).status_code == 404

    queue.present_pending_batches()
    response = client.post("/api/cancel", json={"batch_id": open_task.batch_id})
    assert response.status_code == 200
    assert response.json()["removed"] == 1


def test_api_exposes_recovered_task_for_manual_reconciliation(queue):
    task = _enqueue_presented(queue)
    batch = queue.get_batch(task.batch_id)
    queue.reserve_tasks(task.batch_id, [task.task_id], batch.nonce)

    from core import queue_manager

    queue_manager.reset_queue()
    response = _client().get("/api/queue/all")

    assert response.status_code == 200
    assert response.json()["batches"] == []
    attention = response.json()["attention_required"]
    assert len(attention) == 1
    assert attention[0]["task_id"] == task.task_id
    assert attention[0]["state"] == "indeterminate"
    assert attention[0]["recovery_reason"] == "process_restarted_during_execution"


def test_manual_not_applied_creates_recovery_batch_without_jira_write(
    queue, monkeypatch
):
    task = _enqueue_presented(queue)
    batch = queue.get_batch(task.batch_id)
    queue.reserve_tasks(task.batch_id, [task.task_id], batch.nonce)

    from core import queue_manager

    queue_manager.reset_queue()
    jira = MagicMock()
    monkeypatch.setattr(jira_client, "get_client", MagicMock(return_value=jira))
    client = _client()

    response = client.post(
        "/api/indeterminate/resolve",
        json={
            "batch_id": task.batch_id,
            "task_id": task.task_id,
            "resolution": "not_applied",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "pending"
    assert payload["task_id"] == task.task_id
    assert payload["recovery_task_id"] != task.task_id
    assert payload["recovery_batch_id"] != task.batch_id
    queue_payload = client.get("/api/queue/all").json()
    assert queue_payload["attention_required"] == []
    assert len(queue_payload["batches"]) == 1
    recovery = queue_payload["batches"][0]
    assert recovery["batch_id"] == payload["recovery_batch_id"]
    assert recovery["kind"] == "recovery"
    assert recovery["recovery_from"] == task.batch_id
    assert [item["task_id"] for item in recovery["tasks"]] == [
        payload["recovery_task_id"]
    ]
    jira.assert_not_called()


@pytest.mark.parametrize(
    ("resolution", "state", "status"),
    [
        ("succeeded", "succeeded", "reconciled_succeeded"),
        ("not_applied", "pending", "reconciled_not_applied"),
        ("abandoned", "cancelled", "reconciled_abandoned"),
    ],
)
def test_manual_resolution_writes_reconciliation_commit_log(
    queue, resolution, state, status
):
    task = _enqueue_presented(queue)
    batch = queue.get_batch(task.batch_id)
    reserved = queue.reserve_tasks(task.batch_id, [task.task_id], batch.nonce)[0]

    from core import queue_manager

    queue_manager.reset_queue()
    response = _client().post(
        "/api/indeterminate/resolve",
        json={
            "batch_id": task.batch_id,
            "task_id": task.task_id,
            "resolution": resolution,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == state
    assert payload["execution_id"] == reserved.execution_id
    if resolution == "not_applied":
        assert payload["recovery_task_id"] is not None
        assert payload["recovery_batch_id"] is not None
    else:
        assert payload["recovery_task_id"] is None
        assert payload["recovery_batch_id"] is None

    record = jira_executor.read_commit_log(1)[0]
    assert record["record_type"] == "reconciliation"
    assert record["batch_id"] == task.batch_id
    assert record["results"][0] == {
        "task_id": task.task_id,
        "operation_type": task.operation_type,
        "issue_key": task.issue_key,
        "status": status,
        "resolution": resolution,
        "execution_id": reserved.execution_id,
        "recovery_task_id": payload["recovery_task_id"],
        "recovery_batch_id": payload["recovery_batch_id"],
        "summary": record["results"][0]["summary"],
    }


def test_manual_resolution_rejects_pending_task(queue):
    task = _enqueue_presented(queue)

    response = _client().post(
        "/api/indeterminate/resolve",
        json={
            "batch_id": task.batch_id,
            "task_id": task.task_id,
            "resolution": "succeeded",
        },
    )

    assert response.status_code == 409
    assert queue.get_task(task.batch_id, task.task_id).state == "pending"


def test_manual_resolution_rejects_unknown_resolution(queue):
    task = _enqueue_presented(queue)

    response = _client().post(
        "/api/indeterminate/resolve",
        json={
            "batch_id": task.batch_id,
            "task_id": task.task_id,
            "resolution": "retry",
        },
    )

    assert response.status_code == 422


def test_confirmation_template_uses_no_innerhtml_for_api_data():
    from pathlib import Path

    template = (Path(__file__).parents[1] / "web" / "templates" / "confirm.html").read_text(encoding="utf-8")
    assert "innerHTML" not in template
    assert "outerHTML" not in template
    assert "insertAdjacentHTML" not in template
    assert "document.write" not in template
    assert "textContent" in template


def test_malicious_html_reaches_api_but_is_only_rendered_as_text(queue):
    payload = '<img src=x onerror="globalThis.pwned=true">'
    task = queue.enqueue(
        "add_comment",
        "PROJ-1",
        {"issue_key": "PROJ-1", "body": payload},
        payload,
    )
    queue.present_pending_batches()

    response = _client().get("/api/queue/all")

    assert response.status_code == 200
    returned = response.json()["batches"][0]["tasks"][0]
    assert returned["task_id"] == task.task_id
    assert returned["description"] == payload
    assert returned["payload"]["body"] == payload


def test_single_batch_page_and_api_are_not_exposed(queue):
    task = _enqueue_presented(queue)
    client = _client()

    assert client.get(f"/queue/{task.batch_id}").status_code == 404
    assert client.get(f"/api/queue/{task.batch_id}").status_code == 404


def test_all_batches_page_is_available():
    response = _client().get("/queue/all")
    assert response.status_code == 200
    assert "JIRA 变更确认" in response.text


def test_cancel_rejects_extra_fields(queue):
    task = _enqueue_presented(queue)
    response = _client().post(
        "/api/cancel",
        json={"batch_id": task.batch_id, "extra": "not allowed"},
    )
    assert response.status_code == 422
