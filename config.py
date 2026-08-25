"""Validated local configuration and derived runtime paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
SERVICE_VERSION = "2.1.0"


def _abs_path(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


@dataclass
class JiraConfig:
    url: str
    auth_type: str
    token: str = ""
    username: str = ""
    password: str = ""
    verify_ssl: bool = True
    timeout: int = 30

    def __repr__(self) -> str:
        return f"JiraConfig(url=***, auth_type={self.auth_type!r})"


@dataclass
class Config:
    jira: JiraConfig
    web_port: int = 8766
    max_txt_bytes: int = 262144
    operations_db_file: str = ""
    mcp_logs_db_file: str = ""
    execution_lock_file: str = ""

    def sensitive_values(self) -> list[str]:
        values = [self.jira.token, self.jira.password, self.jira.username]
        if self.jira.url:
            values.append(self.jira.url.split("://")[-1].split("/")[0])
        return [value for value in values if value]

    def __repr__(self) -> str:
        return f"Config(jira={self.jira!r}, web_port={self.web_port})"


_config: Config | None = None


def _validate(raw: dict[str, Any]) -> None:
    if "jira" not in raw or not isinstance(raw["jira"], dict):
        raise ValueError("config.yaml 缺少 [jira] 配置段")
    jira = raw["jira"]
    if not jira.get("url"):
        raise ValueError("config.yaml 缺少 jira.url")
    auth_type = jira.get("auth_type", "pat")
    if auth_type not in ("pat", "basic"):
        raise ValueError("jira.auth_type 必须是 pat 或 basic")
    if auth_type == "pat" and not jira.get("token"):
        raise ValueError("auth_type=pat 时必须提供 jira.token")
    if auth_type == "basic" and not (jira.get("username") and jira.get("password")):
        raise ValueError("auth_type=basic 时必须提供 jira.username 和 jira.password")


def load_config(path: str | None = None) -> Config:
    cfg_path = path or CONFIG_PATH
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(
            f"未找到配置文件 {cfg_path}，请复制 config.example.yaml 为 config.yaml 并填写。"
        )
    with open(cfg_path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    _validate(raw)
    jira_raw = raw["jira"]
    jira = JiraConfig(
        url=jira_raw["url"].rstrip("/"),
        auth_type=jira_raw.get("auth_type", "pat"),
        token=jira_raw.get("token", "") or "",
        username=jira_raw.get("username", "") or "",
        password=jira_raw.get("password", "") or "",
        verify_ssl=bool(jira_raw.get("verify_ssl", True)),
        timeout=int(jira_raw.get("timeout", 30)),
    )
    web = raw.get("web", {}) or {}
    files = raw.get("files", {}) or {}
    data_dir = _abs_path("data")
    os.makedirs(data_dir, exist_ok=True)
    return Config(
        jira=jira,
        web_port=int(web.get("port", 8766)),
        max_txt_bytes=int(files.get("max_txt_bytes", 262144)),
        operations_db_file=os.path.join(data_dir, "operations.sqlite3"),
        mcp_logs_db_file=os.path.join(data_dir, "mcp_logs.sqlite3"),
        execution_lock_file=os.path.join(data_dir, "jira_execution.lock"),
    )


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(cfg: Config | None) -> None:
    global _config
    _config = cfg
