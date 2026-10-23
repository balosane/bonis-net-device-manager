"""MikroTik RouterOS 6 / 7. Targeted `set` on a found item, guarded by what was read."""
from __future__ import annotations

import ipaddress
import re

from ..intents import Dns, DhcpNetworks, MgmtApi, Ntp, Resolver, Syslog
from ..parse import ros_print_map, ros_print_value, ros_records
from ..plan import DevicePlan, Step
from .base import Driver

NETWORKS = "/ip dhcp-server network print detail without-paging"
SERVERS = "/ip dhcp-server print detail without-paging"
DNS = "/ip dns print"
NTP = "/system ntp client print"
LOG_ACTIONS = "/system logging action print detail without-paging"
LOG_ACTION_NAME = "netdev-remote"
LOG_ACTION_ONE = f"/system logging action print detail without-paging where name={LOG_ACTION_NAME}"
UNSET = "0.0.0.0"
SERVICE_API = "/ip service print detail without-paging where name=api"
_O = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_CIDR = rf"{_O}(?:\.{_O}){{3}}(?:/(?:3[0-2]|[12]?[0-9]))?"
_CIDRLIST = re.compile(rf"{_CIDR}(?:,{_CIDR})*")


MAX_RESTORE_ADDRESSES = 32      # what the routeros_service module accepts for a restore
_ADDR_LAST = re.compile(r'address=(?:"[^"]*"|\S*)\s*$')
_ENTRY_LINE = re.compile(r"^\s*\d+\s")


def _address_wrapped(text: str) -> bool:
    """True when the `api` row's address may continue on a following line (a wrapped list)."""
    lines = text.splitlines()
    for i, line in enumerate(lines[:-1]):
        if 'name="api"' not in line:
            continue
        m = _ADDR_LAST.search(line)
        nxt = lines[i + 1]
        if m and m.group(0).rstrip() != 'address=""' and nxt.strip() and not _ENTRY_LINE.match(nxt):
            return True
    return False


def _is_v4(text: str) -> bool:
    try:
        return str(ipaddress.IPv4Address(text)) == text
    except ValueError:
        return False


_HOSTLIST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*(,[A-Za-z0-9][A-Za-z0-9.-]*)*")


def _norm(servers: str | None) -> list[str]:
    return [s.strip() for s in (servers or "").split(",") if s.strip()]


