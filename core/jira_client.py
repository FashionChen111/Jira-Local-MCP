"""JIRA 连接单例（Server / Data Center，REST v2）。

- 惰性建立连接；读工具与执行引擎共用同一实例。
- 凭证仅在此处从 config 读取传给 jira 库，绝不外泄到 MCP 返回值 / 日志明文。
- 认证：仅支持 PAT（Personal Access Token / Bearer）。
"""
from __future__ import annotations

import threading
from typing import Any

from config import get_config
from core.runtime_logger import get_logger

_client: Any = None
_lock = threading.Lock()


class JiraConnectionError(Exception):
    """JIRA 连接 / 认证失败。"""


def _build_client() -> Any:
    from jira import JIRA  # 延迟导入，避免无依赖时 import server 崩溃

    cfg = get_config().jira
    options = {
        "server": cfg.url,
        "verify": cfg.verify_ssl,
    }
    try:
        client = JIRA(options=options, token_auth=cfg.token, timeout=cfg.timeout)
    except Exception as e:
        # 不把凭证 / 域名放进异常文本
        raise JiraConnectionError(f"连接 JIRA 失败: {type(e).__name__}") from e
    return client


def get_client() -> Any:
    """返回全局 JIRA 客户端（惰性建立）。"""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            _client = _build_client()
            get_logger().info("JIRA 客户端已建立连接")
        return _client


def ping() -> bool:
    """探活：能否取到当前用户信息。"""
    try:
        get_client().myself()
        return True
    except Exception as e:
        get_logger().error(f"JIRA 探活失败: {type(e).__name__}")
        return False


def reset_client() -> None:
    """测试用：清空客户端单例。"""
    global _client
    _client = None


def set_client(client: Any) -> None:
    """测试用：注入 mock 客户端。"""
    global _client
    _client = client
