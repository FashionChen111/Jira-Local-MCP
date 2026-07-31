"""本地文本文件安全读取。

用于 description_file / {"description": {"file": ...}} 场景：
  - 仅接受绝对路径
  - 仅接受 .txt 纯文本
  - 大小受配置上限约束
  - 尝试以 UTF-8 解码，拒绝二进制内容
"""
from __future__ import annotations

import os

from config import get_config


class TextFileError(Exception):
    """文本文件读取相关的业务异常。"""


def read_text_file(path: str) -> str:
    """安全读取本地 txt 文件内容。

    Raises:
        TextFileError: 路径非法 / 非 txt / 超大 / 非文本。
    """
    cfg = get_config()

    if not path or not isinstance(path, str):
        raise TextFileError("文件路径为空")
    if not os.path.isabs(path):
        raise TextFileError(f"文件路径必须为绝对路径: {path}")
    if not path.lower().endswith(".txt"):
        raise TextFileError(f"仅允许读取 .txt 文件: {path}")
    if not os.path.isfile(path):
        raise TextFileError(f"文件不存在: {path}")

    size = os.path.getsize(path)
    if size > cfg.max_txt_bytes:
        raise TextFileError(
            f"文件过大 ({size} 字节)，上限 {cfg.max_txt_bytes} 字节: {path}"
        )

    try:
        with open(path, "rb") as f:
            raw = f.read()
        # 二进制探测：出现 NUL 字节视为非文本
        if b"\x00" in raw:
            raise TextFileError(f"文件疑似二进制，拒绝读取: {path}")
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise TextFileError(f"文件不是有效的 UTF-8 文本: {path}")
