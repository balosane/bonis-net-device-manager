"""netdev reconcile: NetBox tags (desired state) + git config -> device x feature matrix. Read-only, never runs a plan.

Reads NetBox (GET only) and the git config; the only thing it may write is the --emit directory."""
from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

import yaml

from .features import ConfigError
from .intents import IntentError
from .inventory import InventoryResult, devices_from_netbox
from .netbox import NetBoxClient, NetBoxError
from .templates import INVALID, Result, compile_device, dns_intents, format_matrix, load_config


def add_reconcile_parser(sub) -> None:
    p = sub.add_parser("reconcile", help="NetBox tags -> device x feature matrix (read-only; --emit writes plan inputs)")
    p.add_argument("--netbox", required=True, metavar="URL", help="NetBox base URL, e.g. http://127.0.0.1:28080")
    p.add_argument("--token-env", required=True, metavar="NAME", help="name of the environment variable holding the API token")
    p.add_argument("--token-kind", choices=("bearer", "token"), default="bearer", help="bearer (v2 tokens, default) or token (v1)")
    p.add_argument("--host-header", help="HTTP Host header to send (needed behind the SSH tunnel)")
    p.add_argument("--config", default="config", help="config directory (default ./config)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--emit", metavar="DIR", help="write dns intent + targets files for `netdev plan` into DIR (must not exist or be empty)")
    p.set_defaults(fn=cmd_reconcile)


def _emit_groups(results: list[Result], hosts: dict[str, str]) -> list[dict]:
    """One group per distinct DNS intent (servers + scope); devices sorted by name."""
    groups: dict[str, dict] = {}
    for device, dns in dns_intents(results):
        doc = {"intent": "dns", "primary": dns.primary}
        if dns.secondary:
            doc["secondary"] = dns.secondary
        if dns.extra:
            doc["extra"] = list(dns.extra)
        doc["scope"] = dns.scope.as_dict()
        doc["reason"] = "netdev reconcile: desired state from NetBox tags"
        g = groups.setdefault(json.dumps(doc, sort_keys=True), {"intent": doc, "targets": []})
        g["targets"].append({"name": device, "host": hosts[device]})
    out = sorted(groups.values(), key=lambda g: g["targets"][0]["name"])
    for g in out:
        g["targets"].sort(key=lambda t: t["name"])
    return out


def _check_emit_dir(path: Path) -> None:
    if path.is_symlink():
        raise NetBoxError(f"--emit {path}: is a symlink; refusing to write through it")
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise NetBoxError(f"--emit {path}: exists and is not an empty directory; refusing to write into it")


def _dump(path: Path, doc) -> None:
    data = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)   # not secret; never overwrite
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def write_emit(path: Path, groups: list[dict]) -> list[str]:
    _check_emit_dir(path)
    path.mkdir(parents=True, exist_ok=True)
    _check_emit_dir(path)       # re-check right before writing
    written = []
    index = []
    single = len(groups) == 1
    for n, g in enumerate(groups, 1):
        intent_name = "dns-intent.yml" if single else f"dns-{n}.yml"
        targets_name = "targets.yml" if single else f"targets-{n}.yml"
        _dump(path / intent_name, g["intent"])
        _dump(path / targets_name, {"targets": g["targets"]})
        written += [intent_name, targets_name]
        index.append({"intent": intent_name, "targets": targets_name, "devices": [t["name"] for t in g["targets"]]})
    _dump(path / "index.yml", {"groups": index})
    return written + ["index.yml"]


def reconcile(client, config_dir):
    """-> (results, InventoryResult). Per-device compile errors become problems instead of aborting the run."""
    cfg = load_config(config_dir, with_devices=False)
    inv = devices_from_netbox(client, cfg)
    cfg = dataclasses.replace(cfg, templates={**cfg.templates, **inv.synthetic_templates},
                              devices={d.name: d for d in inv.devices})
    results: list[Result] = []
    ok_devices = []
    for dev in inv.devices:
        missing = [f"{layer} {value!r} is not a key of vars/{layer}.yml"
                   for layer, value in (("region", dev.region), ("site", dev.site), ("role", dev.role))
                   if value is not None and (Path(config_dir) / "vars" / f"{layer}.yml").exists() and value not in cfg.vars[layer]]
        if missing:
            inv.problems.append((dev.name, "; ".join(missing) + " (add it, possibly as an empty entry `name: {}`)"))
            continue
        try:
            results += compile_device(cfg, dev)
            ok_devices.append(dev)
        except ConfigError as e:
            inv.problems.append((dev.name, str(e)))
    inv.problems.sort()
    inv.devices = ok_devices
    return results, inv


def cmd_reconcile(a) -> int:
    try:
        if a.emit:
            _check_emit_dir(Path(a.emit))
        client = NetBoxClient.from_env(a.netbox, a.token_env, token_kind=a.token_kind, host_header=a.host_header)
        results, inv = reconcile(client, a.config)
        groups = _emit_groups(results, {d.name: d.host for d in inv.devices}) if a.emit else []
    except (ConfigError, NetBoxError, IntentError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    bad = any(r.status == INVALID for r in results) or bool(inv.problems)
    if a.json:
        print(json.dumps({"results": [r.as_dict() for r in results],
                          "problems": [{"device": n, "reason": r} for n, r in inv.problems],
                          "unmanaged_count": len(inv.unmanaged)}, indent=2, ensure_ascii=False))
    else:
        print(format_matrix(results))
        for n, r in inv.problems:
            print(f"PROBLEM  {n}: {r}")
        for n, r in inv.unmanaged:
            if r.startswith("status "):
                print(f"UNMANAGED  {n}: carries a template/feature tag but {r}")
        print(f"{len(inv.devices)} managed, {len(inv.problems)} with problems, {len(inv.unmanaged)} unmanaged (no tag or not active)")
    if a.emit and bad:
        print("error: --emit refused: there are problems or invalid results; nothing was written", file=sys.stderr)
        return 1
    if a.emit:
        try:
            files = write_emit(Path(a.emit), groups)
        except (NetBoxError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"emitted to {a.emit}: {', '.join(files)}",
              file=sys.stderr if a.json else sys.stdout)
    return 1 if bad else 0
