# -*- coding: utf-8 -*-
"""Core of the bonis.netdev.routeros_service module: no ansible imports, so it is testable on its own.

Ansible runs a module from a zip of the collection, so this file must not import bonis_netdev. The
SSH options mirror module_utils/airos_cfg.py (a test guards the drift).

Purpose: the RouterOS API is disabled on part of the fleet, so the normal API modules cannot reach those
routers; enabling the API itself has to happen over SSH. The module is deliberately NARROW: it reads and
sets ONE service (`api`), two properties (`disabled`, `address`). It is not a command runner.

WRITES are guarded by the code constant WRITES_ENABLED (fleet-wide write ban; turning it on must be a
reviewed commit, not a config switch). Every command goes through `GuardedTransport`, which runs only the
one read command and, when writes are enabled, the one write shape.
"""
from __future__ import absolute_import, division, print_function

__metaclass__ = type

import ipaddress
import os
import re
import subprocess

# Reviewed-commit switch. While False, a run that would change the device fails (check_mode is unaffected).
WRITES_ENABLED = False

READ_CMD = "/ip service print detail without-paging where name=api"
MAX_ADDRESSES = 8
MAX_RESTORE_ADDRESSES = 32
MIN_PREFIX = 24

ARGUMENT_SPEC = dict(
    host=dict(type="str", required=True),
    username=dict(type="str", required=True),
    password=dict(type="str", required=True, no_log=True),
    port=dict(type="int", default=22),
    service=dict(type="str", default="api", choices=["api"]),
    disabled=dict(type="bool", required=True),
    address=dict(type="list", elements="str"),
    from_disabled=dict(type="bool"),
    from_address=dict(type="list", elements="str"),
    restore=dict(type="bool", default=False),
)

# mirrors module_utils/airos_cfg.py SSH_OPTS (legacy SSH servers need ssh-rsa / old kex)
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=8", "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
    "-o", "HostKeyAlgorithms=+ssh-rsa,ssh-dss", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa,ssh-dss",
    "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
]


class ServiceError(Exception):
    """A failure that is reported to the user as `msg`; never carries secrets."""


class CommandRefused(ServiceError):
    pass


# --- address validation -------------------------------------------------------------------------------
_OCT = r"(?:0|[1-9]\d{0,2})"
_STRICT_RX = re.compile(r"^%s(?:\.%s){3}(?:/(?:0|[1-9]\d?))?\Z" % (_OCT, _OCT))


def _norm(entry):
    """Canonical form for comparison: IPv4 -> 'a.b.c.d/len'. Anything else is returned unchanged."""
    try:
        return str(ipaddress.IPv4Network(entry, strict=False))
    except ValueError:
        return entry


def validate_address(entry):
    """Return the entry or raise ServiceError. Strict: this text ends up in a remote command."""
    if not isinstance(entry, str) or not _STRICT_RX.match(entry):
        raise ServiceError("address entry %r is not a canonical IPv4 address or IPv4/prefix" % (entry,))
    try:
        net = ipaddress.IPv4Network(entry, strict=False)
    except ValueError:
        raise ServiceError("address entry %r is not a valid IPv4 address or IPv4/prefix" % (entry,))
    if net.prefixlen < MIN_PREFIX:
        raise ServiceError("address entry %s: prefix shorter than /%d is refused (0.0.0.0/0 included)" % (entry, MIN_PREFIX))
    first, last = net.network_address, net.broadcast_address
    for ip in (first, last):
        if int(ip) >> 24 == 0:
            raise ServiceError("address entry %s is in 0.0.0.0/8 (unspecified)" % entry)
        if ip.is_loopback:
            raise ServiceError("address entry %s is loopback" % entry)
        if ip.is_multicast:
            raise ServiceError("address entry %s is multicast" % entry)
        if ip.is_reserved:
            raise ServiceError("address entry %s is reserved" % entry)
    if "/" not in entry and str(first) == "255.255.255.255":
        raise ServiceError("address entry %s is the broadcast address" % entry)
    return entry


