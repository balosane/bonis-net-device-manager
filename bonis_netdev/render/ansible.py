"""Plan -> Ansible playbook, and the check that a playbook is still exactly that plan.

The playbook is a GENERATED artefact, never hand-written. It carries the plan_id; approval is a git MR
containing plan.json + playbook.yml; the controller runs `verify_playbook` and refuses on any violation.

Deny by default, like classify.py: a step that has no mapping to an allowlisted module raises RenderError.
Nothing here talks to a device; it only produces and checks text.

Layout of the output (one play, localhost, connection: local):

    vars: netdev_plan_id, netdev_check_only, netdev_waves, netdev_api_tls, netdev_validate_certs
    tasks:
      - debug: plan id
      - block (tags: [dev_<name>])            one per device, devices sorted by name (same order as plan.json)
          prechecks of change 1               reads + assert; a failing precheck must NOT trigger a rollback
          - block: change 1 + its verify      verify tasks only run when `not netdev_check_only`
            rescue: rollback, then fail       rollback only for what this change group did, then stop

Credentials come from the variables netdev_user / netdev_pass and are never rendered into the file."""
from __future__ import annotations

import hashlib
import json
import re

import yaml

from ..plan import DevicePlan, Plan, Step

ALLOWLIST = frozenset({
    "community.routeros.api_info",
    "community.routeros.api_modify",
    "bonis.netdev.airos_cfg",
    "ansible.builtin.debug",
    "ansible.builtin.assert",
    "ansible.builtin.set_fact",
})
M_INFO, M_MODIFY, M_AIROS = "community.routeros.api_info", "community.routeros.api_modify", "bonis.netdev.airos_cfg"
M_DEBUG, M_ASSERT = "ansible.builtin.debug", "ansible.builtin.assert"

# keys of a task that are not the module
TASK_KEYWORDS = frozenset({
    "name", "block", "rescue", "always", "when", "tags", "register", "check_mode", "vars", "no_log", "ignore_errors",
    "ignore_unreachable", "delegate_to", "delegate_facts", "environment", "become", "become_user", "changed_when",
    "failed_when", "loop", "with_items", "until", "retries", "delay", "args", "connection", "diff", "run_once",
    "any_errors_fatal", "notify", "listen", "throttle", "timeout", "module_defaults", "collections", "debugger",
    "async", "poll",
})

_CONN = {"hostname": None, "username": "{{ netdev_user }}", "password": "{{ netdev_pass }}",
         "tls": "{{ netdev_api_tls }}", "validate_certs": "{{ netdev_validate_certs }}"}
_IGNORE = {"handle_absent_entries": "ignore", "handle_entries_content": "ignore"}

_SERVERS = r"[0-9a-f.:,]+"
_ROS_DNS_SET = re.compile(rf"^/ip dns set servers=({_SERVERS})$", re.I)
_ROS_NET_SET = re.compile(rf"^/ip dhcp-server network set \[find address=([0-9./]+)\] dns-server=({_SERVERS})$", re.I)
_AIROS_SET = re.compile(r"^airos-cfg-set base=([0-9a-f]{64}|current)((?: [a-z0-9_.]+=[^\s]*)+)$", re.I)
_ROS_DNS_READ = re.compile(r"^/ip dns print$")
_ROS_NET_READ = re.compile(r"^/ip dhcp-server network print detail without-paging where address=([0-9./]+)$")
_ROS_PLAIN_READ = re.compile(r"^/([a-z0-9 /-]+?) print(?: detail)?(?: without-paging)?$", re.I)
_AIROS_READ = "cat /tmp/system.cfg"
_HOST = re.compile(r"^[A-Za-z0-9._:-]+$")


class RenderError(ValueError):
    """The plan cannot be expressed as an allowlisted playbook."""


# ---------------------------------------------------------------- plan helpers

def plan_id_of(doc: dict) -> str:
    """Recompute the plan id from a plan.json document (same rule as `netdev check`)."""
    body = {"intent": doc["intent"], "devices": doc["devices"]}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def plan_from_dict(doc: dict) -> Plan:
    """Rebuild a Plan from plan.json. Steps are re-classified on the way in, so a doctored command is refused."""
    devices = []
    for d in doc["devices"]:
        d = dict(d)
        steps = [Step(s["phase"], s["command"], s["purpose"], s.get("expect"), s.get("rollback")) for s in d.pop("steps")]
        devices.append(DevicePlan(steps=steps, **d))
    return Plan(doc["intent"], devices, writes_allowed=bool(doc.get("writes_allowed", False)))


# ---------------------------------------------------------------- rendering

def _safe(text: str, what: str) -> str:
    """Ansible templates every string argument; plan text must never carry template syntax."""
    if any(m in text for m in ("{{", "{%", "{#")):
        raise RenderError(f"{what} contains template syntax: {text!r}")
    return text


