"""Configuration derives only the current SQLite runtime paths."""
from __future__ import annotations

import os

import yaml

import config


def test_load_config_derives_sqlite_paths(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"jira": {"url": "https://jira.example.com", "auth_type": "pat", "token": "x"}}),
        encoding="utf-8",
    )
    cfg = config.load_config(str(path))
    assert cfg.operations_db_file.endswith(os.path.join("data", "operations.sqlite3"))
    assert cfg.mcp_logs_db_file.endswith(os.path.join("data", "mcp_logs.sqlite3"))
    assert not hasattr(cfg, "data_dir")
    assert not hasattr(cfg, "queue_file") and not hasattr(cfg, "commit_log_file")


def test_config_repr_and_sensitive_values_do_not_expose_credentials(cfg):
    assert "SECRET_TOKEN" not in repr(cfg)
    assert "jira.example.com" in cfg.sensitive_values()
