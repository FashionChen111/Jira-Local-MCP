"""Shared fixtures with all runtime output under pytest's tmp_path."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_mod
from config import Config, JiraConfig


@pytest.fixture
def cfg(tmp_path):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    value = Config(
        jira=JiraConfig(
            url="https://jira.example.com", auth_type="pat", token="SECRET_TOKEN"
        ),
        web_port=8766,
        max_txt_bytes=1024,
        operations_db_file=os.path.join(data_dir, "operations.sqlite3"),
        mcp_logs_db_file=os.path.join(data_dir, "mcp_logs.sqlite3"),
        execution_lock_file=os.path.join(data_dir, "jira_execution.lock"),
    )
    config_mod.set_config(value)
    from core.runtime_logger import reset_logger
    from core.storage.database import reset_database_initialization
    from core.operations import reset_services
    from web.review_sessions import default_manager
    reset_database_initialization()
    reset_logger()
    reset_services()
    default_manager.clear()
    yield value
    from core.executing import worker as execution_worker
    execution_worker.stop_worker(timeout=2)
    reset_services()
    reset_database_initialization()
    reset_logger()
    config_mod.set_config(None)


@pytest.fixture
def queue(cfg):
    from core.operations import get_services
    return get_services()


@pytest.fixture
def web_client(cfg):
    from fastapi.testclient import TestClient
    from web import app as web_app
    from web.review_sessions import default_manager
    default_manager.clear()
    client = TestClient(web_app.create_app())
    bootstrap_url = web_app.create_confirmation_url("http://testserver")
    response = client.get(bootstrap_url, follow_redirects=False)
    assert response.status_code == 303
    return client
