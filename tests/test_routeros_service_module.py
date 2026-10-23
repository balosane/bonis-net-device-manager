import importlib.util
import json
import py_compile
import re
import subprocess
import shutil
import sys
from pathlib import Path

import pytest
import yaml

COLL = Path(__file__).resolve().parent.parent / "ansible/collections/ansible_collections/bonis/netdev"
LIB_PATH = COLL / "plugins/module_utils/routeros_service.py"
MOD_PATH = COLL / "plugins/modules/routeros_service.py"
AIROS_LIB_PATH = COLL / "plugins/module_utils/airos_cfg.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


lib = _load("routeros_service_lib", LIB_PATH)

# recorded outputs ----------------------------------------------------------------------------------
V6_ENABLED = (
    'Flags: X - disabled, I - invalid \n'
    ' 3   name="api" port=8728 address=192.168.238.0/24,10.1.2.3/32 certificate=none \n')
V6_DISABLED = (
    'Flags: X - disabled, I - invalid \n'
    ' 3 X name="api" port=8728 address="" \n')
V6_ADDR_ABSENT = (
    'Flags: X - disabled, I - invalid \n'
    ' 3 X name="api" port=8728 \n')
V7_ENABLED = (
    'Flags: X - disabled, I - invalid\n'
    ' 3    name="api" port=8728 address=192.168.238.0/24 certificate=none max-sessions=20 vrf=main\n')
V7_DISABLED_WRAPPED = (
    'Flags: X - disabled, I - invalid\n'
    ' 3 X  name="api" port=8728 address=192.168.238.0/24,\n'
    '       10.1.2.3/32 max-sessions=20\n'
    '       vrf=main\n')
V7_EMPTY = (
    'Flags: X - disabled, I - invalid\n'
    ' 3    name="api" port=8728 address="" certificate=none max-sessions=20 vrf=main\n')
V7_EMPTY_BARE = ' 3    name="api" port=8728 address= certificate=none\n'

GOOD = ["192.168.238.0/24"]


class Fake(lib.Transport):
    def __init__(self, reads=None, text=V7_DISABLED_WRAPPED, effect=None):
        self.reads = list(reads or [])
        self.text = text
        self.effect = effect
        self.cmds = []

    def run(self, command):
        self.cmds.append(command)
        if command == lib.READ_CMD:
            return self.reads.pop(0) if self.reads else self.text
        if self.effect:
            self.effect(self, command)
        return ""


def ros_text(disabled, addrs):
    return ' 3 %sname="api" port=8728 address=%s\n' % ("X " if disabled else "  ", ",".join(addrs) or '""')


def device_effect(t, cmd):
    m = re.match(r'/ip service set \[find name=api\] disabled=(yes|no) address=(.*)$', cmd)
    addrs = [] if m.group(2) == '""' else m.group(2).split(",")
    t.text = ros_text(m.group(1) == "yes", addrs)


def params(**kw):
    p = dict(host="10.0.0.1", username="u", password="pw-SECRET", port=22, service="api", disabled=False,
             address=list(GOOD), from_disabled=None, from_address=None)
    p.update(kw)
    return p


@pytest.fixture
def writes_on(monkeypatch):
    monkeypatch.setattr(lib, "WRITES_ENABLED", True)


# parsing ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,dis,addr", [
    (V6_ENABLED, False, ["192.168.238.0/24", "10.1.2.3/32"]),
    (V6_DISABLED, True, []),
    (V6_ADDR_ABSENT, True, []),
    (V7_ENABLED, False, ["192.168.238.0/24"]),
    (V7_DISABLED_WRAPPED, True, ["192.168.238.0/24", "10.1.2.3/32"]),
    (V7_EMPTY, False, []),
    (V7_EMPTY_BARE, False, []),
    ("name: api\ndisabled: yes\naddress: 1.2.3.4/32\n", True, ["1.2.3.4/32"]),
    (' 0    name="ssh" port=22\n 3 X name="api" port=8728 address=\n 4  name="api-ssl"\n', True, []),
])
def test_parse(text, dis, addr):
    assert lib.parse_service(text) == {"disabled": dis, "address": addr}


