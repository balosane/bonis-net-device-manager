"""What a plan step does to a device - the one place that decides. Deny by default:
a command no rule matches is UNKNOWN and is refused, so a new kind of write can never
slip in just because nobody classified it (same idea as olt-magix oltdb.gpon_ops.classify)."""
from __future__ import annotations

import enum
import re


class Effect(str, enum.Enum):
    READ = "read"
    UPDATE = "update"
    UNKNOWN = "unknown"


_RULES = [
    # RouterOS reads
    (re.compile(r"^/[a-z0-9 /-]+ print( [\w-]+)*( where [\w=\"./, -]+)?$", re.I), Effect.READ),
    (re.compile(r"^/system (resource|identity) print$"), Effect.READ),
    # RouterOS writes: exactly these two shapes
    (re.compile(r"^/ip dhcp-server network set \[find address=[0-9./]+\] dns-server=[0-9a-f.:,]+$", re.I), Effect.UPDATE),
    (re.compile(r"^/ip dns set servers=[0-9a-f.:,]+$", re.I), Effect.UPDATE),
    # airOS
    (re.compile(r"^cat /tmp/system\.cfg$"), Effect.READ),
    (re.compile(r"^airos-cfg-set base=([0-9a-f]{64}|current)( [a-z0-9_.]+=[^\s]*)+$", re.I), Effect.UPDATE),
]


def effect_of(command: str) -> Effect:
    command = command.strip()
    if "\n" in command or ";" in command or "`" in command or "$(" in command:
        return Effect.UNKNOWN
    for rx, eff in _RULES:
        if rx.match(command):
            return eff
    return Effect.UNKNOWN
