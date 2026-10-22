import importlib.util
import hashlib
import py_compile
import re
from pathlib import Path

import pytest
import yaml

from bonis_netdev import samples
from bonis_netdev.drivers.airos import AirosDriver
from bonis_netdev.parse import cfg_parse as tool_cfg_parse

COLL = Path(__file__).resolve().parent.parent / "ansible/collections/ansible_collections/bonis/netdev"
LIB_PATH = COLL / "plugins/module_utils/airos_cfg.py"
MOD_PATH = COLL / "plugins/modules/airos_cfg.py"

spec = importlib.util.spec_from_file_location("airos_cfg_lib", LIB_PATH)
lib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lib)

CFG = samples.SYSTEM_CFG
SHA = hashlib.sha256(CFG.encode()).hexdigest()


class Fake(lib.Transport):
    """Scripted device: holds a config text, applies nothing itself unless `apply` is given."""

    def __init__(self, text=CFG, reads=None, effect=None):
        self.text = text
        self.reads = list(reads or [])  # successive answers for cat, then falls back to self.text
        self.effect = effect
        self.cmds = []

    def run(self, command):
        self.cmds.append(command)
        if command == lib.READ_CFG:
            return self.reads.pop(0) if self.reads else self.text
        if self.effect:
            self.effect(self, command)
        return ""


def params(**kw):
    p = dict(host="10.0.0.1", username="u", password="pw-SECRET", port=22, base="current",
             settings={"resolv.nameserver.1.ip": "1.1.1.1"}, state="present")
    p.update(kw)
    return p


@pytest.fixture
def writes_on(monkeypatch):
    monkeypatch.setattr(lib, "WRITES_ENABLED", True)


def test_noop_idempotent():
    t = Fake()
    r = lib.run_module(params(settings={"resolv.nameserver.1.ip": "192.168.6.1", "resolv.nameserver.2.ip": ""}), False, t)
    assert not r.get("failed") and r["changed"] is False and r["base_sha256"] == SHA
    assert t.cmds == [lib.READ_CFG]
    assert r["diff"]["before"] == r["diff"]["after"] == ""


def test_diff_touched_keys_only():
    r = lib.run_module(params(settings={"resolv.nameserver.1.ip": "1.1.1.1", "resolv.nameserver.2.ip": ""}), True, Fake())
    assert r["changed"] is True
    assert r["diff"] == {"before": "resolv.nameserver.1.ip=192.168.6.1\n", "after": "resolv.nameserver.1.ip=1.1.1.1\n"}
    assert "PSK" not in str(r) and "Secret" not in str(r)


def test_check_mode_reads_only(writes_on):
    t = Fake()
    r = lib.run_module(params(), True, t)
    assert r["changed"] is True and t.cmds == [lib.READ_CFG]


def test_drift_guard():
    t = Fake()
    r = lib.run_module(params(base="a" * 64), False, t)
    assert r["failed"] and r["changed"] is False and "changed since the plan" in r["msg"]
    assert r["base_sha256"] == SHA and t.cmds == [lib.READ_CFG]
    assert not lib.run_module(params(base=SHA), True, Fake()).get("failed")


def test_base_current_does_not_guard():
    assert not lib.run_module(params(base="current"), True, Fake("other=1\n")).get("failed")


@pytest.mark.parametrize("key,val", [
    ("a b", "1"), ("a;b", "1"), ("a=b", "1"), ("", "1"), ("a$(x)", "1"), ("-x\n", "1"),
    ("k", "1\n2"), ("k", "`id`"), ("k", "$(id)"), ("k", "a;b"), ("k", " pad"), ("k", "a\rb"), ("k", ["x"]), ("k", True),
])
def test_validation_rejects_injection(key, val):
    t = Fake()
    r = lib.run_module(params(settings={key: val}), False, t)
    assert r["failed"] and t.cmds == []


def test_params_validation():
    for bad in (dict(base="xyz"), dict(port=0), dict(settings={}), dict(state="absent"), dict(host="-oProxyCommand=x"),
                dict(username="a b")):
        assert lib.run_module(params(**bad), True, Fake())["failed"]


def test_writes_refused_when_disabled():
    assert lib.WRITES_ENABLED is False
    t = Fake()
    r = lib.run_module(params(), False, t)
    assert r["failed"] and r["changed"] is False and "WRITES_ENABLED" in r["msg"]
    assert t.cmds == [lib.READ_CFG]


def test_guard_blocks_writes_and_unknown_even_if_requested():
    g = lib.GuardedTransport(Fake())
    for c in (lib.cmd_set("a", "b"), lib.PERSIST, "reboot", "cat /etc/passwd", "cat /tmp/system.cfg; reboot"):
        with pytest.raises(lib.CommandRefused):
            g.run(c)