@pytest.mark.parametrize("text,frag", [
    ("", "no row"),
    (' 0    name="ssh" port=22\n', "no row"),
    (V6_ENABLED + V6_ENABLED.split("\n", 1)[1], "ambiguous"),
    (' 3 XI name="api" port=8728\n', "invalid"),
    ("bad command name\n", "no row"),
])
def test_parse_failures(text, frag):
    with pytest.raises(lib.ServiceError, match=frag):
        lib.parse_service(text)


# behaviour ----------------------------------------------------------------------------------------
def test_noop_idempotent():
    t = Fake(text=V7_ENABLED)
    r = lib.run_module(params(), False, t)
    assert not r.get("failed") and r["changed"] is False
    assert t.cmds == [lib.READ_CMD]
    assert r["diff"]["before"] == r["diff"]["after"] and r["current"] == {"disabled": False, "address": GOOD}


def test_noop_address_normalisation():
    t = Fake(text=ros_text(False, ["10.1.2.3/32"]))
    r = lib.run_module(params(address=["10.1.2.3"]), False, t)
    assert r["changed"] is False and t.cmds == [lib.READ_CMD]


def test_diff_and_check_mode_never_writes(writes_on):
    for text in (V6_DISABLED, V7_EMPTY, V6_ADDR_ABSENT):
        t = Fake(text=text)
        r = lib.run_module(params(), True, t)
        assert r["changed"] is True and not r.get("failed") and t.cmds == [lib.READ_CMD]
        assert set(r["diff"]["before"]) == set(r["diff"]["after"]) == {"disabled", "address"}
        assert r["diff"]["after"] == {"disabled": False, "address": GOOD}
        assert r["current"]["address"] == []


def test_disable_keeps_current_address_when_omitted(writes_on):
    t = Fake(text=V7_ENABLED, effect=device_effect)
    r = lib.run_module(params(disabled=True, address=None), False, t)
    assert r["changed"] is True
    assert t.cmds[1] == "/ip service set [find name=api] disabled=yes address=192.168.238.0/24"


def test_disable_with_address_given(writes_on):
    t = Fake(text=V7_ENABLED, effect=device_effect)
    r = lib.run_module(params(disabled=True, address=["10.9.9.0/24"]), False, t)
    assert r["changed"] is True and r["diff"]["after"] == {"disabled": True, "address": ["10.9.9.0/24"]}


def test_drift_guard():
    for kw in (dict(from_disabled=True), dict(from_address=[]), dict(from_address=["1.1.1.1/32"]),
               dict(from_disabled=True, from_address=[])):
        t = Fake(text=V7_ENABLED)
        r = lib.run_module(params(**kw), False, t)
        assert r["failed"] and r["changed"] is False and "changed since the plan" in r["msg"]
        assert t.cmds == [lib.READ_CMD]
    ok = lib.run_module(params(from_disabled=True, from_address=[]), True, Fake(text=V6_DISABLED))
    assert not ok.get("failed") and ok["changed"] is True
    ok = lib.run_module(params(from_address=["10.1.2.3", "192.168.238.0/24"]), True, Fake(text=V6_ENABLED))
    assert not ok.get("failed")


def test_drift_guard_empty_matches_absent_address():
    r = lib.run_module(params(from_address=[], from_disabled=True), True, Fake(text=V6_ADDR_ABSENT))
    assert not r.get("failed")


# validation ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("addr", ["10.0.0.1", "10.0.0.1/32", "192.168.1.0/24", "172.16.5.128/25", "8.8.8.8/32",
                                  "212.119.38.0/24", "10.0.0.0/30"])
def test_valid_addresses(addr):
    assert lib.validate_address(addr) == addr


