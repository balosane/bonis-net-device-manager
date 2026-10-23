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


_ALLOWED_RE = re.compile(r"[0-9]+(?:\.[0-9]+){3}(?:/[0-9]+)?")
MAX_API_ALLOWED = 8
MIN_API_PREFIX = 24


@dataclass(frozen=True)
class MgmtApi:
    """Enable the plain RouterOS API service, restricted to these source addresses (IPv4 or IPv4/prefix, canonical form).

    Protects the controller's own access path: the address list is what the router accepts API connections from."""
    allowed: tuple[str, ...]
    reason: str = ""

    def __post_init__(self):
        if not isinstance(self.allowed, (tuple, list)):
            raise IntentError(f"allowed: expected a list of addresses, got {self.allowed!r}")
        if not self.allowed:
            raise IntentError("allowed: at least one address is required")
        if len(self.allowed) > MAX_API_ALLOWED:
            raise IntentError(f"allowed: at most {MAX_API_ALLOWED} entries, got {len(self.allowed)}")
        seen = set()
        for a in self.allowed:
            if not isinstance(a, str) or not _ALLOWED_RE.fullmatch(a):
                raise IntentError(f"allowed: {a!r} is not an IPv4 address or IPv4/prefix")
            try:
                net = ipaddress.IPv4Network(a, strict=True)
            except ValueError:
                raise IntentError(f"allowed: {a!r} is not a valid IPv4 address or network (host bits set?)") from None
            canonical = str(net.network_address) if "/" not in a else str(net)
            if canonical != a:
                raise IntentError(f"allowed: {a!r} is not in canonical form ({canonical!r})")
            if net.prefixlen < MIN_API_PREFIX:
                raise IntentError(f"allowed: {a!r} is wider than /{MIN_API_PREFIX}; the API must not be opened to a large network")
            ip = net.network_address
            for name, bad in (("unspecified", ip.is_unspecified), ("loopback", ip.is_loopback), ("multicast", ip.is_multicast),
                              ("broadcast/reserved", ip.is_reserved or net.broadcast_address == ipaddress.IPv4Address("255.255.255.255"))):
                if bad:
                    raise IntentError(f"allowed: {a!r} is {name}, not a usable controller address")
            if net in seen:
                raise IntentError(f"allowed: duplicate entry {a!r}")
            seen.add(net)
        # a list given by a caller is frozen into a tuple (the dataclass is frozen and hashable)
        object.__setattr__(self, "allowed", tuple(self.allowed))

    @property
    def networks(self) -> tuple[str, ...]:
        """The entries in CIDR form (a bare address is a /32), intent order kept: what is written to the device."""
        return tuple(a if "/" in a else f"{a}/32" for a in self.allowed)

    def as_dict(self):
        return {"intent": "mgmt-api", "allowed": list(self.allowed), "reason": self.reason}
