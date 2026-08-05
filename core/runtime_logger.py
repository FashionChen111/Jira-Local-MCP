"""Redacted runtime logging; stdout remains exclusive to MCP JSON-RPC."""
from __future__ import annotations

import logging
import sys

from config import Config, get_config

_logger: logging.Logger | None = None


class _RedactFilter(logging.Filter):
    def __init__(self, sensitive: list[str]) -> None:
        super().__init__()
        self._sensitive = sorted(filter(None, sensitive), key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            for value in self._sensitive:
                message = message.replace(value, "***")
            record.msg = message
            record.args = ()
        except Exception:
            pass
        return True


def redact(text: str, cfg: Config | None = None) -> str:
    cfg = cfg or get_config()
    for value in sorted(cfg.sensitive_values(), key=len, reverse=True):
        text = text.replace(value, "***")
    return text


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    logger = logging.getLogger("jira_local_mcp")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    handler.addFilter(_RedactFilter(get_config().sensitive_values()))
    logger.addHandler(handler)
    _logger = logger
    return logger


def reset_logger() -> None:
    global _logger
    _logger = None
