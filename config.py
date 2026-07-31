"""配置加载模块。

- 以本文件所在目录（项目根）为基准解析所有路径，避免 STDIO 子进程
  工作目录不确定导致的路径错误（spec §9.2）。
- 凭证仅存于此对象，__repr__ 脱敏，绝不整体序列化到日志或 MCP 返回值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

# 项目根 = 本文件所在目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")

SERVICE_VERSION = "1.0.0"


def _abs_path(path: str) -> str:
    """相对路径以项目根为基准解析为绝对路径。"""
    if os.path.isabs(path):
        return path
    return os.path.join(BASE_DIR, path)


@dataclass
class JiraConfig:
    url: str
    auth_type: str  # pat | basic
    token: str = ""
    username: str = ""
    password: str = ""
    verify_ssl: bool = True
    timeout: int = 30

    def __repr__(self) -> str:  # 脱敏，防止误打印
        return f"JiraConfig(url=***, auth_type={self.auth_type!r})"


@dataclass
class Config:
    jira: JiraConfig
    web_port: int = 8766
    expire_minutes: int = 30
    max_tasks_per_batch: int = 20
    max_txt_bytes: int = 262144
    log_dir: str = ""
    commit_log_keep: int = 200

    # 运行时派生的绝对路径
    data_dir: str = field(default="")
    queue_file: str = field(default="")
    commit_log_file: str = field(default="")
    audit_dir: str = field(default="")

    def sensitive_values(self) -> list[str]:
        """需要在日志中脱敏的敏感字符串集合。"""
        vals: list[str] = []
        for v in (self.jira.token, self.jira.password, self.jira.username):
            if v:
                vals.append(v)
        # JIRA 域名
        if self.jira.url:
            host = self.jira.url.split("://")[-1].split("/")[0]
            if host:
                vals.append(host)
        return vals

    def __repr__(self) -> str:
        return (
            f"Config(jira={self.jira!r}, web_port={self.web_port}, "
            f"expire_minutes={self.expire_minutes})"
        )


_config: Config | None = None


def _validate(raw: dict[str, Any]) -> None:
    if "jira" not in raw or not isinstance(raw["jira"], dict):
        raise ValueError("config.yaml 缺少 [jira] 配置段")
    jira = raw["jira"]
    if not jira.get("url"):
        raise ValueError("config.yaml 缺少 jira.url")
    auth_type = jira.get("auth_type", "pat")
    if auth_type not in ("pat", "basic"):
        raise ValueError(f"jira.auth_type 必须是 pat 或 basic，当前为 {auth_type!r}")
    if auth_type == "pat" and not jira.get("token"):
        raise ValueError("auth_type=pat 时必须提供 jira.token")
    if auth_type == "basic" and not (jira.get("username") and jira.get("password")):
        raise ValueError("auth_type=basic 时必须提供 jira.username 和 jira.password")
    queue = raw.get("queue", {}) or {}
    max_tasks = queue.get("max_tasks_per_batch", 20)
    if not isinstance(max_tasks, int) or isinstance(max_tasks, bool) or not 1 <= max_tasks <= 1000:
        raise ValueError("queue.max_tasks_per_batch 必须是 1 到 1000 的整数")


def load_config(path: str | None = None) -> Config:
    """从 config.yaml 加载配置并派生运行时路径。"""
    cfg_path = path or CONFIG_PATH
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(
            f"未找到配置文件 {cfg_path}，请复制 config.example.yaml 为 config.yaml 并填写。"
        )
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

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
    queue = raw.get("queue", {}) or {}
    files = raw.get("files", {}) or {}
    log = raw.get("log", {}) or {}

    log_dir = _abs_path(log.get("dir", "data"))
    data_dir = _abs_path("data")

    cfg = Config(
        jira=jira,
        web_port=int(web.get("port", 8766)),
        expire_minutes=int(queue.get("expire_minutes", 30)),
        max_tasks_per_batch=int(queue.get("max_tasks_per_batch", 20)),
        max_txt_bytes=int(files.get("max_txt_bytes", 262144)),
        log_dir=log_dir,
        commit_log_keep=int(log.get("commit_log_keep", 200)),
        data_dir=data_dir,
        queue_file=os.path.join(data_dir, "pending_queue.json"),
        commit_log_file=os.path.join(data_dir, "commit_log.json"),
        audit_dir=os.path.join(log_dir, "audit"),
    )

    # 确保运行时目录存在
    os.makedirs(cfg.data_dir, exist_ok=True)
    os.makedirs(cfg.audit_dir, exist_ok=True)
    return cfg


def get_config() -> Config:
    """全局单例配置。"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(cfg: Config) -> None:
    """测试用：注入配置单例。"""
    global _config
    _config = cfg
