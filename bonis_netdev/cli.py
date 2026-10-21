"""netdev plan | show | check

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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="netdev", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="read devices, write a plan (never writes to devices)")
    p.add_argument("--intent", required=True)
    p.add_argument("--targets", required=True)
    p.add_argument("--credentials", required=True)
    p.add_argument("--out", default="plan.json")
    p.add_argument("--workers", type=int, default=6)
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("show", help="human-readable view of a stored plan")
    p.add_argument("plan")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("check", help="verify the plan_id against the content")
    p.add_argument("plan")
    p.set_defaults(fn=cmd_check)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (IntentError, FileNotFoundError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