class RouterOsDriver(Driver):
    implements = frozenset({"dns-standard", "ntp-standard", "syslog-standard", "mgmt-api"})

    def __init__(self, major: int):
        self.major = major
        self.name = f"routeros{major}"

    def read_commands(self, intent) -> list[str]:
        if isinstance(intent, Dns):
            return [DNS] if isinstance(intent.scope, Resolver) else [NETWORKS, SERVERS]
        if isinstance(intent, Ntp):
            return [NTP]
        if isinstance(intent, Syslog):
            return [LOG_ACTIONS]
        if isinstance(intent, MgmtApi):
            return [SERVICE_API]
        return []

    def plan(self, device, intent, live, host) -> DevicePlan:
        dp = DevicePlan(device, host, self.name, self.fingerprint(live))
        if isinstance(intent, Ntp):
            self._plan_ntp(dp, intent, live)
            return dp
        if isinstance(intent, Syslog):
            self._plan_syslog(dp, intent, live)
            return dp
        if isinstance(intent, MgmtApi):
            self._plan_mgmt_api(dp, intent, live)
            return dp
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

    # ---------------------------------------------------------------- NTP client
    def _plan_ntp(self, dp, intent: Ntp, live):
        """Two shapes, chosen by what the device prints (not by the major version alone): RouterOS 6 and 7 before 7.15
        have primary-ntp/secondary-ntp (IPv4 only, at most two); 7.15+ have `servers` (IP addresses or host names)."""
        p = ros_print_map(live[NTP])
        enabled = p.get("enabled", "").lower()
        if enabled not in ("yes", "no"):
            dp.unsupported.append(f"cannot read `enabled` from {NTP!r}; is the ntp package installed?")
            return
        if p.get("mode", "unicast") != "unicast":
            dp.notes.append(f"NTP mode is {p['mode']!r}; servers are only used in unicast mode and the mode is not changed")
        if "primary-ntp" in p:
            self._ntp_legacy(dp, intent, p, enabled)
        elif "servers" in p:
            self._ntp_servers(dp, intent, p, enabled)
        else:
            dp.unsupported.append("NTP client output has neither primary-ntp nor servers; unknown RouterOS layout")

    def _ntp_steps(self, dp, set_new, set_old, expect_old, expect_new, label_old, label_new):
        dp.steps += [
            Step("precheck", NTP, "re-read the NTP client; it must still hold the planned old values", expect=expect_old),
            Step("change", set_new, f"NTP client {label_old} -> {label_new}", rollback=set_old),
            Step("verify", NTP, "NTP client must hold the new values", expect=expect_new),
        ]

    def _ntp_legacy(self, dp, intent, p, enabled):
        if len(intent.servers) > 2:
            dp.unsupported.append(f"this RouterOS NTP client holds 2 servers (primary-ntp, secondary-ntp), intent has {len(intent.servers)}")
            return
        for s in intent.servers:
            try:
                ipaddress.IPv4Address(s)
            except ValueError:
                dp.unsupported.append(f"this RouterOS NTP client takes IPv4 addresses only (primary-ntp), not {s!r}; "
                                      "host names would need server-dns-names, which is not planned yet")
                return
        want_p = intent.servers[0]
        want_s = intent.servers[1] if len(intent.servers) > 1 else UNSET
        have_p = p.get("primary-ntp") or UNSET
        have_s = p.get("secondary-ntp") or UNSET
        if not (_is_v4(have_p) and _is_v4(have_s)):
            dp.unsupported.append(f"current NTP values {have_p!r}/{have_s!r} are not IPv4 addresses; refusing to plan a rollback")
            return
        if p.get("server-dns-names"):
            dp.notes.append(f"server-dns-names={p['server-dns-names']} is set and is not changed")
        if enabled == "yes" and (have_p, have_s) == (want_p, want_s):
            dp.unchanged.append(f"ntp primary-ntp={have_p} secondary-ntp={have_s}")
            return
        self._ntp_steps(
            dp, f"/system ntp client set enabled=yes primary-ntp={want_p} secondary-ntp={want_s}",
            f"/system ntp client set enabled={enabled} primary-ntp={have_p} secondary-ntp={have_s}",
            f"enabled={enabled} primary-ntp={have_p} secondary-ntp={have_s}", f"enabled=yes primary-ntp={want_p} secondary-ntp={want_s}",
            f"enabled={enabled} {have_p},{have_s}", f"enabled=yes {want_p},{want_s}")

    def _ntp_servers(self, dp, intent, p, enabled):
        want = ",".join(intent.servers)
        have = p.get("servers", "")
        if have and not _HOSTLIST.fullmatch(have):
            dp.unsupported.append(f"current NTP servers {have!r} contain characters outside [A-Za-z0-9.,-]; refusing to plan a rollback")
            return
        if enabled == "yes" and [x.strip().lower() for x in have.split(",") if x.strip()] == [x.lower() for x in intent.servers]:
            dp.unchanged.append(f"ntp servers={have}")
            return
        if not have:
            dp.notes.append("NTP servers were empty, so there is no automatic rollback value")
        self._ntp_steps(
            dp, f"/system ntp client set enabled=yes servers={want}",
            f"/system ntp client set enabled={enabled} servers={have}" if have else None,
            f"enabled={enabled} servers={have}", f"enabled=yes servers={want}",
            f"enabled={enabled} servers={have or '(none)'}", f"enabled=yes servers={want}")

    # ---------------------------------------------------------------- syslog
    def _plan_syslog(self, dp, intent: Syslog, live):
        """Only an EXISTING logging action named netdev-remote is updated. Creating actions or rules is a risk that is
        not taken while writes are banned, and existing rules (e.g. action `remote`, prefix = host name) are never touched."""
        act = next((r for r in ros_records(live[LOG_ACTIONS]) if r.get("name") == LOG_ACTION_NAME), None)
        if act is None:
            dp.unsupported.append(f"logging action {LOG_ACTION_NAME!r} does not exist; creation of logging actions/rules is not "
                                  "supported while writes are banned; needs the create-step design")
            return
        if act.get("target") != "remote":
            dp.unsupported.append(f"logging action {LOG_ACTION_NAME!r} has target={act.get('target')!r}, not remote; not touched")
            return
        have_h = act.get("remote") or UNSET
        have_p = act.get("remote-port") or "514"
        if not (_is_v4(have_h) and re.fullmatch(r"[1-9][0-9]{0,4}", have_p) and int(have_p) <= 65535):
            dp.unsupported.append(f"current action values remote={have_h!r} remote-port={have_p!r} are not plain; refusing to plan a rollback")
            return
        if (have_h, have_p) == (intent.host, str(intent.port)):
            dp.unchanged.append(f"logging action {LOG_ACTION_NAME} remote={have_h} remote-port={have_p}")
            return
        find = f"[find name={LOG_ACTION_NAME}]"
        dp.steps += [
            Step("precheck", LOG_ACTION_ONE, "re-read the action; it must still hold the planned old values",
                 expect=f"remote={have_h} remote-port={have_p}"),
            Step("change", f"/system logging action set {find} remote={intent.host} remote-port={intent.port}",
                 f"logging action {LOG_ACTION_NAME} remote {have_h}:{have_p} -> {intent.host}:{intent.port}",
                 rollback=f"/system logging action set {find} remote={have_h} remote-port={have_p}"),
            Step("verify", LOG_ACTION_ONE, "the action must hold the new values",
                 expect=f"remote={intent.host} remote-port={intent.port}"),
        ]

    # ---------------------------------------------------------------- mgmt-api
    def _plan_mgmt_api(self, dp, intent: MgmtApi, live):
        """Enable the plain API service and restrict it to the intent's addresses. Only the service `api` is touched
        (never ssh, winbox, www, api-ssl ...). The write is `/ip service set [find name=api] ...`; the rollback restores the
        previous `disabled` and `address`."""
        if _address_wrapped(live[SERVICE_API]):
            dp.unsupported.append("the address value of service `api` is wrapped over several lines in the device output; "
                                  "refusing to guess (no change planned)")
            return
        row = next((r for r in ros_records(live[SERVICE_API]) if r.get("name") == "api"), None)
        if row is None:
            dp.unsupported.append(f"cannot find the service `api` in the output of {SERVICE_API!r}")
            return
        flags = row["_flags"]
        if "I" in flags and "X" not in flags:     # RouterOS marks a DISABLED service X and I together; I alone is something else
            dp.unsupported.append(f"service `api` is flagged invalid without being disabled (flags {flags!r}); not touched")
            return
        disabled = "yes" if "X" in flags else "no"
        have = (row.get("address") or "").strip()
        if have and not _CIDRLIST.fullmatch(have):
            dp.unsupported.append(f"current api address list {have!r} is not a plain list of IPv4 addresses/prefixes; "
                                  "refusing to plan a rollback")
            return
        if len(have.split(",")) > MAX_RESTORE_ADDRESSES:
            dp.unsupported.append(f"current api address list has more than {MAX_RESTORE_ADDRESSES} entries; "
                                  "the rollback could not be restored by the module")
            return
        want = ",".join(intent.networks)
        try:
            same = ({ipaddress.IPv4Network(a, strict=False) for a in have.split(",") if a}
                    == {ipaddress.IPv4Network(a) for a in intent.networks})
        except ValueError:
            dp.unsupported.append(f"current api address list {have!r} cannot be parsed; refusing to plan a rollback")
            return
        if disabled == "no" and same:
            dp.unchanged.append(f"api service enabled, address={have}")
            return
        if disabled == "no" and not have:
            dp.notes.append("API was open to everyone; this narrows it")
        back_addr = have if have else '""'
        label_old = f"{'disabled' if disabled == 'yes' else 'enabled'}, address={have or '(any)'}"
        dp.steps += [
            Step("precheck", SERVICE_API, "re-read the api service; it must still hold the planned old values",
                 expect=f"disabled={disabled} address={have}"),
            Step("change", f"/ip service set [find name=api] disabled=no address={want}",
                 f"api service {label_old} -> enabled, address={want}",
                 rollback=f"/ip service set [find name=api] disabled={disabled} address={back_addr}"),
            Step("verify", SERVICE_API, "the api service must be enabled and restricted to the intent's addresses",
                 expect=f"disabled=no address={want}"),
        ]
