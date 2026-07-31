"""内嵌 Web 服务启动和就绪性测试。"""
from __future__ import annotations

import socket
import time
from urllib.parse import urlparse

import pytest

from web import app as web_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _stop_web_server() -> None:
    server = web_app._server
    thread = web_app._server_thread
    if server is not None:
        server.should_exit = True
    if thread is not None:
        thread.join(timeout=5)
    web_app._server = None
    web_app._server_thread = None
    web_app._actual_port = None


def test_ensure_started_returns_only_after_port_is_connectable(cfg):
    _stop_web_server()
    cfg.web_port = _free_port()
    try:
        url = web_app.ensure_started()
        parsed = urlparse(url)

        with socket.create_connection((parsed.hostname, parsed.port), timeout=1):
            pass

        assert web_app.ensure_started() == url
        assert web_app._server.started is True
        assert web_app._server_thread is not None
        assert web_app._server_thread.is_alive()
    finally:
        _stop_web_server()


class _DelayedServer:
    def __init__(self, config, *, startup_delay: float = 0.05):
        self.config = config
        self.startup_delay = startup_delay
        self.started = False
        self.should_exit = False

    def run(self) -> None:
        time.sleep(self.startup_delay)
        self.started = True
        while not self.should_exit:
            time.sleep(0.01)


def test_ensure_started_waits_for_its_uvicorn_server(cfg, monkeypatch):
    import uvicorn

    _stop_web_server()
    cfg.web_port = _free_port()
    fake_server = _DelayedServer(object())
    monkeypatch.setattr(uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", lambda config: fake_server)

    started_at = time.monotonic()
    try:
        web_app.ensure_started()

        assert time.monotonic() - started_at >= fake_server.startup_delay
        assert fake_server.started is True
    finally:
        _stop_web_server()


def test_ensure_started_cleans_up_after_startup_timeout(cfg, monkeypatch):
    import uvicorn

    _stop_web_server()
    cfg.web_port = _free_port()
    fake_server = _DelayedServer(object(), startup_delay=1)
    monkeypatch.setattr(uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", lambda config: fake_server)
    monkeypatch.setattr(web_app, "_STARTUP_TIMEOUT_SECONDS", 0.02)

    with pytest.raises(RuntimeError, match="启动超时"):
        web_app.ensure_started()

    assert fake_server.should_exit is True
    assert web_app._server is None
    assert web_app._server_thread is None
    assert web_app._actual_port is None