def _tag(device: str) -> str:
    return "dev_" + re.sub(r"[^A-Za-z0-9]+", "_", device).strip("_")


class _Ctx:
    def __init__(self, dev: DevicePlan, index: int, check_only: bool):
        self.dev, self.index, self.check_only = dev, index, check_only
        self.host = dev.host
        self.n = 0

    def var(self) -> str:
        self.n += 1
        return f"netdev_r_{self.index}_{self.n}"

    def task(self, name: str, module: str, args: dict, *, register: str | None = None, verify: bool = False) -> dict:
        t: dict = {"name": f"{self.dev.device}: {_safe(name, 'task name')}", module: args}
        if register:
            t["register"] = register
        t["check_mode"] = True if self.check_only else "{{ netdev_check_only | bool }}"
        if verify:
            t["when"] = "not netdev_check_only"
        return t

    def conn(self) -> dict:
        return {**_CONN, "hostname": self.host}


def _assert(ctx: _Ctx, name: str, conditions: list[str], msg: str, verify: bool = False) -> dict:
    return ctx.task(name, M_ASSERT, {"that": conditions, "fail_msg": _safe(msg, "message"), "quiet": True}, verify=verify)


def _kind(dev: DevicePlan) -> str:
    if dev.driver.startswith("routeros"):
        return "routeros"
    if dev.driver == "airos":
        return "airos"
    raise RenderError(f"device {dev.device}: driver {dev.driver!r} has no Ansible mapping")


def _expect_value(step: Step, rx: str, who: str) -> str:
    m = re.fullmatch(rx, step.expect or "")
    if not m:
        raise RenderError(f"{who}: cannot render expect {step.expect!r} of step {step.command!r}")
    return m.group(1)


def _render_update(ctx: _Ctx, command: str, purpose: str, kind: str, verb: str) -> list[dict]:
    """A classified update -> tasks. Only the listed fields of the listed entries are touched."""
    d = ctx.dev.device
    if kind == "routeros" and (m := _ROS_DNS_SET.match(command)):
        return [ctx.task(f"{verb} {purpose}", M_MODIFY,
                         {**ctx.conn(), "path": "ip dns", "data": [{"servers": m.group(1)}], **_IGNORE})]
    if kind == "routeros" and (m := _ROS_NET_SET.match(command)):
        addr, servers = m.groups()
        reg = ctx.var()
        return [
            # api_modify would CREATE a missing entry; make sure it exists first
            ctx.task(f"{verb} {addr}: read networks", M_INFO, {**ctx.conn(), "path": "ip dhcp-server network"}, register=reg),
            _assert(ctx, f"{verb} {addr}: network must exist",
                    [f"{reg}.result | selectattr('address', 'equalto', '{addr}') | list | length == 1"],
                    f"{d}: DHCP network {addr} does not exist; refusing to create it"),
            ctx.task(f"{verb} {purpose}", M_MODIFY,
                     {**ctx.conn(), "path": "ip dhcp-server network",
                      "data": [{"address": addr, "dns-server": servers}], **_IGNORE}),
        ]
    if kind == "airos" and (m := _AIROS_SET.match(command)):
        base, rest = m.groups()
        settings = dict(kv.split("=", 1) for kv in rest.split())
        return [ctx.task(f"{verb} {purpose}", M_AIROS,
                         {"host": ctx.host, "username": "{{ netdev_user }}", "password": "{{ netdev_pass }}",
                          "base": base, "settings": _safe_map(settings), "state": "present"})]
    raise RenderError(f"{d}: no Ansible mapping for update {command!r}")


def _safe_map(settings: dict) -> dict:
    return {_safe(k, "airOS key"): _safe(v, "airOS value") for k, v in settings.items()}


