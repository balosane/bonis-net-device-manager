"""Ubiquiti airOS (M and AC). The running config is the flat file /tmp/system.cfg.

The plan holds NO copy of that file (it carries the wireless PSK and password hashes).
It holds `airos-cfg-set base=<sha256 of the file> key=value ...`: an executor re-reads the
file, refuses if the hash changed, replaces exactly those keys in the whole file, writes the
whole file and commits it. A partial file is never written."""
from __future__ import annotations

import hashlib

from ..intents import Dns, Ntp, Resolver, Syslog
from ..parse import cfg_parse
from ..plan import DevicePlan, Step
from .base import Driver

CFG = "cat /tmp/system.cfg"
DNS_KEYS = ("resolv.nameserver.1.ip", "resolv.nameserver.2.ip")
NTP_SERVER_KEYS = ("ntpclient.1.server", "ntpclient.2.server")


class AirosDriver(Driver):
    name = "airos"
    implements = frozenset({"dns-standard", "ntp-standard", "syslog-standard"})

    def read_commands(self, intent) -> list[str]:
        return [CFG]

    def plan(self, device, intent, live, host) -> DevicePlan:
        text = live[CFG]
        base = hashlib.sha256(text.encode()).hexdigest()
        # the fingerprint is the hash only: nothing from the file is kept in the plan
        dp = DevicePlan(device, host, self.name, base)
        cfg = cfg_parse(text)
        if isinstance(intent, Ntp):
            self._plan_ntp(dp, intent, cfg, base)
        elif isinstance(intent, Syslog):
            self._plan_keys(dp, cfg, base, {
                "syslog.remote.ip": intent.host, "syslog.remote.port": str(intent.port),
                "syslog.remote.status": "enabled", "syslog.status": "enabled"},
                f"syslog {intent.host}:{intent.port}")
        elif isinstance(intent, Dns):
            self._plan_dns(dp, intent, cfg, base)
        else:
            dp.unsupported.append(f"intent {type(intent).__name__} is not supported by airos")
        return dp

    def _plan_ntp(self, dp, intent, cfg, base):
        """Keys ntpclient.N.server (+ .status), ntpclient.status. The airos_cfg module appends keys that are missing
        from the file, so ntpclient.2.* is created only when the intent has a second server."""
        if len(intent.servers) > len(NTP_SERVER_KEYS):
            dp.unsupported.append(f"airOS NTP client holds {len(NTP_SERVER_KEYS)} servers, intent has {len(intent.servers)}")
            return
        want = {"ntpclient.status": "enabled"}
        for i, srv in enumerate(intent.servers, 1):
            want[f"ntpclient.{i}.server"] = srv
            want[f"ntpclient.{i}.status"] = "enabled"
        stale = [k for k in NTP_SERVER_KEYS[len(intent.servers):] if cfg.get(k)]
        if stale:
            dp.unsupported.append(f"{stale} would stay set; clearing a key is not planned yet")
        self._plan_keys(dp, cfg, base, want, "ntp " + ",".join(intent.servers))

    def _plan_dns(self, dp, intent, cfg, base):
        if not isinstance(intent.scope, Resolver):
            dp.unsupported.append("airOS has no DHCP server networks to change; use scope Resolver")
            return
        if len(intent.servers) > len(DNS_KEYS):
            dp.unsupported.append(f"airOS resolver holds {len(DNS_KEYS)} servers, intent has {len(intent.servers)}")
            return
        want = dict(zip(DNS_KEYS, intent.servers))
        stale = [k for k in DNS_KEYS[len(intent.servers):] if cfg.get(k)]
        if stale:
            dp.unsupported.append(f"{stale} would stay set; clearing a key is not planned yet")
        self._plan_keys(dp, cfg, base, want, "resolver " + ",".join(want.values()))

    @staticmethod
    def _plan_keys(dp, cfg, base, want, label):
        changes = {k: v for k, v in want.items() if cfg.get(k) != v}
        if not changes:
            dp.unchanged.append(label)
            return
        unset = [k for k in changes if not cfg.get(k)]
        if unset:
            dp.notes.append(f"{', '.join(sorted(unset))} was unset; rollback restores only the keys that had a value")
        args = " ".join(f"{k}={v}" for k, v in sorted(changes.items()))
        back = " ".join(f"{k}={cfg[k]}" for k in sorted(changes) if cfg.get(k))
        dp.steps += [
            Step("precheck", CFG, "re-read system.cfg; its sha256 must still be the planned base", expect=f"sha256:{base}"),
            Step("change", f"airos-cfg-set base={base} {args}",
                 "; ".join(f"{k}: {cfg.get(k) or '(unset)'} -> {v}" for k, v in sorted(changes.items())),
                 # `current`: the executor re-reads the file at rollback time instead of trusting a hash
                 rollback=f"airos-cfg-set base=current {back}" if back else None),
            Step("verify", CFG, "system.cfg must hold the new values", expect=args),
        ]
