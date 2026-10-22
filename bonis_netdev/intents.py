"""Vendor-neutral intents: WHAT should be true, never HOW a device does it."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field


class IntentError(ValueError):
    pass


def _ip(value: str, what: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise IntentError(f"{what}: {value!r} is not an IP address") from None


@dataclass(frozen=True)
class DhcpNetworks:
    """Every DHCP network of the device, except the excluded prefixes."""
    exclude: tuple[str, ...] = ()

    def __post_init__(self):
        for p in self.exclude:
            try:
                ipaddress.ip_network(p)
            except ValueError:
                raise IntentError(f"exclude: {p!r} is not a prefix") from None

    def as_dict(self):
        return {"kind": "dhcp-networks", "exclude": sorted(self.exclude)}


@dataclass(frozen=True)
class Resolver:
    """The device's own resolver (what it uses for its own lookups)."""

    def as_dict(self):
        return {"kind": "resolver"}


@dataclass(frozen=True)
class Dns:
    primary: str
    secondary: str | None = None
    scope: DhcpNetworks | Resolver = field(default_factory=Resolver)
    extra: tuple[str, ...] = ()       # appended after secondary, e.g. an IPv6 resolver
    reason: str = ""                  # which policy / wave produced this intent

    def __post_init__(self):
        _ip(self.primary, "primary")
        if self.secondary is not None:
            _ip(self.secondary, "secondary")
        for e in self.extra:
            _ip(e, "extra")
        servers = self.servers
        if len(set(servers)) != len(servers):
            raise IntentError(f"duplicate DNS server in {servers}")

    @property
    def servers(self) -> tuple[str, ...]:
        return tuple(s for s in (self.primary, self.secondary, *self.extra) if s)

    def as_dict(self):
        return {"intent": "dns", "servers": list(self.servers), "scope": self.scope.as_dict(),
                "reason": self.reason}


_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$", re.I)


@dataclass(frozen=True)
class Ntp:
    servers: tuple[str, ...]          # IP addresses or host names
    reason: str = ""

    def __post_init__(self):
        if not self.servers:
            raise IntentError("servers: at least one NTP server is required")
        for s in self.servers:
            if not _HOST_RE.match(s):
                raise IntentError(f"servers: {s!r} is not an IP address or host name")
        if len({s.lower() for s in self.servers}) != len(self.servers):
            raise IntentError(f"duplicate NTP server in {self.servers}")

    def as_dict(self):
        return {"intent": "ntp", "servers": list(self.servers), "reason": self.reason}
