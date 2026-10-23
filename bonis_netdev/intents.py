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


_DOTTED = re.compile(r"[0-9]+(\.[0-9]+){3}")


def _check_unicast_v4(value: str, what: str) -> None:
    """Reject IPv4 addresses that can never be a real server: 0.0.0.0 means "unset" on RouterOS (it would switch the service
    off), and loopback, multicast, broadcast, reserved and link-local addresses are mistakes."""
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError:
        raise IntentError(f"{what}: {value!r} is not a valid IPv4 address") from None
    for name, bad in (("unspecified (0.0.0.0 means unset on RouterOS)", ip.is_unspecified), ("loopback", ip.is_loopback),
                      ("multicast", ip.is_multicast), ("broadcast/reserved", ip.is_reserved),
                      ("link-local", ip.is_link_local)):
        if bad:
            raise IntentError(f"{what}: {value!r} is {name}, not a usable server address")


@dataclass(frozen=True)
class Ntp:
    """NTP servers: IPv4 addresses or host names. IPv6 literals are rejected on purpose (a current limit, see DESIGN.md)."""
    servers: tuple[str, ...]
    reason: str = ""

    def __post_init__(self):
        if not self.servers:
            raise IntentError("servers: at least one NTP server is required")
        for s in self.servers:
            if isinstance(s, str) and ":" in s:
                raise IntentError(f"servers: {s!r}: IPv6 addresses are not supported (deliberate current limit); use an IPv4 address or a host name")
            if not isinstance(s, str) or not _HOST_RE.fullmatch(s):   # fullmatch: `$` would accept a trailing newline
                raise IntentError(f"servers: {s!r} is not an IPv4 address or host name")
            if _DOTTED.fullmatch(s):
                _check_unicast_v4(s, "servers")
        if len({s.lower() for s in self.servers}) != len(self.servers):
            raise IntentError(f"duplicate NTP server in {self.servers}")

    def as_dict(self):
        return {"intent": "ntp", "servers": list(self.servers), "reason": self.reason}


@dataclass(frozen=True)
class Syslog:
    """Send the device's log to one remote syslog server (IPv4)."""
    host: str
    port: int = 514
    reason: str = ""

    def __post_init__(self):
        try:
            ok = isinstance(self.host, str) and ipaddress.ip_address(self.host).version == 4
        except ValueError:
            ok = False
        if not ok or str(ipaddress.ip_address(self.host)) != self.host:
            raise IntentError(f"host: {self.host!r} is not a canonical IPv4 address")
        _check_unicast_v4(self.host, "host")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise IntentError(f"port: {self.port!r} is not a port number (1-65535)")

    def as_dict(self):
        return {"intent": "syslog", "host": self.host, "port": self.port, "reason": self.reason}