@pytest.mark.parametrize("addr", [
    "0.0.0.0/0", "0.0.0.0", "0.0.0.0/32", "10.0.0.0/8", "10.0.0.0/23", "192.168.0.0/16", "1.2.3.4/0",
    "127.0.0.1", "127.0.0.0/24", "224.0.0.1", "239.1.1.0/24", "255.255.255.255", "255.255.255.255/32",
    "240.0.0.1", "250.1.1.0/24", "0.1.2.3", "010.0.0.1", "10.00.0.1", "10.0.0.01/24", "10.0.0.1/024", "10.0.0.1/33",
    "10.0.0.256", "1.2.3", "::1", "2001:db8::1", "10.0.0.1 ", " 10.0.0.1", "a.b.c.d", "10.0.0.1,10.0.0.2", "",
    "10.0.0.1;reboot", "10.0.0.1\n", "-1.2.3.4",
])
def test_invalid_addresses(addr):
    with pytest.raises(lib.ServiceError):
        lib.validate_address(addr)


def test_address_list_rules():
    with pytest.raises(lib.ServiceError, match="duplicate"):
        lib.validate_addresses(["10.0.0.1", "10.0.0.1"])
    with pytest.raises(lib.ServiceError, match="duplicate"):
        lib.validate_addresses(["10.0.0.1", "10.0.0.1/32"])
    with pytest.raises(lib.ServiceError, match="at most 8"):
        lib.validate_addresses(["10.0.0.%d" % i for i in range(1, 10)])
    assert len(lib.validate_addresses(["10.0.0.%d" % i for i in range(1, 9)])) == 8


def test_run_validation_gates_before_wire():
    for bad in (dict(address=[]), dict(address=None), dict(address=["0.0.0.0/0"]), dict(address=["10.0.0.0/8"]),
                dict(port=0), dict(host="-oProxyCommand=x"), dict(username="a b"), dict(service="api-ssl"),
                dict(disabled="yes"), dict(from_address="x")):
        t = Fake()
        r = lib.run_module(params(**bad), True, t)
        assert r["failed"] and t.cmds == [], bad
    # disabled=true: address optional but validated when given
    assert not lib.run_module(params(disabled=True, address=None), True, Fake()).get("failed")
    assert lib.run_module(params(disabled=True, address=["0.0.0.0/0"]), True, Fake())["failed"]


# writes -------------------------------------------------------------------------------------------
def test_writes_refused_when_disabled():
    assert lib.WRITES_ENABLED is False
    t = Fake(text=V6_DISABLED)
    r = lib.run_module(params(), False, t)
    assert r["failed"] and r["changed"] is False and "WRITES_ENABLED" in r["msg"]
    assert t.cmds == [lib.READ_CMD]


def test_guard_blocks_writes_when_disabled():
    g = lib.GuardedTransport(Fake())
    with pytest.raises(lib.CommandRefused):
        g.run(lib.cmd_set(False, GOOD))


def test_write_path_exact_commands(writes_on):
    t = Fake(text=V6_DISABLED, effect=device_effect)
    r = lib.run_module(params(address=["192.168.238.0/24", "10.1.2.3"], from_disabled=True, from_address=[]), False, t)
    assert not r.get("failed"), r
    assert r["changed"] is True
    assert t.cmds == [lib.READ_CMD,
                      "/ip service set [find name=api] disabled=no address=192.168.238.0/24,10.1.2.3",
                      lib.READ_CMD]
    assert r["current"] == {"disabled": False, "address": ["192.168.238.0/24", "10.1.2.3"]}


@pytest.mark.parametrize("start,expect_back", [
    (V6_ENABLED, "/ip service set [find name=api] disabled=no address=192.168.238.0/24,10.1.2.3/32"),
    (V6_DISABLED, '/ip service set [find name=api] disabled=yes address=""'),
    (V6_ADDR_ABSENT, '/ip service set [find name=api] disabled=yes address=""'),
])
def test_verify_failure_restores(writes_on, start, expect_back):
    state = {"n": 0}

    def effect(t, cmd):  # first write is ignored by the device, the restore works
        state["n"] += 1
        if state["n"] > 1:
            device_effect(t, cmd)
    t = Fake(text=start, effect=effect)
    r = lib.run_module(params(address=["10.9.9.0/24"], disabled=False), False, t)
    assert r["failed"] and r["changed"] is False and "verification failed" in r["msg"] and "restored" in r["msg"]
    assert t.cmds[1] == "/ip service set [find name=api] disabled=no address=10.9.9.0/24"
    assert t.cmds[2] == lib.READ_CMD and t.cmds[3] == expect_back and t.cmds[-1] == lib.READ_CMD