def validate_addresses(entries, what="address"):
    if not isinstance(entries, list):
        raise ServiceError("%s must be a list" % what)
    if len(entries) > MAX_ADDRESSES:
        raise ServiceError("%s: at most %d entries allowed" % (what, MAX_ADDRESSES))
    seen = set()
    for e in entries:
        validate_address(e)
        n = _norm(e)
        if n in seen:
            raise ServiceError("%s: duplicate entry %s" % (what, e))
        seen.add(n)
    return list(entries)


def validate_restore_addresses(entries, what="address"):
    """Desired addresses of a RESTORE (rollback to a previous device state): anything the write shape can express.

    Empty list (no restriction), IPv4 or IPv4/prefix of any length 0-32 (host bits allowed, the device accepted them once),
    up to MAX_RESTORE_ADDRESSES entries, no duplicates after normalisation. No host names, no IPv6, no garbage."""
    if not isinstance(entries, list):
        raise ServiceError("%s must be a list" % what)
    if len(entries) > MAX_RESTORE_ADDRESSES:
        raise ServiceError("%s: at most %d entries allowed even for a restore" % (what, MAX_RESTORE_ADDRESSES))
    seen = set()
    for e in entries:
        if not isinstance(e, str) or not _ADDR_RX.fullmatch(e):
            raise ServiceError("address entry %r cannot be restored: not an IPv4 address or IPv4/prefix of the write shape" % (e,))
        n = _norm(e)
        if n in seen:
            raise ServiceError("%s: duplicate entry %s" % (what, e))
        seen.add(n)
    return list(entries)


def validate_params(params):
    """Return (disabled, address or None, from_disabled or None, from_address or None, restore)."""
    if params.get("service", "api") != "api":
        raise ServiceError("service must be api")
    port = params.get("port", 22)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ServiceError("port must be 1-65535")
    for name in ("host", "username", "password"):
        if not params.get(name):
            raise ServiceError("%s is required" % name)
    if not re.match(r"^[A-Za-z0-9_.:\-]+\Z", params["host"]) or params["host"].startswith("-"):
        raise ServiceError("invalid host")
    if not re.match(r"^[A-Za-z0-9_.\-]+\Z", params["username"]) or params["username"].startswith("-"):
        raise ServiceError("invalid username")
    disabled = params.get("disabled")
    if not isinstance(disabled, bool):
        raise ServiceError("disabled must be a boolean")
    address = params.get("address")
    restore = params.get("restore", False)
    if not isinstance(restore, bool):
        raise ServiceError("restore must be a boolean")
    if restore:
        # a restore returns the service to a PREVIOUS state, which can be anything the write shape expresses; it is only
        # accepted with the drift guard to the state the forward change created
        if params.get("from_disabled") is None or params.get("from_address") is None:
            raise ServiceError("restore requires both from_disabled and from_address (the drift guard)")
        if address is None:
            raise ServiceError("restore requires address (an empty list restores an unrestricted state)")
        address = validate_restore_addresses(address, "address")
    elif address is not None:
        address = validate_addresses(address, "address")
    if not restore and disabled is False and not address:
        raise ServiceError("address must be a non-empty list when disabled is false (enabling the API without a "
                           "source restriction is refused)")
    fd = params.get("from_disabled")
    if fd is not None and not isinstance(fd, bool):
        raise ServiceError("from_disabled must be a boolean")
    fa = params.get("from_address")
    if fa is not None:
        if not isinstance(fa, list) or not all(isinstance(x, str) for x in fa):
            raise ServiceError("from_address must be a list of strings")
        fa = [x.strip() for x in fa]
    return disabled, address, fd, fa, restore


# --- parsing ------------------------------------------------------------------------------------------
_ENTRY_RX = re.compile(r"^\s*(\d+)\s+((?:[A-Z]+\s+)*)(.*)$")
_KV_RX = re.compile(r'([A-Za-z][\w-]*)=("[^"]*"|\S*)')
_COLON_RX = re.compile(r"^\s*([A-Za-z][\w-]*):\s*(.*)$")


def _entries(text):
    """Split `print detail` output into [(flags, 'raw text')]. Wrapped lines are joined to their entry."""
    out = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith(("Flags:", "Columns:")):
            continue
        m = _ENTRY_RX.match(line)
        if m:
            out.append([m.group(2).replace(" ", ""), m.group(3).strip(), [m.group(3)]])
        elif out:
            body = line.strip()
            out[-1][1] += body if out[-1][1].endswith(",") else " " + body
            out[-1][2].append(line)
    return out


