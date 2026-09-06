"""Rolling review-session lifetime and HTTP renewal behavior."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web import app as web_app
from web.review_sessions import ReviewSessionManager


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Tokens:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, _bytes: int) -> str:
        self.count += 1
        return f"credential-{self.count}"


def _new_manager() -> tuple[ReviewSessionManager, _Clock]:
    clock = _Clock()
    return ReviewSessionManager(clock=clock, token_factory=_Tokens()), clock


def test_session_rotates_near_expiry_without_extending_old_id():
    manager, clock = _new_manager()
    bootstrap_url = manager.create_confirmation_url("http://testserver")
    bootstrap = bootstrap_url.rsplit("/", 1)[-1]
    original = manager.exchange_bootstrap(bootstrap)
    assert original is not None
    assert manager.exchange_bootstrap(bootstrap) is None

    clock.advance(839)
    assert manager.authenticate(original).renewed_session_id is None
    clock.advance(1)
    decision = manager.authenticate(original)
    assert decision.valid is True
    assert decision.renewed_session_id is not None
    successor = decision.renewed_session_id

    clock.advance(59.9)
    late_decision = manager.authenticate(original)
    assert late_decision.valid is True
    assert late_decision.renewed_session_id is not None
    clock.advance(0.1)
    assert manager.authenticate(original).valid is False
    assert manager.authenticate(successor).valid is True

    restarted = ReviewSessionManager(clock=clock, token_factory=_Tokens())
    assert restarted.authenticate(successor).valid is False

    clock.advance(900)
    assert manager.authenticate(successor).valid is False
    assert manager.authenticate(late_decision.renewed_session_id).valid is False


def test_etag_304_request_renews_cookie_at_the_60_second_boundary(cfg):
    manager, clock = _new_manager()
    client = TestClient(web_app.create_app(session_manager=manager))
    bootstrap_url = manager.create_confirmation_url("http://testserver")
    bootstrap = client.get(bootstrap_url, follow_redirects=False)
    assert bootstrap.status_code == 303
    original = client.cookies.get("jira_confirm_session")

    first = client.get("/api/state")
    assert first.status_code == 200
    clock.advance(840)
    renewed = client.get(
        "/api/state", headers={"If-None-Match": first.headers["etag"]}
    )

    assert renewed.status_code == 304
    assert "Max-Age=900" in renewed.headers["set-cookie"]
    assert client.cookies.get("jira_confirm_session") != original


@pytest.mark.parametrize(
    "method,path,json_body,expected_status",
    [
        ("GET", "/queue", None, 200),
        ("GET", "/api/execution-history", None, 200),
        (
            "POST",
            "/api/queue/cancel",
            {
                "expected_queue_revision": 0,
                "operation_ids": ["op-" + "c" * 32],
            },
            409,
        ),
    ],
)
def test_page_read_api_and_mutation_api_all_roll_credentials(
    cfg, method, path, json_body, expected_status
):
    manager, clock = _new_manager()
    client = TestClient(web_app.create_app(session_manager=manager))
    bootstrap_url = manager.create_confirmation_url("http://testserver")
    assert client.get(bootstrap_url, follow_redirects=False).status_code == 303
    original = client.cookies.get("jira_confirm_session")
    clock.advance(840)

    response = client.request(method, path, json=json_body)

    assert response.status_code == expected_status
    assert "Max-Age=900" in response.headers["set-cookie"]
    assert client.cookies.get("jira_confirm_session") != original