def test_restore_failure_reported(writes_on):
    t = Fake(text=V6_DISABLED, effect=lambda t, c: setattr(t, "text", ros_text(False, ["9.9.9.9"])))  # device garbles
    r = lib.run_module(params(), False, t)
    assert r["failed"] and r["changed"] is False and "RESTORE ALSO FAILED" in r["msg"]


def test_unrestorable_previous_state_refuses_before_writing(writes_on):
    t = Fake(text=' 3 name="api" port=8728 address=router.example.com\n')
    r = lib.run_module(params(), False, t)
    assert r["failed"] and "nothing was written" in r["msg"] and t.cmds == [lib.READ_CMD]


# classifier / injection ---------------------------------------------------------------------------
def test_classifier():
    assert lib.classify(lib.READ_CMD) == "read"
    assert lib.classify(lib.cmd_set(False, GOOD)) == "write"
    assert lib.classify(lib.cmd_set(True, [])) == "write"
    assert lib.classify(lib.cmd_set(True, ["1.2.3.4", "5.6.7.8/32"])) == "write"
    base = lib.cmd_set(False, GOOD)
    bad = [
        base + ";/system reboot", base + "\n/system reboot", base + "\r", base + "`id`", base + " $(id)", base + "{{x}}",
        base + " ", base + "\n", lib.READ_CMD + "\n", lib.READ_CMD + " ", lib.READ_CMD + ";/system reboot",
        "/ip service set [find name=ssh] disabled=yes address=1.2.3.4",
        "/ip service set [find name=api-ssl] disabled=no address=1.2.3.4",
        "/ip service set [find name=api] disabled=no address=1.2.3.4 port=22",
        "/ip service set [find name=api] disabled=no",
        "/ip service set [find name=api] disabled=maybe address=1.2.3.4",
        "/ip service set [find name=api] disabled=no address=1.2.3.4,",
        "/ip service set [find name=api] disabled=no address=999.1.1.1",
        "/ip service set [find name=api] disabled=no address=1.2.3.4/33",
        "/ip service set [find name=api] disabled=no address=router",
        "/ip service set [find name=api] port=22",
        "/ip service set [find] disabled=yes address=1.2.3.4",
        "/ip service print detail without-paging where name=ssh",
        "/ip service print", "/system reboot", "/export", "/user print", "", None,
    ]
    for c in bad:
        assert lib.classify(c) == "unknown", c
    g = lib.GuardedTransport(Fake())
    for c in bad:
        with pytest.raises(lib.CommandRefused):
            g.run(c)


def test_guard_blocks_unknown_even_with_writes_on(writes_on):
    t = Fake()
    g = lib.GuardedTransport(t)
    with pytest.raises(lib.CommandRefused):
        g.run("/system reboot")
    assert t.cmds == []


def test_injection_attempts_via_params_rejected(writes_on):
    for addr in ("10.0.0.1;/system reboot", "10.0.0.1\n/system reboot", "10.0.0.1`id`", "$(id)", "{{ x }}", "10.0.0.1 address=2"):
        t = Fake(text=V6_DISABLED)
        r = lib.run_module(params(address=[addr]), False, t)
        assert r["failed"] and t.cmds == []


# secrets / spec -----------------------------------------------------------------------------------
def test_secrets_never_in_results():
    for kw in (dict(), dict(address=["bad"]), dict(from_disabled=True)):
        for check in (True, False):
            r = lib.run_module(params(**kw), check, Fake(text=V7_ENABLED))
            assert "pw-SECRET" not in repr(r)


def test_ssh_password_not_in_argv(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]

        class P:
            returncode, stdout = 0, "x"
        return P()
    monkeypatch.setattr(lib.subprocess, "run", fake_run)
    lib.SshTransport("h", 2222, "u", "pw-SECRET").run(lib.READ_CMD)
    assert "pw-SECRET" not in " ".join(seen["argv"]) and seen["env"]["SSHPASS"] == "pw-SECRET"
    assert seen["argv"][:3] == ["sshpass", "-e", "ssh"] and "2222" in seen["argv"]
    assert seen["argv"][-1] == lib.READ_CMD