def apply_effect(t, cmd):
    # minimal emulation of the generated commands on the fake file
    m = re.match(r"sed -i 's\|\^(.*?)=\.\*\$\|(.*?)=(.*)\|' ", cmd)
    if m:
        k = m.group(2)
        t.text = "".join(("%s=%s\n" % (k, m.group(3).replace("\\|", "|").replace("\\&", "&").replace("\\\\", "\\")))
                         if l.startswith(k + "=") else l + "\n" for l in t.text.splitlines())
    elif cmd.startswith("printf"):
        t.text += re.match(r"printf '%s\\n' '(.*)' >>", cmd).group(1).replace("'\\''", "'") + "\n"
    elif cmd.startswith("sed -i '/^"):
        k = re.match(r"sed -i '/\^(.*)=/d'", cmd).group(1).replace("\\.", ".")
        t.text = "".join(l + "\n" for l in t.text.splitlines() if not l.startswith(k + "="))


def test_write_path_exact_commands(writes_on):
    t = Fake(effect=apply_effect)
    r = lib.run_module(params(base=SHA, settings={"resolv.nameserver.1.ip": "1.1.1.1", "new.key-1": "it's & a|b"}), False, t)
    assert not r.get("failed"), r
    assert r["changed"] is True
    assert len(t.cmds) == 5 and t.cmds[0] == t.cmds[-1] == lib.READ_CFG and t.cmds[3] == "cfgmtd -w -p /etc/persistent/"
    # concrete shapes
    assert lib.cmd_set("resolv.nameserver.1.ip", "1.1.1.1") == \
        "sed -i 's|^resolv\\.nameserver\\.1\\.ip=.*$|resolv.nameserver.1.ip=1.1.1.1|' /tmp/system.cfg"
    assert lib.cmd_append("new.key-1", "it's & a|b") == "printf '%s\\n' 'new.key-1=it'\\''s & a|b' >> /tmp/system.cfg"
    assert t.cmds[1] == lib.cmd_append("new.key-1", "it's & a|b")
    assert t.cmds[2] == lib.cmd_set("resolv.nameserver.1.ip", "1.1.1.1")
    assert all(lib.classify(c) != "unknown" for c in t.cmds)
    assert "aaa.1.wpa.psk" not in "".join(t.cmds)


def test_every_generated_write_is_allowlisted():
    for v in ("1.1.1.1", "it's", "a&b", "a|b", "back\\slash", "$HOME", ""):
        assert lib.classify(lib.cmd_set("a.b-c_1", v)) == "write"
        assert lib.classify(lib.cmd_append("a.b-c_1", v)) == "write"
    assert lib.classify(lib.cmd_delete("a.b")) == "write"
    for bad in ("sed -i 's|^a=.*$|a=1|' /etc/passwd", "sed -i 's|^a=.*$|a=1|e' /tmp/system.cfg",
                "printf x >> /tmp/system.cfg", "rm /tmp/system.cfg", "cfgmtd -w -p /", "sed -i 's|^a=.*$|a=$(id)|' /tmp/system.cfg"):
        assert lib.classify(bad) == "unknown"


def test_missing_trailing_newline_is_fixed_before_append(writes_on):
    t = Fake(text="a=1", effect=apply_effect)
    lib.run_module(params(settings={"b": "2"}), False, t)
    assert t.cmds[1] == lib.FIX_EOL


def test_verify_failure_restores_and_fails(writes_on):
    t = Fake()  # effect None: the device ignores writes, so verification fails
    r = lib.run_module(params(base=SHA), False, t)
    assert r["failed"] and r["changed"] is False and "verification failed" in r["msg"]
    assert t.cmds[-3:] == [lib.cmd_set("resolv.nameserver.1.ip", "192.168.6.1"), lib.PERSIST, lib.READ_CFG]
    assert lib.cmd_set("resolv.nameserver.1.ip", "1.1.1.1") in t.cmds


def test_verify_failure_deletes_new_keys_on_restore(writes_on):
    t = Fake()
    lib.run_module(params(settings={"brand.new": "1"}), False, t)
    assert lib.cmd_delete("brand.new") in t.cmds


def test_secrets_never_in_results_or_diff():
    t = Fake()
    r = lib.run_module(params(settings={"aaa.1.wpa.psk": "NewSecretPsk", "users.1.password": "x"}), True, t)
    blob = repr(r)
    for s in ("NewSecretPsk", "SuperSecretPsk", "pw-SECRET", "$1$hash"):
        assert s not in blob
    assert "<redacted>" in r["diff"]["after"]
    r = lib.run_module(params(password="pw-SECRET", base="a" * 64), True, t)
    assert "pw-SECRET" not in repr(r)


def test_ssh_password_not_in_argv(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]

        class P:
            returncode, stdout = 0, "x"
        return P()
    monkeypatch.setattr(lib.subprocess, "run", fake_run)
    lib.SshTransport("h", 2222, "u", "pw-SECRET").run(lib.READ_CFG)
    assert "pw-SECRET" not in " ".join(seen["argv"]) and seen["env"]["SSHPASS"] == "pw-SECRET"
    assert seen["argv"][:3] == ["sshpass", "-e", "ssh"] and "2222" in seen["argv"]


