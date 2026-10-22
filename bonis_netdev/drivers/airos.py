"""Ubiquiti airOS (M and AC). The running config is the flat file /tmp/system.cfg.

The plan holds NO copy of that file (it carries the wireless PSK and password hashes).
It holds `airos-cfg-set base=<sha256 of the file> key=value ...`: an executor re-reads the
file, refuses if the hash changed, replaces exactly those keys in the whole file, writes the
whole file and commits it. A partial file is never written."""
from __future__ import annotations

import hashlib

from ..intents import Dns, Resolver
from ..parse import cfg_parse
from ..plan import DevicePlan, Step
from .base import Driver

CFG = "cat /tmp/system.cfg"
DNS_KEYS = ("resolv.nameserver.1.ip", "resolv.nameserver.2.ip")


class AirosDriver(Driver):
    name = "airos"
    implements = frozenset({"dns-standard"})

    def read_commands(self, intent) -> list[str]:
        return [CFG]

    def plan(self, device, intent, live, host) -> DevicePlan:
        text = live[CFG]
        base = hashlib.sha256(text.encode()).hexdigest()
        # the fingerprint is the hash only: nothing from the file is kept in the plan
        dp = DevicePlan(device, host, self.name, base)
        if not isinstance(intent, Dns):
            dp.unsupported.append(f"intent {type(intent).__name__} is not supported by airos")
            return dp
        if not isinstance(intent.scope, Resolver):
            dp.unsupported.append("airOS has no DHCP server networks to change; use scope Resolver")
            return dp
        if len(intent.servers) > len(DNS_KEYS):
            dp.unsupported.append(f"airOS resolver holds {len(DNS_KEYS)} servers, intent has {len(intent.servers)}")
            return dp
        cfg = cfg_parse(text)
        want = dict(zip(DNS_KEYS, intent.servers))
        changes = {k: v for k, v in want.items() if cfg.get(k) != v}
        stale = [k for k in DNS_KEYS[len(intent.servers):] if cfg.get(k)]
        if stale:
            dp.unsupported.append(f"{stale} would stay set; clearing a key is not planned yet")
        if not changes:
            dp.unchanged.append("resolver " + ",".join(want.values()))
            return dp
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
        return dp
