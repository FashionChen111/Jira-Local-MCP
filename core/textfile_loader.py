"""Strict inline/file text sources and bounded local UTF-8 file reads."""
from __future__ import annotations

import os

from config import get_config


class TextFileError(Exception):
    """文本文件读取相关的业务异常。"""


def read_text_file(path: object, *, field_path: str = "file") -> str:
    """Read one absolute, bounded UTF-8 ``.txt`` path for a named field."""

    cfg = get_config()

    if not isinstance(path, str):
        raise TextFileError(f"{field_path} 必须是文件路径字符串")
    if not path:
        raise TextFileError(f"{field_path} 文件路径为空")
    if not os.path.isabs(path):
        raise TextFileError(f"{field_path} 必须为绝对路径: {path}")
    if not path.lower().endswith(".txt"):
        raise TextFileError(f"{field_path} 仅允许读取 .txt 文件: {path}")
    if not os.path.isfile(path):
        raise TextFileError(f"{field_path} 文件不存在: {path}")

    size = os.path.getsize(path)
    if size > cfg.max_txt_bytes:
        raise TextFileError(
            f"{field_path} 文件过大 ({size} 字节)，"
            f"上限 {cfg.max_txt_bytes} 字节: {path}"
        )

    try:
        with open(path, "rb") as f:
            raw = f.read()
        if b"\x00" in raw:
            raise TextFileError(
                f"{field_path} 文件疑似二进制，拒绝读取: {path}"
            )
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TextFileError(
            f"{field_path} 文件不是有效的 UTF-8 文本: {path}"
        ) from exc


def snapshot_text_source(value: object, *, field_path: str) -> str | None:
    """Resolve an inline string, null, or exact ``{"file": path}`` source."""

    if value is None or isinstance(value, str):
        return value
    if not isinstance(value, dict) or set(value) != {"file"}:
        raise TextFileError(
            f"{field_path} 必须是字符串、null 或仅包含 file 的对象"
        )
    return read_text_file(value["file"], field_path=f"{field_path}.file")