_ADDR_LAST_RX = re.compile(r'address=(?:"[^"]*"|\S*)\s*$')


def _address_wrapped(lines):
    """True when the address value may continue on a following line: `address=` is the last token of a line that is
    followed by a continuation line (a wrapped list, e.g. a continuation starting with a comma). `address=""` is complete."""
    for i, line in enumerate(lines[:-1]):
        m = _ADDR_LAST_RX.search(line)
        if m and m.group(0).rstrip() != 'address=""':
            return True
    return False


def parse_service(text):
    """Return {'disabled': bool, 'address': [str]} for the single `api` row or raise ServiceError."""
    rows = []
    for flags, body, lines in _entries(text):
        kv = dict((k, v.strip('"')) for k, v in _KV_RX.findall(body))
        if kv.get("name") == "api":
            if _address_wrapped(lines):
                raise ServiceError("the address value of service api is wrapped over several lines in the device output; "
                                   "refusing to guess (no change made)")
            rows.append((flags, kv, True))
    if not rows:  # `key: value` layout of a single record (name: api / disabled: no)
        kv = {}
        for line in text.splitlines():
            m = _COLON_RX.match(line)
            if m:
                kv[m.group(1)] = m.group(2).strip().strip('"')
        if kv.get("name") == "api":
            rows.append(("", kv, False))
    if not rows:
        raise ServiceError("no row for service api in the device output")
    if len(rows) > 1:
        raise ServiceError("ambiguous: %d rows for service api in the device output" % len(rows))
    flags, kv, _ = rows[0]
    if "I" in flags and "X" not in flags:
        # RouterOS sets I together with X on every disabled service; I alone is unexplained, so refuse
        raise ServiceError("service api is flagged invalid (I) without being disabled (X); refusing to reason about it")
    if "disabled" in kv:
        if kv["disabled"] not in ("yes", "no", "true", "false"):
            raise ServiceError("unreadable disabled value in the device output")
        disabled = kv["disabled"] in ("yes", "true")
        if "X" in flags and not disabled:
            raise ServiceError("conflicting disabled information in the device output")
    else:
        disabled = "X" in flags
    raw = kv.get("address", "")
    address = [a.strip() for a in raw.split(",") if a.strip()]
    return {"disabled": disabled, "address": address}


# --- command generation and classification ------------------------------------------------------------
_ADDR_CMD = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}(?:/(?:3[0-2]|[12]?\d))?"
_ADDR_RX = re.compile(_ADDR_CMD)
_WRITE_RX = re.compile(r'^/ip service set \[find name=api\] disabled=(?:yes|no) address=(?:""|%s(?:,%s)*)\Z'
                       % (_ADDR_CMD, _ADDR_CMD))


def cmd_set(disabled, addresses):
    """The ONE write shape. An empty address list (`address=""`) is used only to restore an unrestricted state."""
    return "/ip service set [find name=api] disabled=%s address=%s" % (
        "yes" if disabled else "no", ",".join(addresses) if addresses else '""')


def classify(command):
    """'read', 'write' or 'unknown'. Deny by default (idea of bonis_netdev/classify.py)."""
    if not isinstance(command, str):
        return "unknown"
    if any(c in command for c in (";", "\n", "\r", "`")) or "$(" in command or "{{" in command:
        return "unknown"
    if command == READ_CMD:
        return "read"
    if _WRITE_RX.match(command):
        return "write"
    return "unknown"


# --- transport ----------------------------------------------------------------------------------------
class Transport(object):
    """Anything that can run one remote command and return its stdout (raise ServiceError on failure)."""

    def run(self, command):  # pragma: no cover - protocol
        raise NotImplementedError


class GuardedTransport(Transport):
    """Wraps a transport; refuses any command that is not allowed, BEFORE it reaches the wire."""

    def __init__(self, inner):
        self._inner = inner

    def run(self, command):
        kind = classify(command)
        if kind == "read":
            return self._inner.run(command)
        if kind == "write" and WRITES_ENABLED:
            return self._inner.run(command)
        raise CommandRefused("command refused by the allowlist (writes enabled: %s)" % bool(WRITES_ENABLED))


