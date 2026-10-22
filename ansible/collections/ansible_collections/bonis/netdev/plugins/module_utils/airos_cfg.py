# -*- coding: utf-8 -*-
"""Core of the bonis.netdev.airos_cfg module: no ansible imports, so it is testable on its own.

Ansible runs a module from a zip of the collection, so this file must not import bonis_netdev.
The parts that mirror the tool are marked "mirrors bonis_netdev/..." and tests/test_airos_module.py
runs both implementations on the same inputs to catch drift.

WRITES are guarded by the code constant WRITES_ENABLED (fleet-wide write ban; turning it on must be
a reviewed commit, not a config switch). Every command goes through `GuardedTransport`, which runs
only the fixed read command and, when writes are enabled, commands of the generated write shapes.
"""
from __future__ import absolute_import, division, print_function

__metaclass__ = type

import hashlib
import os
import re
import subprocess

# Reviewed-commit switch. While False, a run that would change the device fails (check_mode is unaffected).
WRITES_ENABLED = False

CFG_PATH = "/tmp/system.cfg"
READ_CFG = "cat /tmp/system.cfg"
PERSIST = "cfgmtd -w -p /etc/persistent/"
FIX_EOL = "sed -i -e '$a\\' /tmp/system.cfg"

KEY_RX = re.compile(r"^[A-Za-z0-9_.\-]+\Z")
BASE_RX = re.compile(r"^(?:[0-9a-f]{64}|current)\Z")
# secrets never go into diff/msg: values of keys that look like credentials are masked
SECRET_KEY_RX = re.compile(r"(psk|passw|secret|hash|token|key$|community)", re.I)
REDACTED = "<redacted>"

ARGUMENT_SPEC = dict(
    host=dict(type="str", required=True),
    username=dict(type="str", required=True),
    password=dict(type="str", required=True, no_log=True),
    port=dict(type="int", default=22),
    base=dict(type="str", required=True),
    settings=dict(type="dict", required=True),
    state=dict(type="str", default="present", choices=["present"]),
)

# mirrors bonis_netdev/transport.py SSH_OPTS (legacy airOS SSH needs ssh-rsa / old kex)
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=8", "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
    "-o", "HostKeyAlgorithms=+ssh-rsa,ssh-dss", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa,ssh-dss",
    "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
]


class AirosCfgError(Exception):
    """A failure that is reported to the user as `msg`; never carries config contents or secrets."""


class CommandRefused(AirosCfgError):
    pass


# --- parsing (mirrors bonis_netdev/parse.py cfg_parse; keep identical) ---------------------------------
def cfg_parse(text):
    cfg = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            cfg[k] = v
    return cfg


def sha256_text(text):
    # mirrors bonis_netdev/drivers/airos.py: hashlib.sha256(text.encode()).hexdigest()
    return hashlib.sha256(text.encode()).hexdigest()


# --- validation ---------------------------------------------------------------------------------------
def validate_settings(settings):
    """Return {key: str value} or raise AirosCfgError. Strict: this text ends up in a remote shell."""
    if not isinstance(settings, dict) or not settings:
        raise AirosCfgError("settings must be a non-empty dict of key -> value")
    out = {}
    for key, value in settings.items():
        if not isinstance(key, str) or not KEY_RX.match(key):
            raise AirosCfgError("invalid setting key %r (allowed: letters, digits, _ . -)" % (key,))
        if SECRET_KEY_RX.search(key):
            raise AirosCfgError("setting %s looks like a credential; such keys are refused because their values would "
                                "leak through module_args and -v output" % key)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise AirosCfgError("value of %s must be a string (quote it in the playbook)" % key)
        value = str(value)
        if any(c in value for c in ("\n", "\r", "\0", "`", ";")) or "$(" in value:
            raise AirosCfgError("value of %s contains a forbidden character (newline, backtick, ';' or '$(')" % key)
        if value != value.strip():
            raise AirosCfgError("value of %s has leading or trailing whitespace" % key)
        out[key] = value
    return out


def validate_params(params):
    if params.get("state", "present") != "present":
        raise AirosCfgError("state must be present")
    if not BASE_RX.match(str(params.get("base", ""))):
        raise AirosCfgError("base must be a 64-hex sha256 or the literal 'current'")
    port = params.get("port", 22)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise AirosCfgError("port must be 1-65535")
    for name in ("host", "username", "password"):
        if not params.get(name):
            raise AirosCfgError("%s is required" % name)
    if not re.match(r"^[A-Za-z0-9_.:\-]+\Z", params["host"]) or params["host"].startswith("-"):
        raise AirosCfgError("invalid host")
    if not re.match(r"^[A-Za-z0-9_.\-]+\Z", params["username"]) or params["username"].startswith("-"):
        raise AirosCfgError("invalid username")
    return validate_settings(params.get("settings"))


# --- command generation and classification ------------------------------------------------------------
def _sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _sed_value(v):
    return v.replace("\\", "\\\\").replace("&", "\\&").replace("|", "\\|")


def cmd_set(key, value):
    """Replace the line of an existing key."""
    return "sed -i %s %s" % (
        _sh_quote("s|^%s=.*$|%s=%s|" % (key.replace(".", "\\."), key, _sed_value(value))), CFG_PATH)


def cmd_append(key, value):
    return "printf '%%s\\n' %s >> %s" % (_sh_quote("%s=%s" % (key, value)), CFG_PATH)


def cmd_delete(key):
    return "sed -i %s %s" % (_sh_quote("/^%s=/d" % key.replace(".", "\\.")), CFG_PATH)