def _render_read(ctx: _Ctx, step: Step, kind: str, verify: bool) -> list[dict]:
    d, cmd, what = ctx.dev.device, step.command, step.phase
    if kind == "airos":
        if cmd != _AIROS_READ:
            raise RenderError(f"{d}: no Ansible mapping for read {cmd!r}")
        if what == "precheck":
            # no read-only airOS module; the base hash is enforced by airos_cfg itself when it changes the file
            return [ctx.task(f"{step.purpose} (base hash is enforced by bonis.netdev.airos_cfg)", M_DEBUG,
                             {"msg": _safe(f"precheck: {step.expect or 'none'}", "message")})]
        settings = dict(kv.split("=", 1) for kv in _expect_value(step, r"((?:[a-z0-9_.]+=\S*)(?: [a-z0-9_.]+=\S*)*)", d).split())
        reg = ctx.var()
        return [
            # re-apply the expected values against the current file: nothing may change
            ctx.task(f"{step.purpose}", M_AIROS,
                     {"host": ctx.host, "username": "{{ netdev_user }}", "password": "{{ netdev_pass }}",
                      "base": "current", "settings": _safe_map(settings), "state": "present"}, register=reg, verify=True),
            _assert(ctx, f"{step.purpose}: nothing left to change", [f"not {reg}.changed"],
                    f"{d}: airOS config does not hold the expected values", verify=True),
        ]
    reg = ctx.var()
    if m := _ROS_DNS_READ.match(cmd):
        tasks = [ctx.task(f"{step.purpose}", M_INFO, {**ctx.conn(), "path": "ip dns"}, register=reg, verify=verify)]
        if step.expect is not None:
            want = _expect_value(step, rf"servers:\s*({_SERVERS}|)", d)
            tasks.append(_assert(ctx, f"{step.purpose}: value", [f"(({reg}.result | first).servers | default('')) == '{want}'"],
                                 f"{d}: resolver servers are not {want or '(none)'}", verify))
        return tasks
    if m := _ROS_NET_READ.match(cmd):
        addr = m.group(1)
        sel = f"{reg}.result | selectattr('address', 'equalto', '{addr}') | list"
        tasks = [ctx.task(f"{step.purpose}", M_INFO, {**ctx.conn(), "path": "ip dhcp-server network"}, register=reg, verify=verify)]
        cond = f"({sel}) | length == 1"
        if step.expect is not None:
            want = _expect_value(step, rf"dns-server=({_SERVERS}|)", d)
            cond += f" and ((({sel}) | first)['dns-server'] | default('')) == '{want}'"
            msg = f"{d}: DHCP network {addr} does not have dns-server={want or '(none)'}"
        else:
            msg = f"{d}: DHCP network {addr} does not exist"
        tasks.append(_assert(ctx, f"{step.purpose}: value", [cond], msg, verify))
        return tasks
    if m := _ROS_PLAIN_READ.match(cmd):
        if step.expect is not None:
            raise RenderError(f"{d}: cannot check expect {step.expect!r} of read {cmd!r}")
        return [ctx.task(f"{step.purpose}", M_INFO, {**ctx.conn(), "path": " ".join(m.group(1).replace("/", " ").split())},
                         register=reg, verify=verify)]
    raise RenderError(f"{d}: no Ansible mapping for read {cmd!r}")


def _groups(steps: list[Step]):
    """Split steps into (prechecks, change, verifies) groups; stray reads become a group without a change."""
    out, pre, cur = [], [], None
    for s in steps:
        if s.phase == "precheck":
            if cur is not None:
                cur = None
            pre.append(s)
        elif s.phase == "change":
            cur = (pre, s, [])
            out.append(cur)
            pre = []
        else:
            if cur is None:
                raise RenderError(f"verify step {s.command!r} has no change before it")
            cur[2].append(s)
    if pre:
        out.append((pre, None, []))
    return out


def _render_device(dev: DevicePlan, index: int, check_only: bool) -> dict | None:
    if dev.error:
        raise RenderError(f"device {dev.device}: the plan records an error ({dev.error}); re-plan it first")
    if not _HOST.match(dev.host):
        raise RenderError(f"device {dev.device}: host {dev.host!r} is not a plain address")
    _safe(dev.device, "device name")
    if not dev.steps:
        return None
    kind = _kind(dev)
    ctx = _Ctx(dev, index, check_only)
    body: list[dict] = []
    for pre, change, post in _groups(dev.steps):
        for s in pre:
            body += _render_read(ctx, s, kind, verify=False)
        if change is None:
            continue
        inner = _render_update(ctx, change.command, change.purpose, kind, "change")
        for s in post:
            inner += _render_read(ctx, s, kind, verify=True)
        group: dict = {"block": inner}
        if change.rollback:
            rollback = _render_update(ctx, change.rollback, f"undo {change.purpose}", kind, "rollback")
            stop = _assert(ctx, "stop after rollback", ["false"], f"{dev.device}: change failed; rollback was run, stopping")
            group["rescue"] = rollback + [stop]
        body.append(group)
    return {"name": f"{dev.device} ({dev.host}, {dev.driver})", "tags": [_tag(dev.device)], "block": body}


