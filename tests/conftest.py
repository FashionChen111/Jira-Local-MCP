"""pytest 公共夹具：注入临时目录配置，重置队列/客户端单例。"""
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
    audit_dir = os.path.join(data_dir, "audit")
    os.makedirs(audit_dir, exist_ok=True)
    c = Config(
        jira=JiraConfig(url="https://jira.example.com", auth_type="pat", token="SECRET_TOKEN"),
        web_port=8766,
        expire_minutes=30,
        max_tasks_per_batch=20,
        max_txt_bytes=1024,
        log_dir=data_dir,
        commit_log_keep=50,
        data_dir=data_dir,
        queue_file=os.path.join(data_dir, "pending_queue.json"),
        commit_log_file=os.path.join(data_dir, "commit_log.json"),
        audit_dir=audit_dir,
    )
    config_mod.set_config(c)
    yield c
    config_mod.set_config(None)  # type: ignore


@pytest.fixture
def queue(cfg):
    from core import queue_manager

    queue_manager.reset_queue()
    q = queue_manager.get_queue()
    yield q
    queue_manager.reset_queue()
