"""Validated local configuration and derived runtime paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from env_file import parse_env

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, ".env")
SERVICE_VERSION = "3.0.0"


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


def _positive_integer(raw: dict[str, str], key: str, default: int, maximum: int | None = None) -> int:
    value = raw.get(key, str(default))
    if not value.isascii() or not value.isdecimal():
        raise ValueError(f"{key} 必须是正整数")
    result = int(value)
    if result < 1 or (maximum is not None and result > maximum):
        raise ValueError(f"{key} 超出允许范围")
    return result


def load_config(path: str | None = None) -> Config:
    cfg_path = path or CONFIG_PATH
    if not os.path.exists(cfg_path):
        raise FileNotFoundError("未找到项目 .env，请运行 setup.bat / setup.command 或 scripts/install.py。")
    with open(cfg_path, "r", encoding="utf-8-sig") as handle:
        raw = parse_env(handle.read())
    return config_from_values(raw)


def config_from_values(raw: dict[str, str]) -> Config:
    """Validate configuration before the installer writes it to disk."""
    url = raw.get("ATLASSIAN_JIRA_URL", "").strip().rstrip("/")
    try:
        parts = urlsplit(url)
        valid_url = (
            parts.scheme in ("http", "https") and bool(parts.hostname)
            and parts.username is None and parts.password is None
            and not parts.query and not parts.fragment
            and not any(char.isspace() for char in url)
        )
        parts.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("ATLASSIAN_JIRA_URL 必须是有效的 http(s) 地址，不能含凭据、查询或片段")
    auth_type = raw.get("ATLASSIAN_JIRA_AUTH_TYPE", "pat")
    if auth_type not in ("pat", "basic"):
        raise ValueError("ATLASSIAN_JIRA_AUTH_TYPE 必须是 pat 或 basic")
    required = ("ATLASSIAN_JIRA_PAT",) if auth_type == "pat" else (
        "ATLASSIAN_JIRA_USERNAME", "ATLASSIAN_JIRA_PASSWORD",
    )
    for key in required:
        if not raw.get(key, "").strip():
            raise ValueError(f"缺少 {key}")
    verify = raw.get("ATLASSIAN_JIRA_VERIFY_SSL", "true").lower()
    if verify not in ("true", "false"):
        raise ValueError("ATLASSIAN_JIRA_VERIFY_SSL 必须是 true 或 false")
    jira = JiraConfig(
        url=url, auth_type=auth_type,
        token=raw.get("ATLASSIAN_JIRA_PAT", ""),
        username=raw.get("ATLASSIAN_JIRA_USERNAME", ""),
        password=raw.get("ATLASSIAN_JIRA_PASSWORD", ""),
        verify_ssl=verify == "true",
        timeout=_positive_integer(raw, "ATLASSIAN_JIRA_TIMEOUT_SECONDS", 30),
    )
    web_port = _positive_integer(raw, "JIRA_MCP_WEB_PORT", 8766, 65535)
    max_txt_bytes = _positive_integer(raw, "JIRA_MCP_MAX_TXT_BYTES", 262144)
    data_dir = _abs_path("data")
    return Config(
        jira=jira, web_port=web_port, max_txt_bytes=max_txt_bytes,
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