def _build(plan: Plan, check_only: bool) -> list[dict]:
    devices = sorted(plan.devices, key=lambda d: d.device)
    blocks, tags = [], []
    for i, dev in enumerate(devices):
        blk = _render_device(dev, i, check_only)
        if blk is None:
            continue
        if blk["tags"][0] in tags:
            raise RenderError(f"device names {dev.device!r} and another map to the same tag {blk['tags'][0]!r}")
        tags.append(blk["tags"][0])
        blocks.append(blk)
    pid = plan.plan_id
    tasks = [{"name": f"plan {pid[:12]}: {'check mode only' if check_only else 'APPLY MODE'}",
              M_DEBUG: {"msg": f"netdev plan {pid}"},
              "check_mode": True if check_only else "{{ netdev_check_only | bool }}"}] + blocks
    play = {
        "name": f"netdev plan {pid[:12]}",
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars": {"netdev_plan_id": pid, "netdev_check_only": check_only, "netdev_waves": [tags],
                 "netdev_api_tls": False, "netdev_validate_certs": False},
        "tasks": tasks,
    }
    return [play]


def render_playbook(plan: Plan, *, check_only: bool = True) -> str:
    """YAML text of the playbook for `plan`. Deterministic. Raises RenderError for anything not mappable."""
    play = _build(plan, check_only)
    header = (f"# GENERATED by bonis-netdev from plan {plan.plan_id}. Do not edit: `netdev verify-playbook` re-renders and compares.\n"
              f"# mode: {'check only (every task has check_mode: true)' if check_only else 'APPLY MODE (check_mode follows netdev_check_only)'}\n"
              "# credentials: variables netdev_user / netdev_pass, never stored here.\n"
              "# waves: netdev_waves lists device tags; select a wave with --tags.\n")
    return header + yaml.safe_dump(play, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1_000_000)


# ---------------------------------------------------------------- verification

def _walk(tasks):
    for t in tasks:
        yield t
        if isinstance(t, dict):
            for k in ("block", "rescue", "always"):
                if isinstance(t.get(k), list):
                    yield from _walk(t[k])


def _label(t) -> str:
    return repr(t.get("name", "?")) if isinstance(t, dict) else repr(t)


def verify_playbook(plan_json: dict, playbook_text: str, *, require_check_only: bool = False) -> list[str]:
    """Violations that make the playbook unfit to run for this plan; empty list = ok."""
    v: list[str] = []
    try:
        real_id = plan_id_of(plan_json)
    except (KeyError, TypeError) as e:
        return [f"plan JSON is not a plan ({type(e).__name__}: {e})"]
    if plan_json.get("plan_id") != real_id:
        v.append(f"plan JSON was modified: its plan_id {plan_json.get('plan_id')} != recomputed {real_id}")
    try:
        docs = yaml.safe_load(playbook_text)
    except yaml.YAMLError as e:
        return v + [f"playbook is not valid YAML: {e}"]
    if not (isinstance(docs, list) and len(docs) == 1 and isinstance(docs[0], dict)):
        return v + ["playbook must be exactly one play"]
    play = docs[0]
    pvars = play.get("vars") if isinstance(play.get("vars"), dict) else {}
    if pvars.get("netdev_plan_id") != real_id:
        v.append(f"playbook plan_id {pvars.get('netdev_plan_id')} does not match the plan {real_id}")
    check_only = pvars.get("netdev_check_only")
    if not isinstance(check_only, bool):
        v.append("play var netdev_check_only is missing or not a boolean")
    elif require_check_only and not check_only:
        v.append("playbook is rendered in apply mode but check-only was required")
    for key in play:
        if key not in ("name", "hosts", "connection", "gather_facts", "vars", "tasks"):
            v.append(f"play key {key!r} is not allowed (pre_tasks/roles/handlers/imports could run unreviewed code)")
    tasks = play.get("tasks")
    if not isinstance(tasks, list):
        return v + ["play has no task list"]

    for t in _walk(tasks):
        if not isinstance(t, dict):
            v.append(f"task {_label(t)} is not a mapping")
            continue
        mods = [k for k in t if k not in TASK_KEYWORDS]
        if len(mods) > 1:
            v.append(f"task {_label(t)} has more than one module key: {sorted(mods)}")
        for m in mods:
            if m not in ALLOWLIST:
                v.append(f"task {_label(t)} uses module {m!r} which is not in the allowlist")
        if not mods and "block" not in t:
            v.append(f"task {_label(t)} has no module")
        if check_only is True and "block" not in t and t.get("check_mode") is not True:
            v.append(f"task {_label(t)} lacks check_mode: true in a check-only playbook")

    if isinstance(check_only, bool):
        try:
            expected = _build(plan_from_dict(plan_json), check_only)[0]
        except (RenderError, ValueError, TypeError, KeyError) as e:
            return v + [f"plan cannot be rendered: {e}"]
        if expected["vars"] != pvars:
            v.append("play vars differ from the re-rendered playbook")
        for k in ("hosts", "connection", "gather_facts"):
            if play.get(k) != expected[k]:
                v.append(f"play key {k!r} differs from the re-rendered playbook")
        if tasks != expected["tasks"]:
            v.append("task list differs from the playbook re-rendered from the plan "
                     "(reordered, added, removed or modified task)")
    return v
