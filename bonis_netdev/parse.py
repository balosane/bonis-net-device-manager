"""Parsers for what the drivers read. RouterOS `print detail` -> list of dicts."""
from __future__ import annotations

import re

KV = re.compile(r'(?:^|(?<=\s))([\w-]+)=("[^"]*"|.*?)(?=\s+[\w-]+=|\s*$)')
ENTRY = re.compile(r"^\s*\d+\s+(?:([A-Z]+)\s+)?")


def ros_records(text: str) -> list[dict]:
    """Flags (X disabled, I invalid, D dynamic ...) are kept under `_flags`."""
    out, cur = [], None
    for line in text.splitlines():
        m = ENTRY.match(line)
        if m:
            cur = {"_flags": m.group(1) or ""}
            out.append(cur)
        if cur is not None:
            for k, v in KV.findall(ENTRY.sub("", line, count=1) if m else line.strip()):
                cur[k] = v.strip().strip('"')
    return out


def ros_print_value(text: str, key: str) -> str | None:
    """`/ip dns print` style: `            servers: a,b`."""
    m = re.search(rf"^\s*{re.escape(key)}:\s*(.*)$", text, re.M)
    return m.group(1).strip() if m else None


_PRINT_KEY = re.compile(r"^\s*([\w-]+):(?:\s+(.*))?$")


def ros_print_map(text: str) -> dict[str, str]:
    """Singleton `print` / `print detail` -> {key: value}, tolerant of both layouts.

    `key: value` lines (a value may continue on following indented lines without a key, e.g. the RouterOS 7
    `servers:` list; continuation lines are joined with `,`) and `key=value` tokens (also quoted values). An IPv6
    continuation line such as `2001:db8::1` is not mistaken for a key because a key colon is followed by a space."""
    out: dict[str, str] = {}
    last = None
    for line in text.splitlines():
        if not line.strip():
            last = None
            continue
        m = _PRINT_KEY.match(line)
        if m:
            last = m.group(1)
            out[last] = (m.group(2) or "").strip()
            continue
        body = ENTRY.sub("", line, count=1).strip() if ENTRY.match(line) else line.strip()
        pairs = KV.findall(body)
        if pairs:
            for k, v in pairs:
                out[k] = v.strip().strip('"')
            last = None
        elif last is not None and line[0].isspace():
            out[last] = f"{out[last]},{body}" if out[last] else body
    return out


def cfg_parse(text: str) -> dict[str, str]:
    cfg = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            cfg[k] = v
    return cfg
