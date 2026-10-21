"""netdev plan | show | check

  netdev selftest | demo                     no devices, no credentials
  netdev probe HOST                          what the tool sees on one device (read-only)
  netdev try HOST --dns 1.1.1.1,8.8.8.8      plan one device without files
  netdev plan --intent dns.yml --targets targets.yml --credentials secrets.yml --out plan.json
  netdev show plan.json
  netdev check plan.json          # recompute plan_id, refuse a plan edited by hand

The prototype has no apply command: it reads devices and writes a plan.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from concurrent.futures import ThreadPoolExecutor

import yaml

from .drivers.base import build_device_plan, detect_driver
from .intents import DhcpNetworks, Dns, IntentError, Resolver
from .plan import DevicePlan, Plan
from .transport import SshReadTransport


def load_intent(path: str) -> Dns:
    with open(path, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    if d.get("intent") != "dns":
        raise IntentError(f"{path}: only `intent: dns` exists in the prototype")
    sc = d.get("scope") or {"kind": "resolver"}
    if sc.get("kind") == "dhcp-networks":
        scope = DhcpNetworks(exclude=tuple(sc.get("exclude") or ()))
    elif sc.get("kind") == "resolver":
        scope = Resolver()
    else:
        raise IntentError(f"{path}: unknown scope kind {sc.get('kind')!r}")
    return Dns(primary=d["primary"], secondary=d.get("secondary"), extra=tuple(d.get("extra") or ()),
               scope=scope, reason=d.get("reason", ""))


def load_credentials(path: str) -> list[tuple[str, str]]:
    with open(path, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    return [(c["username"], c["password"]) for c in d.get("credential_sets", [])]


def plan_target(target: dict, intent, creds) -> DevicePlan:
    name, host = target["name"], target["host"]
    try:
        t = SshReadTransport(host, creds)
        return build_device_plan(detect_driver(t), name, intent, t)
    except Exception as e:  # one unreachable device must not hide the rest
        return DevicePlan(name, host, "?", "", error=f"{type(e).__name__}: {e}")


def cmd_plan(a) -> int:
    a.credentials = find_credentials(a.credentials)
    intent = load_intent(a.intent)
    with open(a.targets, encoding="utf-8") as f:
        targets = (yaml.safe_load(f) or {}).get("targets", [])
    creds = load_credentials(a.credentials)
    with ThreadPoolExecutor(a.workers) as ex:
        devices = list(ex.map(lambda t: plan_target(t, intent, creds), targets))
    plan = Plan(intent.as_dict(), devices)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(plan.to_json(created_at=now))
    print(plan.summary())
    print(f"\nwritten {a.out}  plan_id={plan.plan_id}")
    return 1 if any(d.error for d in devices) else 0


def _load_plan(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cmd_show(a) -> int:
    doc = _load_plan(a.plan)
    print(f"plan_id {doc['plan_id']}  created {doc['created_at']}  writes_allowed={doc['writes_allowed']}")
    print(json.dumps(doc["intent"], ensure_ascii=False))
    for d in doc["devices"]:
        print(f"\n== {d['device']} ({d['host']}, {d['driver']})" + (f"  ERROR {d['error']}" if d["error"] else ""))
        for s in d["steps"]:
            if s["phase"] == "change":
                print(f"  {s['command']}\n      {s['purpose']}" + (f"\n      rollback: {s['rollback']}" if s["rollback"] else ""))
        for k in ("unchanged", "skipped", "unsupported", "notes"):
            for line in d[k]:
                print(f"  [{k}] {line}")
    return 0


def cmd_check(a) -> int:
    import hashlib
    doc = _load_plan(a.plan)
    body = {"intent": doc["intent"], "devices": doc["devices"]}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    ok = hashlib.sha256(blob.encode()).hexdigest() == doc["plan_id"]
    print("OK plan_id matches content" if ok else "MISMATCH: plan was modified after it was created")
    return 0 if ok else 2


def find_credentials(explicit: str | None) -> str:
    """--credentials, else $NETDEV_CREDENTIALS, else ./credentials.yml, else ~/.config/netdev/credentials.yml."""
    import os
    for c in (explicit, os.environ.get("NETDEV_CREDENTIALS"), "credentials.yml",
              os.path.expanduser("~/.config/netdev/credentials.yml")):
        if c and os.path.exists(c):
            return c
    raise FileNotFoundError("no credentials file: use --credentials, $NETDEV_CREDENTIALS, ./credentials.yml "
                            "or ~/.config/netdev/credentials.yml (format: examples/credentials.example.yml)")


def _intent_from_args(a) -> Dns:
    servers = [s.strip() for s in a.dns.split(",") if s.strip()]
    if not 1 <= len(servers) <= 6:
        raise IntentError("--dns needs 1 or more comma-separated IPs")
    scope = Resolver() if a.resolver else DhcpNetworks(exclude=tuple(a.exclude))
    return Dns(servers[0], servers[1] if len(servers) > 1 else None, scope, extra=tuple(servers[2:]),
               reason="netdev try")


def _print_device(d: DevicePlan) -> None:
    print(f"== {d.device} ({d.host}, {d.driver})" + (f"  ERROR {d.error}" if d.error else ""))
    for s in d.steps:
        if s.phase == "change":
            print(f"  {s.command}\n      {s.purpose}" + (f"\n      rollback: {s.rollback}" if s.rollback else ""))
    for k in ("unchanged", "skipped", "unsupported", "notes"):
        for line in getattr(d, k):
            print(f"  [{k}] {line}")
    print(f"  => {d.changes} change(s)")


def cmd_try(a) -> int:
    """One device, no files: read it, print the plan. Nothing is written to the device."""
    intent = _intent_from_args(a)
    creds = load_credentials(find_credentials(a.credentials))
    d = plan_target({"name": a.name or a.host, "host": a.host}, intent, creds)
    _print_device(d)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(Plan(intent.as_dict(), [d]).to_json(datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")))
        print(f"plan written to {a.out}")
    return 1 if d.error else 0


def cmd_probe(a) -> int:
    """Show what the tool sees on a device (device class and the values it would work with)."""
    from .drivers.airos import AirosDriver
    from .drivers.routeros import DNS, NETWORKS, RouterOsDriver
    from .parse import cfg_parse, ros_print_value, ros_records
    t = SshReadTransport(a.host, load_credentials(find_credentials(a.credentials)))
    drv = detect_driver(t)
    print(f"{a.host}: driver {drv.name}")
    if isinstance(drv, RouterOsDriver):
        print(f"resolver servers: {ros_print_value(t.read(DNS), 'servers') or '(none)'}")
        for r in ros_records(t.read(NETWORKS)):
            print(f"  network {r.get('address'):<20} dns-server={r.get('dns-server') or '(none)'}")
    elif isinstance(drv, AirosDriver):
        cfg = cfg_parse(t.read("cat /tmp/system.cfg"))
        for k in ("resolv.nameserver.1.ip", "resolv.nameserver.2.ip"):
            print(f"  {k} = {cfg.get(k) or '(unset)'}")
    return 0


def cmd_demo(a) -> int:
    """Plan against built-in fake devices: shows the output without any network or credentials."""
    from . import samples
    from .drivers.airos import AirosDriver
    from .drivers.base import build_device_plan
    from .drivers.routeros import RouterOsDriver
    intent = Dns("192.168.238.119", "192.168.6.1", DhcpNetworks(exclude=("192.168.88.0/24",)), reason="demo")
    devs = [build_device_plan(RouterOsDriver(6), "DEMO-ROUTER", intent, samples.ros_transport()),
            build_device_plan(AirosDriver(), "DEMO-AP", Dns("192.168.238.119", "192.168.6.1"), samples.airos_transport())]
    for d in devs:
        _print_device(d)
        print()
    plan = Plan(intent.as_dict(), devs)
    print(f"plan_id {plan.plan_id}")
    return 0


def cmd_selftest(a) -> int:
    """Check the safety properties on fake devices; prints PASS/FAIL per check. Exit 1 on any failure."""
    import hashlib
    import json

    from . import samples
    from .classify import Effect, effect_of
    from .drivers.airos import AirosDriver
    from .drivers.base import build_device_plan
    from .drivers.routeros import RouterOsDriver
    from .transport import WriteRefused
    results = []

    def check(name, fn):
        try:
            ok = bool(fn())
        except Exception as e:
            ok, name = False, f"{name} ({type(e).__name__}: {e})"
        results.append(ok)
        print(("PASS  " if ok else "FAIL  ") + name)

    def refused(cmd):
        try:
            samples.ros_transport().read(cmd)
        except WriteRefused:
            return True
        return False

    intent = Dns("192.168.238.119", "192.168.6.1", DhcpNetworks(exclude=("192.168.88.0/24",)))
    ros = build_device_plan(RouterOsDriver(6), "r", intent, samples.ros_transport())
    air = build_device_plan(AirosDriver(), "a", Dns("192.168.238.119", "192.168.6.1"), samples.airos_transport())
    plan = Plan(intent.as_dict(), [ros, air])
    doc = json.loads(plan.to_json())
    body = json.dumps({"intent": doc["intent"], "devices": doc["devices"]}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    check("read-only transport refuses a DNS change", lambda: refused("/ip dns set servers=1.1.1.1"))
    check("read-only transport refuses a reboot", lambda: refused("/system reboot"))
    check("command with ';' is not classified", lambda: effect_of("/ip dns print; /system reboot") == Effect.UNKNOWN)
    check("unknown command is refused (deny by default)", lambda: effect_of("/user remove [find]") == Effect.UNKNOWN)
    check("excluded prefix 192.168.88.0/24 is never planned", lambda: not any("192.168.88.0/24" in s.command for s in ros.steps))
    check("every change has a precheck before and a verify after",
          lambda: [s.phase for s in ros.steps] == ["precheck", "change", "verify"] * ros.changes)
    check("every change with an old value has a rollback",
          lambda: all(s.rollback for s in ros.steps if s.phase == "change" and "(none)" not in s.purpose))
    check("airOS plan contains no PSK or password hash",
          lambda: "SuperSecretPsk" not in plan.to_json() and "$1$hash" not in plan.to_json())
    check("same input gives the same plan_id", lambda: plan.plan_id == Plan(intent.as_dict(), [ros, air]).plan_id)
    check("plan_id matches the sha256 of the content", lambda: hashlib.sha256(body.encode()).hexdigest() == doc["plan_id"])
    check("a plan edited by hand no longer matches its plan_id",
          lambda: hashlib.sha256(body.replace("192.168.238.119", "9.9.9.9", 1).encode()).hexdigest() != doc["plan_id"])
    check("the plan says writes are not allowed", lambda: doc["writes_allowed"] is False)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="netdev", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="read devices, write a plan (never writes to devices)")
    p.add_argument("--intent", required=True)
    p.add_argument("--targets", required=True)
    p.add_argument("--credentials")
    p.add_argument("--out", default="plan.json")
    p.add_argument("--workers", type=int, default=6)
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("try", help="plan one device without files: netdev try HOST --dns 1.1.1.1,8.8.8.8")
    p.add_argument("host")
    p.add_argument("--dns", required=True, help="comma-separated servers: primary,secondary[,extra...]")
    p.add_argument("--resolver", action="store_true", help="plan the device's own resolver instead of its DHCP networks")
    p.add_argument("--exclude", action="append", default=["192.168.88.0/24"], metavar="PREFIX",
                   help="prefix to leave alone (repeatable; 192.168.88.0/24 is always excluded)")
    p.add_argument("--name")
    p.add_argument("--credentials")
    p.add_argument("--out", help="also store the plan as JSON")
    p.set_defaults(fn=cmd_try)
    p = sub.add_parser("probe", help="show the device class and the DNS values the tool sees (read-only)")
    p.add_argument("host")
    p.add_argument("--credentials")
    p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("demo", help="plan against built-in fake devices (no network, no credentials)")
    p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("selftest", help="check the safety properties on fake devices")
    p.set_defaults(fn=cmd_selftest)
    p = sub.add_parser("show", help="human-readable view of a stored plan")
    p.add_argument("plan")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("check", help="verify the plan_id against the content")
    p.add_argument("plan")
    p.set_defaults(fn=cmd_check)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (IntentError, FileNotFoundError, KeyError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
