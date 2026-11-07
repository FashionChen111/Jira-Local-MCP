"""Single-line .env syntax shared by runtime and the dependency-free setup UI."""
from __future__ import annotations

import re
import json

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_COMMENT = re.compile(r"\s#")


def parse_env(text: str) -> dict[str, str]:
    """Read literal values without interpolation, shell expansion or multiline strings."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.lstrip('\ufeff').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, separator, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not separator or not _KEY.fullmatch(key):
            raise ValueError(f'.env 第 {number} 行格式错误')
        if value.startswith('"'):
            try:
                decoded, end = json.JSONDecoder().raw_decode(value)
            except ValueError:
                raise ValueError(f'.env 第 {number} 行引号格式错误') from None
            tail = value[end:].strip()
            if tail and not tail.startswith('#'):
                raise ValueError(f'.env 第 {number} 行引号格式错误')
            value = decoded
        elif value.startswith("'"):
            end = value.find("'", 1)
            if end < 0 or (value[end + 1:].strip() and not value[end + 1:].lstrip().startswith('#')):
                raise ValueError(f'.env 第 {number} 行引号格式错误')
            value = value[1:end]
        else:
            comment = _COMMENT.search(value)
            if comment:
                value = value[:comment.start()].rstrip()
        values[key] = value
    return values
