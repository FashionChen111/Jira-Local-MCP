"""审计日志与运行日志模块。

两条独立日志流：
  1. 运行日志 —— 输出到 stderr（**绝不 stdout**，stdout 独占 MCP JSON-RPC）。
  2. 审计日志 —— 结构化 JSONL，按天切割，记录全链路事件。

所有日志经脱敏过滤器处理，JIRA 域名 / 用户名 / token / 密码替换为 ***。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from typing import Any

import portalocker

from config import Config, get_config

_AUDIT_LOCK = threading.Lock()
_logger: logging.Logger | None = None


class _RedactFilter(logging.Filter):
    """将配置中的敏感字符串从日志消息里替换掉。"""

    def __init__(self, sensitive: list[str]) -> None:
        super().__init__()
        # 长的先替换，避免子串问题
        self._sensitive = sorted([s for s in sensitive if s], key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            for s in self._sensitive:
                if s and s in msg:
                    msg = msg.replace(s, "***")
            record.msg = msg
            record.args = ()
        except Exception:
            pass
        return True


def redact(text: str, cfg: Config | None = None) -> str:
    """对任意字符串做脱敏，供 executor 组装错误信息时复用。"""
    cfg = cfg or get_config()
    for s in sorted(cfg.sensitive_values(), key=len, reverse=True):
        if s and s in text:
            text = text.replace(s, "***")
    return text


def get_logger() -> logging.Logger:
    """返回全局运行日志器（输出到 stderr）。"""
    global _logger
    if _logger is not None:
        return _logger

    cfg = get_config()
    logger = logging.getLogger("jira_safe_mcp")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    handler.addFilter(_RedactFilter(cfg.sensitive_values()))
    logger.addHandler(handler)

    _logger = logger
    return logger


def _audit_file(cfg: Config) -> str:
    day = time.strftime("%Y%m%d", time.localtime())
    return os.path.join(cfg.audit_dir, f"audit-{day}.jsonl")


def audit(event: str, **fields: Any) -> None:
    """写一条结构化审计记录。

    Args:
        event: 事件类型，如 enqueue / confirm / execute / result / error / expire / cancel
        **fields: 附加字段（会被脱敏）
    """
    cfg = get_config()
    record: dict[str, Any] = {
        "ts": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "event": event,
    }
    record.update(fields)

    # 脱敏：对整条记录的 JSON 文本做替换
    line = json.dumps(record, ensure_ascii=False, default=str)
    line = redact(line, cfg)

    try:
        with _AUDIT_LOCK:
            os.makedirs(cfg.audit_dir, exist_ok=True)
            path = _audit_file(cfg)
            with portalocker.Lock(path + ".lock", mode="a", timeout=10):
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
    except Exception as e:  # 审计失败不能影响主流程
        get_logger().error(f"写审计日志失败: {e}")
