"""Process-local short-lived credentials for the localhost review page."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable


BOOTSTRAP_TTL_SECONDS = 60.0
SESSION_TTL_SECONDS = 15 * 60.0
RENEWAL_WINDOW_SECONDS = 60.0
SESSION_COOKIE = "jira_confirm_session"


@dataclass(frozen=True)
class SessionDecision:
    valid: bool
    renewed_session_id: str | None = None


class ReviewSessionManager:
    """Issue, validate, and rotate in-memory browser credentials."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        self._clock = clock
        self._token_factory = token_factory
        self._lock = threading.Lock()
        self._bootstrap_tokens: dict[str, float] = {}
        self._sessions: dict[str, float] = {}

    def _purge_expired_locked(self, now: float) -> None:
        for mapping in (self._bootstrap_tokens, self._sessions):
            for token, expires_at in list(mapping.items()):
                if expires_at <= now:
                    del mapping[token]

    def create_confirmation_url(self, base_url: str) -> str:
        token = self._token_factory(32)
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            self._bootstrap_tokens[token] = now + BOOTSTRAP_TTL_SECONDS
        return f"{base_url.rstrip('/')}/bootstrap/{token}"

    def exchange_bootstrap(self, token: str) -> str | None:
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            expires_at = self._bootstrap_tokens.pop(token, None)
            if expires_at is None or expires_at <= now:
                return None
            session_id = self._token_factory(32)
            self._sessions[session_id] = now + SESSION_TTL_SECONDS
            return session_id

    def authenticate(self, session_id: str | None) -> SessionDecision:
        if not session_id:
            return SessionDecision(False)
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            expires_at = self._sessions.get(session_id)
            if expires_at is None:
                return SessionDecision(False)
            if expires_at - now > RENEWAL_WINDOW_SECONDS:
                return SessionDecision(True)
            successor = self._token_factory(32)
            self._sessions[successor] = now + SESSION_TTL_SECONDS
            return SessionDecision(True, successor)

    def clear(self) -> None:
        """Clear process-local credentials; intended for controlled test reset."""
        with self._lock:
            self._bootstrap_tokens.clear()
            self._sessions.clear()


default_manager = ReviewSessionManager()


def credential_ttl_minutes() -> int:
    return int(SESSION_TTL_SECONDS // 60)