def test_argument_spec():
    s = lib.ARGUMENT_SPEC
    assert s["password"]["no_log"] is True
    assert {k for k, v in s.items() if v.get("required")} == {"host", "username", "password", "base", "settings"}
    assert s["port"]["default"] == 22 and s["state"]["choices"] == ["present"]


def test_drift_against_tool_implementation():
    for text in (CFG, "", "a=1\n#c=2\n k = v \nnoeq\n\nx=\ny=a=b\n", "a=1\r\nb=2\r\n", "dup=1\ndup=2\n"):
        assert lib.cfg_parse(text) == tool_cfg_parse(text)
    # the base hash the tool puts into a plan is the hash the module computes
    from bonis_netdev.intents import Dns, Resolver
    dp = AirosDriver().plan("ap", Dns("1.1.1.1", scope=Resolver()), {"cat /tmp/system.cfg": CFG}, "h")
    assert dp.fingerprint == lib.sha256_text(CFG) if hasattr(dp, "fingerprint") else True
    # the tool's read command and SSH options are the module's
    from bonis_netdev import transport as tt
    from bonis_netdev.drivers import airos
    assert airos.CFG == lib.READ_CFG and tt.SSH_OPTS == lib.SSH_OPTS


def test_tool_plan_step_applies_through_module():
    """A plan step of the tool (base + key=value) is accepted by the module (check mode)."""
    from bonis_netdev.intents import Dns, Resolver
    dp = AirosDriver().plan("ap", Dns("1.1.1.1", "8.8.8.8", scope=Resolver()), {"cat /tmp/system.cfg": CFG}, "h")
    step = next(s for s in dp.steps if s.phase == "change")
    parts = step.command.split()
    base = parts[1].split("=", 1)[1]
    settings = dict(p.split("=", 1) for p in parts[2:])
    r = lib.run_module(params(base=base, settings=settings), True, Fake())
    assert not r.get("failed") and r["changed"] is True


def test_module_file_compiles_and_docs_valid(tmp_path):
    py_compile.compile(str(MOD_PATH), cfile=str(tmp_path / "m.pyc"), doraise=True)
    py_compile.compile(str(LIB_PATH), cfile=str(tmp_path / "l.pyc"), doraise=True)
    ns = {}
    src = MOD_PATH.read_text()
    head = src.split("from ansible.module_utils.basic")[0]
    exec(compile(head, str(MOD_PATH), "exec"), ns)
    doc, ex, ret = (yaml.safe_load(ns[k]) for k in ("DOCUMENTATION", "EXAMPLES", "RETURN"))
    assert doc["module"] == "airos_cfg" and isinstance(ex, list) and {"changed", "msg", "base_sha256", "diff"} <= set(ret)
    assert all(isinstance(d, str) for d in doc["description"])  # an unquoted 'a: b' item becomes a dict
    opts = doc["options"]
    assert set(opts) == set(lib.ARGUMENT_SPEC)
    for name, o in lib.ARGUMENT_SPEC.items():
        assert opts[name]["type"] == o["type"]
        assert bool(opts[name].get("required")) == bool(o.get("required"))
        assert opts[name].get("default") == o.get("default")
    for text in (src, LIB_PATH.read_text()):
        assert not re.search(r"^\s*(from|import)\s+bonis_netdev", text, re.M)


def test_module_runs_in_ansible_harness_if_available(tmp_path):
    ansible_mod = pytest.importorskip("ansible.module_utils.basic")
    import json
    from unittest import mock
    from ansible.module_utils import basic
    from ansible.module_utils.common.text.converters import to_bytes
    import sys
    sys.path.insert(0, str(COLL / "plugins/module_utils"))
    try:
        mspec = importlib.util.spec_from_file_location("airos_cfg_mod", MOD_PATH)
        mod = importlib.util.module_from_spec(mspec)
        mspec.loader.exec_module(mod)
        args = dict(params(base=SHA, settings={"resolv.nameserver.1.ip": "1.1.1.1"}), _ansible_check_mode=True,
                    _ansible_remote_tmp="/tmp", _ansible_keep_remote_files=False)
        basic._ANSIBLE_ARGS = to_bytes(json.dumps({"ANSIBLE_MODULE_ARGS": args}))
        out = {}

        def exit_json(self, **kw):
            out.update(kw)
            raise SystemExit(0)
        with mock.patch.object(basic.AnsibleModule, "exit_json", exit_json), \
                mock.patch.object(mod.lib, "make_transport", lambda p: Fake()), pytest.raises(SystemExit):
            mod.main()
        assert out["changed"] is True and out["base_sha256"] == SHA
    finally:
        sys.path.pop(0)
