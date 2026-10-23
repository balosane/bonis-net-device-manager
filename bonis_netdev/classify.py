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


_O = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_V4 = rf"{_O}(?:\.{_O}){{3}}"
_PORT = r"(?:[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])"
_CIDR = rf"{_V4}(?:/(?:3[0-2]|[12]?[0-9]))?"
_CIDRS = rf"{_CIDR}(?:,{_CIDR})*"
_HOSTS = r"[A-Za-z0-9][A-Za-z0-9.-]*(?:,[A-Za-z0-9][A-Za-z0-9.-]*)*"

_RULES = [
    # RouterOS reads
    (re.compile(r"^/[a-z0-9 /-]+ print( [\w-]+)*( where [\w=\"./, -]+)?$", re.I), Effect.READ),
    (re.compile(r"^/system (resource|identity) print$"), Effect.READ),
    # RouterOS writes: exactly these shapes
    (re.compile(r"^/ip dhcp-server network set \[find address=[0-9./]+\] dns-server=[0-9a-f.:,]+$", re.I), Effect.UPDATE),
    (re.compile(r"^/ip dns set servers=[0-9a-f.:,]+$", re.I), Effect.UPDATE),
    # RouterOS NTP client: a legacy (primary/secondary, RouterOS 6 and 7 before 7.15) and a list (servers=, 7.15+) shape.
    # Hosts are limited to [A-Za-z0-9.-] (IPv4 dotted quad for the legacy shape): nothing can inject into a command.
    (re.compile(rf"^/system ntp client set enabled=(yes|no) primary-ntp={_V4} secondary-ntp={_V4}$"), Effect.UPDATE),
    (re.compile(rf"^/system ntp client set enabled=(yes|no) servers={_HOSTS}$"), Effect.UPDATE),
    # RouterOS syslog: only an EXISTING action named netdev-remote is updated (never created, rules are never touched)
    (re.compile(rf"^/system logging action set \[find name=netdev-remote\] remote={_V4} remote-port={_PORT}$"), Effect.UPDATE),
    # RouterOS API service (mgmt-api): only the service named api; disabled yes|no and an address list of IPv4/CIDR.
    # The empty address form (`address=""`, "no restriction") is a separate shape that only a rollback uses.
    (re.compile(rf"^/ip service set \[find name=api\] disabled=(yes|no) address={_CIDRS}\Z"), Effect.UPDATE),
    (re.compile(r'^/ip service set \[find name=api\] disabled=(yes|no) address=""\Z'), Effect.UPDATE),
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
