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


def cfg_parse(text: str) -> dict[str, str]:
    cfg = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            cfg[k] = v
    return cfg
