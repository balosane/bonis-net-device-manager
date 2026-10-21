"""MikroTik RouterOS 6 / 7. Targeted `set` on a found item, guarded by what was read."""
from __future__ import annotations

import ipaddress

from ..intents import Dns, DhcpNetworks, Resolver
from ..parse import ros_print_value, ros_records
from ..plan import DevicePlan, Step
from .base import Driver

NETWORKS = "/ip dhcp-server network print detail without-paging"
SERVERS = "/ip dhcp-server print detail without-paging"
DNS = "/ip dns print"


def _norm(servers: str | None) -> list[str]:
    return [s.strip() for s in (servers or "").split(",") if s.strip()]


class RouterOsDriver(Driver):
    def __init__(self, major: int):
        self.major = major
        self.name = f"routeros{major}"

    def read_commands(self, intent) -> list[str]:
        if isinstance(intent, Dns):
            return [DNS] if isinstance(intent.scope, Resolver) else [NETWORKS, SERVERS]
        return []

    def plan(self, device, intent, live, host) -> DevicePlan:
        dp = DevicePlan(device, host, self.name, self.fingerprint(live))
        if not isinstance(intent, Dns):
            dp.unsupported.append(f"intent {type(intent).__name__} is not supported by {self.name}")
            return dp
        want = ",".join(intent.servers)
        if isinstance(intent.scope, Resolver):
            self._plan_resolver(dp, intent, live, want)
        else:
            self._plan_networks(dp, intent, live, want)
        return dp

    def _plan_resolver(self, dp, intent, live, want):
        have = ros_print_value(live[DNS], "servers")
        if _norm(have) == list(intent.servers):
            dp.unchanged.append(f"resolver servers={have}")
            return
        if not have:
            dp.notes.append("resolver servers were empty, so there is no automatic rollback value")
        dp.steps += [
            Step("precheck", DNS, "re-read the resolver settings; servers must still be the planned old value",
                 expect=f"servers: {have or ''}".rstrip()),
            Step("change", f"/ip dns set servers={want}", f"resolver servers {have or '(none)'} -> {want}",
                 rollback=f"/ip dns set servers={have}" if have else None),
            Step("verify", DNS, "resolver servers must be the new value", expect=f"servers: {want}"),
        ]

    def _plan_networks(self, dp, intent, live, want):
        excluded = [ipaddress.ip_network(p) for p in intent.scope.exclude]
        nets = ros_records(live[NETWORKS])
        invalid = [r.get("name") for r in ros_records(live[SERVERS]) if "I" in r["_flags"] or "X" in r["_flags"]]
        if invalid:
            dp.skipped.append(f"DHCP servers disabled/invalid (their networks are still planned): {', '.join(map(str, invalid))}")
        seen = set()
        for r in nets:
            addr = r.get("address")
            if not addr or addr in seen:
                continue
            seen.add(addr)
            try:
                net = ipaddress.ip_network(addr, strict=False)
            except ValueError:
                dp.unsupported.append(f"network {addr!r} is not a prefix")
                continue
            if any(net.subnet_of(x) for x in excluded if net.version == x.version):
                dp.skipped.append(f"{addr} excluded by the intent")
                continue
            have = r.get("dns-server", "")
            if _norm(have) == list(intent.servers):
                dp.unchanged.append(f"{addr} dns-server={have}")
                continue
            find = f"[find address={addr}]"
            if not have:
                dp.notes.append(f"{addr}: dns-server was empty, so there is no automatic rollback value; clear it by hand to undo")
            dp.steps += [
                Step("precheck", f"/ip dhcp-server network print detail without-paging where address={addr}",
                     f"{addr}: dns-server must still be {have or '(none)'}",
                     expect=f"dns-server={have}" if have else None),
                Step("change", f"/ip dhcp-server network set {find} dns-server={want}",
                     f"{addr} dns-server {have or '(none)'} -> {want}",
                     rollback=f"/ip dhcp-server network set {find} dns-server={have}" if have else None),
                Step("verify", f"/ip dhcp-server network print detail without-paging where address={addr}",
                     f"{addr}: dns-server must be {want}", expect=f"dns-server={want}"),
            ]
