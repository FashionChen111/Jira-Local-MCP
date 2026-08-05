"""Embedded localhost Web service readiness tests."""
from __future__ import annotations

import socket
from urllib.parse import urlparse

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


def test_ensure_started_is_ready_and_idempotent(cfg, monkeypatch):
    _stop()
    cfg.web_port = _free_port()
    monkeypatch.setattr(web_app.execution_worker, "start_worker", lambda: None)
    try:
        url = web_app.ensure_started()
        parsed = urlparse(url)
        assert parsed.hostname == "127.0.0.1"
        with socket.create_connection((parsed.hostname, parsed.port), timeout=1):
            pass
        assert web_app.ensure_started() == url
    finally:
        _stop()