_K = r"[A-Za-z0-9_\-]|\\\."
_PLAIN_K = r"[A-Za-z0-9_.\-]+"
_V = r"(?:[^'\n\r`\\|&]|\\[\\&|]|'\\'')*"
_WRITE_RULES = [
    re.compile(r"^sed -i 's\|\^(?:%s)+=\.\*\$\|%s=%s\|' /tmp/system\.cfg$" % (_K, _PLAIN_K, _V)),
    re.compile(r"^printf '%%s\\n' '%s=(?:[^'\n\r`]|'\\'')*' >> /tmp/system\.cfg$" % _PLAIN_K),
    re.compile(r"^sed -i '/\^(?:%s)+=/d' /tmp/system\.cfg$" % _K),
    re.compile(r"^" + re.escape(FIX_EOL) + r"$"),
    re.compile(r"^" + re.escape(PERSIST) + r"$"),
]


def classify(command):
    """'read', 'write' or 'unknown'. Deny by default (idea of bonis_netdev/classify.py)."""
    if command == READ_CFG:
        return "read"
    if "\n" in command or "\r" in command or "`" in command or "$(" in command or ";" in command:
        return "unknown"
    for rx in _WRITE_RULES:
        if rx.match(command):
            return "write"
    return "unknown"


# --- transport ----------------------------------------------------------------------------------------
class Transport(object):
    """Anything that can run one remote command and return its stdout (raise AirosCfgError on failure)."""

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
            raise AirosCfgError("ssh to %s failed: %s" % (self.host, type(e).__name__))
        if p.returncode != 0:
            raise AirosCfgError("ssh to %s: command exited with status %s" % (self.host, p.returncode))
        return p.stdout


# --- logic --------------------------------------------------------------------------------------------
def _mask(key, value):
    return REDACTED if SECRET_KEY_RX.search(key) and value is not None else value


def _render(keys, cfg):
    return "".join("%s=%s\n" % (k, _mask(k, cfg[k])) for k in keys if k in cfg)


def _read(transport):
    text = transport.run(READ_CFG)
    return text, cfg_parse(text), sha256_text(text)


def _write_plan(changes, before, text):
    cmds = []
    if any(k not in before for k in changes) and text and not text.endswith("\n"):
        cmds.append(FIX_EOL)
    for k in sorted(changes):
        cmds.append(cmd_set(k, changes[k]) if k in before else cmd_append(k, changes[k]))
    cmds.append(PERSIST)
    return cmds


def _restore_cmds(changes, before):
    cmds = [cmd_set(k, before[k]) if k in before else cmd_delete(k) for k in sorted(changes)]
    cmds.append(PERSIST)
    return cmds


def run_module(params, check_mode, transport):
    """Run the module logic. Returns the result dict; `failed` is True on failure. Never raises for
    expected failures. `transport` is a Transport (the real one is built by `make_transport`)."""
    result = {"changed": False, "msg": "", "base_sha256": "", "diff": {"before": "", "after": ""}}
    try:
        settings = validate_params(params)
        t = GuardedTransport(transport)
        text, cfg, digest = _read(t)
        result["base_sha256"] = digest
        base = params["base"]
        if base != "current" and base != digest:
            result.update(failed=True, msg="device config changed since the plan was made (base sha256 differs); re-plan")
            return result
        keys = sorted(settings)
        changes = {k: v for k, v in settings.items() if cfg.get(k) != v}
        touched = sorted(changes)
        result["diff"] = {"before": _render(touched, cfg),
                          "after": _render(touched, dict(cfg, **changes))}
        ind = set(cfg_parse("\n".join(l for l in text.splitlines() if l != l.lstrip())))
        indented = sorted(k for k in changes if k in ind)
        if indented:
            result.update(failed=True, msg="key(s) %s are present in the config with leading whitespace; the write "
                          "command only matches lines starting at column 0, refusing a silent no-op" % ", ".join(indented))
            return result
        if not changes:
            result["msg"] = "already in the requested state (%d keys checked)" % len(keys)
            return result
        if check_mode:
            result.update(changed=True, msg="would change %d key(s): %s" % (len(touched), ", ".join(touched)))
            return result
        if not WRITES_ENABLED:
            result.update(failed=True, msg="writes are disabled in this module version (WRITES_ENABLED is False); "
                          "use check_mode. Would change %d key(s): %s" % (len(touched), ", ".join(touched)))
            return result
        for c in _write_plan(changes, cfg, text):
            t.run(c)
        _, after, new_digest = _read(t)
        bad = [k for k in touched if after.get(k) != changes[k]]
        if bad:
            restore_err = ""
            try:
                for c in _restore_cmds(changes, cfg):
                    t.run(c)
                _, back, _ = _read(t)
                if any(back.get(k) != cfg.get(k) for k in touched):
                    restore_err = "; RESTORE ALSO FAILED, check the device"
            except AirosCfgError:
                restore_err = "; RESTORE FAILED, check the device"
            tail = restore_err or "; previous values restored"
            result.update(failed=True, msg="verification failed for key(s): %s%s" % (", ".join(bad), tail))
            return result
        result.update(changed=True, base_sha256=digest,
                      msg="changed %d key(s): %s" % (len(touched), ", ".join(touched)),
                      new_base_sha256=new_digest)
        return result
    except AirosCfgError as e:
        result.update(failed=True, changed=False, msg=str(e))
        return result


def make_transport(params):
    return SshTransport(params["host"], params["port"], params["username"], params["password"])