def test_argument_spec():
    s = lib.ARGUMENT_SPEC
    assert s["password"]["no_log"] is True
    assert {k for k, v in s.items() if v.get("required")} == {"host", "username", "password", "disabled"}
    assert s["port"]["default"] == 22 and s["service"]["choices"] == ["api"]
    assert s["address"]["type"] == s["from_address"]["type"] == "list"
    assert s["from_disabled"]["type"] == "bool"


def test_ssh_opts_match_airos_module():
    airos = _load("airos_cfg_lib_for_drift", AIROS_LIB_PATH)
    assert lib.SSH_OPTS == airos.SSH_OPTS


def test_module_file_compiles_and_docs_valid(tmp_path):
    py_compile.compile(str(MOD_PATH), cfile=str(tmp_path / "m.pyc"), doraise=True)
    py_compile.compile(str(LIB_PATH), cfile=str(tmp_path / "l.pyc"), doraise=True)
    ns = {}
    src = MOD_PATH.read_text()
    exec(compile(src.split("from ansible.module_utils.basic")[0], str(MOD_PATH), "exec"), ns)
    doc, ex, ret = (yaml.safe_load(ns[k]) for k in ("DOCUMENTATION", "EXAMPLES", "RETURN"))
    assert doc["module"] == "routeros_service" and isinstance(ex, list) and {"changed", "msg", "diff", "current"} <= set(ret)
    assert all(isinstance(d, str) for d in doc["description"])
    opts = doc["options"]
    assert set(opts) == set(lib.ARGUMENT_SPEC)
    for name, o in lib.ARGUMENT_SPEC.items():
        assert opts[name]["type"] == o["type"]
        assert bool(opts[name].get("required")) == bool(o.get("required"))
        assert opts[name].get("default") == o.get("default")
        assert opts[name].get("elements") == o.get("elements")
    for text in (src, LIB_PATH.read_text()):
        assert not re.search(r"^\s*(from|import)\s+bonis_netdev", text, re.M)


def test_ansible_doc_renders(tmp_path):
    exe = shutil.which("ansible-doc")
    venv = __import__("os").environ.get("NETDEV_ANSIBLE_VENV")
    if venv and (Path(venv) / "ansible-doc").exists():
        exe = str(Path(venv) / "ansible-doc")
    if not exe:
        pytest.skip("ansible-doc not available")
    root = COLL.parent.parent.parent  # .../ansible/collections
    env = dict(__import__("os").environ, ANSIBLE_COLLECTIONS_PATH=str(root), HOME=str(tmp_path))
    p = subprocess.run([exe, "-t", "module", "bonis.netdev.routeros_service"], env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert "routeros_service" in p.stdout and "from_address" in p.stdout


def test_module_runs_in_ansible_harness_if_available():
    pytest.importorskip("ansible.module_utils.basic")
    from unittest import mock
    from ansible.module_utils import basic
    from ansible.module_utils.common.text.converters import to_bytes
    sys.path.insert(0, str(COLL / "plugins/module_utils"))
    try:
        mod = _load("routeros_service_mod", MOD_PATH)
        args = dict(host="10.0.0.1", username="u", password="pw-SECRET", disabled=False, address=GOOD,
                    from_disabled=True, from_address=[],
                    _ansible_check_mode=True, _ansible_remote_tmp="/tmp", _ansible_keep_remote_files=False)
        basic._ANSIBLE_ARGS = to_bytes(json.dumps({"ANSIBLE_MODULE_ARGS": args}))
        out = {}

        def exit_json(self, **kw):
            out.update(kw)
            raise SystemExit(0)
        with mock.patch.object(basic.AnsibleModule, "exit_json", exit_json), \
                mock.patch.object(mod.lib, "make_transport", lambda p: Fake(text=V6_DISABLED)), pytest.raises(SystemExit):
            mod.main()
        assert out["changed"] is True and out["current"] == {"disabled": True, "address": []}
        assert out["diff"]["after"] == {"disabled": False, "address": GOOD}
    finally:
        sys.path.pop(0)