class SshTransport(Transport):
    """Password SSH via sshpass (-e: the password is passed in the environment, not in argv)."""

    def __init__(self, host, port, username, password, timeout=30):
        self.host, self.port, self.username, self._password, self.timeout = host, port, username, password, timeout

    def run(self, command):
        env = dict(os.environ, SSHPASS=self._password)
        argv = ["sshpass", "-e", "ssh"] + SSH_OPTS + ["-p", str(self.port), "%s@%s" % (self.username, self.host), command]
        try:
            p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                               timeout=self.timeout, universal_newlines=True)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ServiceError("ssh to %s failed: %s" % (self.host, type(e).__name__))
        if p.returncode != 0:
            raise ServiceError("ssh to %s: command exited with status %s" % (self.host, p.returncode))
        return p.stdout


# --- logic --------------------------------------------------------------------------------------------
def _same(a, b):
    return a["disabled"] == b["disabled"] and sorted(map(_norm, a["address"])) == sorted(map(_norm, b["address"]))


def _read(t):
    return parse_service(t.run(READ_CMD))


def _view(state):
    return {"disabled": state["disabled"], "address": list(state["address"])}


def run_module(params, check_mode, transport):
    """Returns the result dict; `failed` is True on failure. Never raises for expected failures."""
    result = {"changed": False, "msg": "", "diff": {"before": {}, "after": {}}, "current": {}}
    try:
        disabled, address, from_disabled, from_address, restore = validate_params(params)
        t = GuardedTransport(transport)
        cur = _read(t)
        result["current"] = _view(cur)
        result["diff"] = {"before": _view(cur), "after": _view(cur)}
        if (from_disabled is not None and from_disabled != cur["disabled"]) or \
                (from_address is not None and sorted(map(_norm, from_address)) != sorted(map(_norm, cur["address"]))):
            if restore:
                msg = ("state already differs from the one the change created (the module may have restored it itself, "
                       "or the change was never made); nothing restored")
            else:
                msg = ("device changed since the plan was made (the api service no longer matches "
                       "from_disabled/from_address); re-plan")
            result.update(failed=True, msg=msg)
            return result
        target = {"disabled": disabled, "address": list(address) if address is not None else list(cur["address"])}
        result["diff"] = {"before": _view(cur), "after": _view(target)}
        if _same(cur, target):
            result["diff"]["after"] = _view(cur)
            result["msg"] = "service api is already in the requested state"
            return result
        if check_mode:
            result.update(changed=True, msg="would change service api: disabled=%s address=%s" % (
                "yes" if disabled else "no", ",".join(target["address"]) or "<none>"))
            return result
        if not WRITES_ENABLED:
            result.update(failed=True, msg="writes are disabled in this module version (WRITES_ENABLED is False); "
                          "use check_mode. Would change service api to disabled=%s address=%s" % (
                              "yes" if disabled else "no", ",".join(target["address"]) or "<none>"))
            return result
        forward = cmd_set(target["disabled"], target["address"])
        back = cmd_set(cur["disabled"], cur["address"])
        if classify(forward) != "write" or classify(back) != "write":
            raise ServiceError("cannot express the new or the previous state with the write shape (unusual address "
                               "entries on the device); nothing was written")
        t.run(forward)
        try:
            after = _read(t)
            problem = "" if _same(after, target) else "verification failed after the change"
        except ServiceError:
            after, problem = None, "the device could not be read after the change"
        if not problem:
            result.update(changed=True, msg="changed service api", diff={"before": _view(cur), "after": _view(after)},
                          current=_view(after))
            return result
        # one restore attempt, whatever went wrong after the write; the playbook's own rescue then sees the restored
        # state and reports that the state differs from the one the change created
        restored, note = False, "; RESTORE FAILED, check the device"
        try:
            t.run(back)
            if _same(_read(t), cur):
                restored, note = True, "; previous values restored by the module itself (restored: true)"
            else:
                note = "; RESTORE ALSO FAILED, check the device"
        except ServiceError:
            pass
        result.update(failed=True, changed=False, restored=restored, msg=problem + note)
        return result
    except ServiceError as e:
        result.update(failed=True, changed=False, msg=str(e))
        return result


def make_transport(params):
    return SshTransport(params["host"], params["port"], params["username"], params["password"])
