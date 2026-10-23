"""mgmt-api end to end (BONISISP-47): intent, classify, RouterOS driver, renderer (SSH module gate), templates, CLI.
All device output is recorded/canned; nothing here touches a device."""
import copy
import json
import os
import shutil
import subprocess

import pytest
import yaml

from bonis_netdev import cli, samples
from bonis_netdev.classify import Effect, effect_of
from bonis_netdev.drivers.airos import AirosDriver
from bonis_netdev.drivers.base import build_device_plan
from bonis_netdev.drivers.routeros import RouterOsDriver
from bonis_netdev.intents import IntentError, MgmtApi
from bonis_netdev.plan import Plan, Step
from bonis_netdev.render import RenderError, plan_from_dict, render_playbook, verify_playbook
from bonis_netdev.templates import NA, OK, compile_config, load_config, mgmt_api_intents

HERE = os.path.dirname(__file__)
GOLDEN = os.path.join(HERE, "golden", "mgmt-api.playbook.yml")
SEED = os.path.join(HERE, "..", "config")
J = "192.168.238.111/32"
MOD = "bonis.netdev.routeros_service"
SET = "/ip service set [find name=api]"
READ = "/ip service print detail without-paging where name=api"


def plan_of(driver, intent, transport, name="D1"):
    return build_device_plan(driver, name, intent, transport)


def ros(major, text):
    return RouterOsDriver(major), samples.ros_service_transport(text, resource=samples.RESOURCE if major == 6 else samples.RESOURCE_7)


def planned(major, text, intent=None, name="D1"):
    drv, tr = ros(major, text)
    return plan_of(drv, intent or MgmtApi((J,)), tr, name)


# ------------------------------------------------------------------ intent
def test_intent_valid_and_as_dict():
    i = MgmtApi((J, "10.0.0.5", "10.20.30.0/24"), reason="r")
    assert i.as_dict() == {"intent": "mgmt-api", "allowed": [J, "10.0.0.5", "10.20.30.0/24"], "reason": "r"}
    assert i.networks == (J, "10.0.0.5/32", "10.20.30.0/24")
    assert MgmtApi([J]).allowed == (J,)
    assert len(MgmtApi(tuple(f"10.0.0.{n}" for n in range(1, 9))).allowed) == 8


@pytest.mark.parametrize("bad,msg", [
    ((), "at least one"),
    (tuple(f"10.0.0.{n}" for n in range(1, 10)), "at most 8"),
    (("0.0.0.0/0",), "wider than /24"),
    (("10.0.0.0/23",), "wider than /24"),
    (("10.0.0.0/8",), "wider than /24"),
    (("127.0.0.1",), "loopback"),
    (("127.0.0.0/24",), "loopback"),
    (("224.0.0.1",), "multicast"),
    (("0.0.0.0",), "unspecified"),
    (("0.0.0.0/32",), "unspecified"),
    (("255.255.255.255",), "reserved"),
    (("240.0.0.1",), "reserved"),
    (("10.0.0.1", "10.0.0.1"), "duplicate"),
    (("10.0.0.1", "10.0.0.1/32"), "duplicate"),
    (("10.0.0.1/24",), "valid IPv4 address or network"),       # host bits set
    (("10.0.0.0/024",), "canonical"),
    (("010.0.0.1",), "not"),
    (("10.0.0.1\n",), "not an IPv4"),
    (("10.0.0.1;reboot",), "not an IPv4"),
    (("10.0.0.1,10.0.0.2",), "not an IPv4"),
    (("{{ x }}",), "not an IPv4"),
    (("2001:db8::1",), "not an IPv4"),
    (("host.example",), "not an IPv4"),
    (("10.0.0.1/33",), "valid"),
    ((5,), "not an IPv4"),
    ("10.0.0.1", "expected a list"),
    (None, "expected a list"),
])
def test_intent_rejects(bad, msg):
    with pytest.raises(IntentError, match=msg):
        MgmtApi(bad)


# ------------------------------------------------------------------ classify
@pytest.mark.parametrize("cmd,eff", [
    (READ, Effect.READ),
    (f"{SET} disabled=no address=192.168.238.111/32", Effect.UPDATE),
    (f"{SET} disabled=no address=192.168.238.111", Effect.UPDATE),
    (f"{SET} disabled=yes address=10.0.0.1/32,10.20.30.0/24,192.168.1.1", Effect.UPDATE),
    (f"{SET} disabled=yes address=0.0.0.0/0", Effect.UPDATE),        # a previous value may be anything of this shape
    (f'{SET} disabled=yes address=""', Effect.UPDATE),
    (f'{SET} disabled=no address=""', Effect.UPDATE),
])
def test_classify_positive(cmd, eff):
    assert effect_of(cmd) == eff


INJECT = [";/system reboot", "\n/system reboot", " `reboot`", "$(reboot)", "{{ x }}", ",a b", " extra=1", "|x", "&x", "'x",
          ",", ",,10.0.0.1", " port=1", "x", "\n", "/33"]


@pytest.mark.parametrize("tail", INJECT)
def test_classify_injection_suffixes(tail):
    for base in (f"{SET} disabled=no address=10.0.0.1/32", f'{SET} disabled=yes address=""'):
        assert effect_of(base + tail) != Effect.UPDATE or tail == "\n", base + tail
    # a trailing newline is stripped by effect_of; an embedded one is refused
    assert effect_of(f"{SET} disabled=no address=10.0.0.1/32\n/system reboot") == Effect.UNKNOWN


@pytest.mark.parametrize("cmd", [
    "/ip service set [find name=ssh] disabled=no address=10.0.0.1/32",
    "/ip service set [find name=api-ssl] disabled=no address=10.0.0.1/32",
    "/ip service set [find] disabled=no address=10.0.0.1/32",
    "/ip service set api disabled=no address=10.0.0.1/32",
    "/ip service set [find name=api] disabled=maybe address=10.0.0.1/32",
    "/ip service set [find name=api] address=10.0.0.1/32",
    "/ip service set [find name=api] disabled=no",
    "/ip service set [find name=api] disabled=no address=",
    "/ip service set [find name=api] disabled=no address=10.0.0.1/33",
    "/ip service set [find name=api] disabled=no address=300.0.0.1",
    "/ip service set [find name=api] disabled=no address=10.0.0.1 port=22",
    "/ip service set [find name=api] disabled=no port=22 address=10.0.0.1",
    "/ip service set [find name=api] disabled=no address=2001:db8::1",
    "/ip service set [find name=api] disabled=no address=host.example",
    "/ip service set [find name=api] disabled=no address=''",
    '/ip service set [find name=api] disabled=no address="10.0.0.1"',
    "/ip service enable api",
    "/ip service disable [find]",
])
def test_classify_negative(cmd):
    assert effect_of(cmd) == Effect.UNKNOWN


def test_step_phase_rules():
    with pytest.raises(ValueError):
        Step("verify", f"{SET} disabled=no address=10.0.0.1", "x")
    with pytest.raises(ValueError):
        Step("change", READ, "x")
    with pytest.raises(ValueError):
        Step("change", f"{SET} disabled=no address=10.0.0.1", "x", rollback=READ)
    Step("change", f"{SET} disabled=no address=10.0.0.1", "x", rollback=f'{SET} disabled=yes address=""')


# ------------------------------------------------------------------ driver
@pytest.mark.parametrize("major,text", [(6, samples.SERVICE_API_6_OFF), (7, samples.SERVICE_API_7_OFF)])
def test_disabled_gets_enabled(major, text):
    dp = planned(major, text)
    assert [s.phase for s in dp.steps] == ["precheck", "change", "verify"] and not dp.unsupported
    pre, ch, ver = dp.steps
    assert pre.command == READ and pre.expect == "disabled=yes address="
    assert ch.command == f"{SET} disabled=no address={J}"
    assert ch.rollback == f'{SET} disabled=yes address=""'
    assert ver.command == READ and ver.expect == f"disabled=no address={J}"
    assert dp.notes == []          # it was disabled, not open


def test_enabled_other_address_is_narrowed():
    dp = planned(7, samples.SERVICE_API_7_ON_OTHER)
    ch = dp.steps[1]
    assert ch.command == f"{SET} disabled=no address={J}"
    assert ch.rollback == f"{SET} disabled=no address=10.1.1.0/24"
    assert dp.steps[0].expect == "disabled=no address=10.1.1.0/24"


def test_unrestricted_enabled_is_narrowed_with_note():
    dp = planned(6, samples.SERVICE_API_6_ON_OPEN)
    assert dp.steps[1].rollback == f'{SET} disabled=no address=""'
    assert any("open to everyone" in n and "narrows" in n for n in dp.notes)


@pytest.mark.parametrize("major,text", [(6, samples.SERVICE_API_6_ON_RESTRICTED), (7, samples.SERVICE_API_7_ON_RESTRICTED)])
def test_unchanged(major, text):
    intent = MgmtApi((J, "10.20.30.0/24")) if major == 6 else MgmtApi((J,))
    dp = planned(major, text, intent)
    assert dp.steps == [] and len(dp.unchanged) == 1


def test_compare_is_order_insensitive_and_keeps_intent_order_when_writing():
    assert planned(6, samples.SERVICE_API_6_ON_RESTRICTED, MgmtApi(("10.20.30.0/24", J))).steps == []
    dp = planned(6, samples.SERVICE_API_6_ON_RESTRICTED, MgmtApi(("10.20.30.0/24", J, "10.9.9.9")))
    assert dp.steps[1].command == f"{SET} disabled=no address=10.20.30.0/24,{J},10.9.9.9/32"
    assert dp.steps[1].rollback == f"{SET} disabled=no address={J},10.20.30.0/24"


def test_bare_address_equals_slash_32_on_the_device():
    assert planned(7, samples.SERVICE_API_7_ON_RESTRICTED, MgmtApi(("192.168.238.111",))).steps == []


def test_disabled_with_the_right_address_is_still_enabled():
    text = samples.SERVICE_API_7_OFF.replace('address=""', f"address={J}")
    dp = planned(7, text)
    assert dp.changes == 1 and dp.steps[1].rollback == f"{SET} disabled=yes address={J}"


@pytest.mark.parametrize("bad", ["fe80::1", "host.example", "10.0.0.1;reboot", "10.0.0.1/33", "10.0.0.256", "10.0.0.1 x"])
def test_bad_previous_address_is_unsupported(bad):
    dp = planned(7, f'Flags: X - disabled, I - invalid \n 5    name="api" port=8728 address="{bad}" vrf=main\n')
    assert dp.steps == [] and len(dp.unsupported) == 1 and "rollback" in dp.unsupported[0]


def test_previous_wide_address_is_restored_as_is():
    dp = planned(7, samples.SERVICE_API_7_ON_OTHER.replace("10.1.1.0/24", "10.0.0.0/8,0.0.0.0/0"))
    assert dp.steps[1].rollback == f"{SET} disabled=no address=10.0.0.0/8,0.0.0.0/0"


def test_missing_service_row_is_unsupported():
    dp = planned(7, "Flags: X - disabled, I - invalid \n")
    assert dp.steps == [] and "cannot find the service" in dp.unsupported[0]
    dp = planned(7, ' 6    name="www" port=80 address=""\n')
    assert dp.steps == [] and dp.unsupported


def test_only_the_api_service_is_ever_touched():
    for text in (samples.SERVICE_API_6_OFF, samples.SERVICE_API_7_ON_OTHER, samples.SERVICE_API_6_ON_OPEN):
        for s in planned(7, text).steps:
            for cmd in ((s.command, s.rollback) if s.phase == "change" else ()):
                assert cmd.startswith("/ip service set [find name=api] disabled=")


def test_only_one_read_is_made_and_nothing_else():
    drv, tr = ros(7, samples.SERVICE_API_7_OFF)
    plan_of(drv, MgmtApi((J,)), tr)
    assert tr.asked == [READ]


def test_airos_is_unsupported_by_the_driver():
    dp = plan_of(AirosDriver(), MgmtApi((J,)), samples.airos_cfg_transport(samples.SYSTEM_CFG_XW))
    assert dp.steps == [] and "not supported by airos" in dp.unsupported[0]
    assert "mgmt-api" not in AirosDriver.implements and "mgmt-api" in RouterOsDriver.implements


# ------------------------------------------------------------------ renderer
def api_plan() -> Plan:
    intent = MgmtApi((J,), reason="demo")
    devs = [planned(6, samples.SERVICE_API_6_OFF, intent, "R6-off"),
            planned(7, samples.SERVICE_API_7_ON_OTHER, intent, "R7-other"),
            planned(6, samples.SERVICE_API_6_ON_OPEN, intent, "R6-open"),
            planned(7, samples.SERVICE_API_7_ON_RESTRICTED, intent, "R7-same")]
    return Plan(intent.as_dict(), devs)


def doc_of(plan):
    return json.loads(plan.to_json("2026-10-23T00:00:00+00:00"))


def devblock(play, name):
    return next(t for t in play["tasks"] if t.get("tags") == ["dev_" + name.replace("-", "_")])["block"]


def all_leaves(tasks):
    for t in tasks:
        if "block" in t:
            yield from all_leaves(t["block"])
            yield from all_leaves(t.get("rescue", []))
        else:
            yield t


def test_render_structure_gate_outside_rescue():
    play = yaml.safe_load(render_playbook(api_plan(), check_only=False))[0]
    body = devblock(play, "R6-off")
    gate, group = body                                # no api_info read of the (possibly disabled) API
    assert MOD in gate and gate["check_mode"] is True and "block" not in gate and "rescue" not in gate
    g = gate[MOD]
    assert g == {"host": "10.0.0.23", "username": "{{ netdev_user }}", "password": "{{ netdev_pass }}", "service": "api",
                 "disabled": False, "address": [J], "from_disabled": True, "from_address": []}
    assert "port" not in g
    change, verify, assert_, = group["block"]
    assert change[MOD] == g and change["check_mode"] == "{{ netdev_check_only | bool }}"
    assert verify[MOD] == {**g, "from_disabled": False, "from_address": [J]}
    assert verify["check_mode"] is True and verify["when"] == "not (netdev_check_only | bool)" and verify["register"]
    assert assert_["ansible.builtin.assert"]["that"] == [f"not {verify['register']}.changed"]
    assert assert_["when"] == "not (netdev_check_only | bool)"
    rollback, stop = group["rescue"]
    assert rollback[MOD] == {**g, "disabled": True, "address": [], "from_disabled": False, "from_address": [J],
                             "restore": True}
    assert rollback["check_mode"] == "{{ netdev_check_only | bool }}"
    assert "ansible.builtin.assert" in stop and stop["ansible.builtin.assert"]["that"] == ["false"]


def test_render_previous_values_flow_into_from_guards():
    play = yaml.safe_load(render_playbook(api_plan(), check_only=False))[0]
    g = devblock(play, "R7-other")[0][MOD]
    assert (g["from_disabled"], g["from_address"], g["address"]) == (False, ["10.1.1.0/24"], [J])
    g = devblock(play, "R6-open")[0][MOD]
    assert (g["from_disabled"], g["from_address"]) == (False, [])
    back = devblock(play, "R6-open")[1]["rescue"][0][MOD]
    assert (back["disabled"], back["address"], back["from_address"]) == (False, [], [J])


def test_unchanged_device_renders_nothing():
    assert "R7-same" not in render_playbook(api_plan())


def test_check_only_everything_is_check_mode_and_no_api_info():
    text = render_playbook(api_plan())
    for t in all_leaves(yaml.safe_load(text)[0]["tasks"]):
        assert t["check_mode"] is True
    assert "api_info" not in text and "api_modify" not in text


def test_gate_and_verify_literal_check_mode_in_apply_mode():
    play = yaml.safe_load(render_playbook(api_plan(), check_only=False))[0]
    leaves_ = [t for t in all_leaves(play["tasks"]) if MOD in t]
    assert len(leaves_) == 3 * 4          # gate, change, verify, rollback for each of the three changed devices
    for t in leaves_:
        label = t["name"]
        if "gate" in label or t.get("when"):
            assert t["check_mode"] is True
        else:
            assert t["check_mode"] == "{{ netdev_check_only | bool }}"


@pytest.mark.parametrize("check_only", [True, False])
def test_roundtrip_and_determinism(check_only):
    text = render_playbook(api_plan(), check_only=check_only)
    assert text == render_playbook(api_plan(), check_only=check_only)
    assert verify_playbook(doc_of(api_plan()), text) == []
    assert verify_playbook(doc_of(api_plan()), text, require_check_only=check_only) == []


def test_plan_json_roundtrip_through_plan_from_dict():
    doc = doc_of(api_plan())
    assert render_playbook(plan_from_dict(doc)) == render_playbook(api_plan())


def edited(plan, fn, **kw):
    play = yaml.safe_load(render_playbook(plan, **kw))
    fn(play[0])
    return yaml.safe_dump(play, sort_keys=False)


def first_gate(play):
    return devblock(play, "R6-off")[0]


def test_verify_rejects_tampered_address_list():
    p = api_plan()
    for fn in (lambda pl: first_gate(pl)[MOD].update(address=[J, "10.0.0.0/24"]),
               lambda pl: first_gate(pl)[MOD].update(address=[]),
               lambda pl: devblock(pl, "R6-off")[1]["block"][0][MOD].update(address=["0.0.0.0/0"]),
               lambda pl: devblock(pl, "R6-off")[1]["rescue"][0][MOD].update(address=["0.0.0.0/0"])):
        assert verify_playbook(doc_of(p), edited(p, fn))


def test_verify_rejects_removed_gate_swapped_module_and_other_tampering():
    p = api_plan()
    assert verify_playbook(doc_of(p), edited(p, lambda pl: devblock(pl, "R6-off").pop(0)))
    assert verify_playbook(doc_of(p), edited(p, lambda pl: first_gate(pl).update({"community.routeros.api_modify": first_gate(pl).pop(MOD)})))
    v = verify_playbook(doc_of(p), edited(p, lambda pl: first_gate(pl).update({"ansible.builtin.command": first_gate(pl).pop(MOD)})))
    assert any("allowlist" in x for x in v)
    assert verify_playbook(doc_of(p), edited(p, lambda pl: first_gate(pl).update(check_mode=False)))
    assert verify_playbook(doc_of(p), edited(p, lambda pl: first_gate(pl)[MOD].update(service="ssh")))
    assert verify_playbook(doc_of(p), edited(p, lambda pl: first_gate(pl)[MOD].update(port=22)))
    assert verify_playbook(doc_of(p), edited(p, lambda pl: devblock(pl, "R6-off")[1].pop("rescue")))
    assert verify_playbook(doc_of(p), edited(p, lambda pl: devblock(pl, "R6-off")[1]["block"].pop(2)))


def test_verify_rejects_jinja_and_yaml_injection():
    p = api_plan()
    v = verify_playbook(doc_of(p), edited(p, lambda pl: first_gate(pl)[MOD].update(address=["{{ lookup('pipe','id') }}"])))
    assert v
    v = verify_playbook(doc_of(p), edited(p, lambda pl: first_gate(pl)[MOD].update(host="{{ x }}")))
    assert any("Jinja" in x for x in v)
    assert verify_playbook(doc_of(p), render_playbook(p) + "\n- evil: true\n")
    assert verify_playbook(doc_of(p), "not: [valid")


def test_verify_rejects_edited_plan_commands():
    d = doc_of(api_plan())
    d["devices"][1]["steps"][1]["command"] = f"{SET} disabled=no address=0.0.0.0/0"
    assert any("plan_id" in x for x in verify_playbook(d, render_playbook(api_plan())))


def test_renderer_refuses_jinja_and_unmapped_input():
    dp = planned(7, samples.SERVICE_API_7_OFF)
    dp.steps[1].purpose = "x {{ y }}"
    with pytest.raises(RenderError, match="template syntax"):
        render_playbook(Plan(MgmtApi((J,)).as_dict(), [dp]))
    # no rollback: the guard values are unknown, so nothing is rendered
    dp = planned(7, samples.SERVICE_API_7_OFF)
    dp.steps[1].rollback = None
    with pytest.raises(RenderError, match="rollback"):
        render_playbook(Plan(MgmtApi((J,)).as_dict(), [dp]))
    # a rollback of another shape
    dp = planned(7, samples.SERVICE_API_7_OFF)
    dp.steps[1].rollback = "/ip dns set servers=1.1.1.1"
    with pytest.raises(RenderError, match="not an api service update"):
        render_playbook(Plan(MgmtApi((J,)).as_dict(), [dp]))
    # a verify that does not read the api service
    dp = planned(7, samples.SERVICE_API_7_OFF)
    dp.steps[2].expect = "disabled=no address=10.0.0.1;x"
    with pytest.raises(RenderError, match="cannot render verify"):
        render_playbook(Plan(MgmtApi((J,)).as_dict(), [dp]))
    # a stray precheck without a change (would need a read through the possibly disabled API) is refused
    dp = planned(7, samples.SERVICE_API_7_OFF)
    dp.steps = dp.steps[:1]
    with pytest.raises(RenderError, match="no Ansible mapping"):
        render_playbook(Plan(MgmtApi((J,)).as_dict(), [dp]))


def test_golden():
    assert render_playbook(api_plan()) == open(GOLDEN, encoding="utf-8").read()


ANSIBLE = os.environ.get("NETDEV_ANSIBLE_VENV") or shutil.which("ansible-playbook")


@pytest.mark.skipif(not ANSIBLE, reason="ansible-playbook not available")
def test_ansible_syntax_check_with_stub_module(tmp_path):
    exe = os.path.join(ANSIBLE, "ansible-playbook") if os.path.isdir(ANSIBLE) else ANSIBLE
    stub = tmp_path / "collections" / "ansible_collections" / "bonis" / "netdev"
    (stub / "plugins" / "modules").mkdir(parents=True)
    (stub / "galaxy.yml").write_text("namespace: bonis\nname: netdev\nversion: 0.0.0\nauthors: [x]\nreadme: README.md\n")
    (stub / "plugins" / "modules" / "routeros_service.py").write_text(
        "DOCUMENTATION = '''\nmodule: routeros_service\nshort_description: stub\noptions: {}\n'''\n")
    pb = tmp_path / "playbook.yml"
    for mode in (True, False):
        pb.write_text(render_playbook(api_plan(), check_only=mode))
        env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": f"{tmp_path / 'collections'}:{os.path.expanduser('~/.ansible/collections')}",
               "ANSIBLE_LOCAL_TEMP": str(tmp_path / "tmp"), "ANSIBLE_HOME": str(tmp_path / "home")}
        r = subprocess.run([exe, "--syntax-check", str(pb)], capture_output=True, text=True, env=env, cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr


# ------------------------------------------------------------------ templates, config and CLI
def test_seed_feature_and_template():
    cfg = load_config(SEED)
    f = cfg.features["mgmt-api"]
    assert (f.domain, f.requires, f.intent, f.protect) == ("mgmt-api", ("mgmt.api",), "mgmt-api", True)
    assert [(p.name, p.type) for p in f.params] == [("api_allowed", "list")]
    assert cfg.templates["api-bootstrap"] == ("mgmt-api",)
    assert cfg.vars["global"][""]["api_allowed"] == [J]
    for m in cfg.models.values():
        assert ("mgmt.api" in m.capabilities) == (m.os_family == "routeros")


def test_compile_ok_on_routeros_and_na_on_airos(tmp_path):
    (tmp_path / "d.yml").write_text(
        "rt: {model: MikroTik hEX S, templates: [api-bootstrap]}\nap: {model: Ubiquiti LHG-5, templates: [api-bootstrap]}\n")
    res = compile_config(load_config(SEED, tmp_path / "d.yml"))
    st = {r.device: r for r in res}
    assert st["rt"].status == OK and st["rt"].params == {"api_allowed": [J]}
    # airOS lacks the capability, so it is `invalid` (capability check comes first); a capable model without the driver is n/a
    assert st["ap"].status == "invalid" and "mgmt.api" in st["ap"].reason
    assert [(d, i.allowed) for d, i in mgmt_api_intents(res)] == [("rt", (J,))]


def test_airos_with_capability_is_na_driver_not_implemented(tmp_path):
    models = tmp_path / "config"
    shutil.copytree(SEED, models)
    ub = models / "models" / "ubiquiti.yml"
    ub.write_text(ub.read_text().replace("syslog.remote]", "syslog.remote, mgmt.api]"))
    (tmp_path / "d.yml").write_text("ap: {model: Ubiquiti LHG-5, templates: [api-bootstrap]}\n")
    res = compile_config(load_config(models, tmp_path / "d.yml"))
    assert res[0].status == NA and res[0].reason == "driver not implemented"
    assert mgmt_api_intents(res) == []


def test_template_intent_raises_config_error_on_bad_values():
    from bonis_netdev.features import ConfigError
    from bonis_netdev.templates import Result
    bad = Result("d", "m", "mgmt-api", OK, "", {"api_allowed": ["0.0.0.0/0"]}, "mgmt-api")
    with pytest.raises(ConfigError, match="d: feature mgmt-api"):
        mgmt_api_intents([bad])


def test_cli_loads_intent(tmp_path):
    i = cli.load_intent(os.path.join(HERE, "..", "examples", "mgmt-api-intent.yml"))
    assert isinstance(i, MgmtApi) and i.allowed == (J,)
    f = tmp_path / "i.yml"
    for text, msg in (("intent: mgmt-api\nallowed: 10.0.0.1\n", "allowed must be a list"),
                      ("intent: mgmt-api\n", "allowed must be a list"),
                      ("intent: mgmt-api\nallowed: [10.0.0.1]\nport: 22\n", "unknown field port"),
                      ("intent: mgmt-api\nallowed: [0.0.0.0/0]\n", "wider than"),
                      ("intent: mgmt-api\nallowed: []\n", "at least one")):
        f.write_text(text)
        with pytest.raises(IntentError, match=msg):
            cli.load_intent(str(f))


def test_cli_plan_writes_plan_and_airos_is_unsupported(tmp_path, monkeypatch):
    transports = {"10.0.0.1": samples.ros_service_transport(samples.SERVICE_API_6_OFF, "10.0.0.1"),
                  "10.0.0.2": samples.airos_cfg_transport(samples.SYSTEM_CFG_XW, "10.0.0.2")}
    monkeypatch.setattr(cli, "SshReadTransport", lambda host, creds: transports[host])
    (tmp_path / "creds.yml").write_text("credential_sets: [{username: u, password: p}]\n")
    (tmp_path / "t.yml").write_text("targets:\n  - {name: R, host: 10.0.0.1}\n  - {name: AP, host: 10.0.0.2}\n")
    out = tmp_path / "plan.json"
    rc = cli.main(["plan", "--intent", os.path.join(HERE, "..", "examples", "mgmt-api-intent.yml"), "--targets", str(tmp_path / "t.yml"),
                   "--credentials", str(tmp_path / "creds.yml"), "--out", str(out), "--workers", "1"])
    assert rc == 0
    doc = json.loads(out.read_text())
    assert doc["intent"]["intent"] == "mgmt-api" and doc["writes_allowed"] is False
    by = {d["device"]: d for d in doc["devices"]}
    assert len(by["R"]["steps"]) == 3 and by["AP"]["steps"] == [] and by["AP"]["unsupported"]
    assert verify_playbook(doc, render_playbook(plan_from_dict(doc))) == []


def test_plan_summary_names_the_allowed_addresses():
    assert J in api_plan().summary().splitlines()[0]


# ------------------------------------------------------------------ real captures (verbatim, trailing spaces kept)
def test_real_captures_are_verbatim():
    assert samples.SERVICE_API_REAL_6_OFF.splitlines() == ['Flags: X - disabled, I - invalid ', ' 0 XI name="api" port=8728 address="" ']
    assert samples.SERVICE_API_REAL_7_OPEN.splitlines()[1] == ' 0   name="api" port=8728 address="" vrf=main max-sessions=20 '


def test_real_ros6_disabled_xi_means_disabled_and_empty_address_is_open():
    dp = planned(6, samples.SERVICE_API_REAL_6_OFF)
    assert dp.steps[0].expect == "disabled=yes address=" and not dp.unsupported
    assert dp.steps[1].rollback == f'{SET} disabled=yes address=""' and dp.notes == []


def test_real_ros7_enabled_unrestricted_tolerates_vrf_and_max_sessions():
    dp = planned(7, samples.SERVICE_API_REAL_7_OPEN)
    assert dp.steps[0].expect == "disabled=no address="
    assert dp.steps[1].command == f"{SET} disabled=no address={J}"
    assert dp.steps[1].rollback == f'{SET} disabled=no address=""'
    assert any("open to everyone" in n for n in dp.notes)


def test_invalid_flag_without_disabled_is_unsupported():
    dp = planned(7, 'Flags: X - disabled, I - invalid \n 0 I  name="api" port=8728 address="" \n')
    assert dp.steps == [] and "invalid" in dp.unsupported[0]


# ------------------------------------------------------------------ review round: restore flag, wrapped values
import importlib.util
import re as _re

_COLL = os.path.join(HERE, "..", "ansible", "collections", "ansible_collections", "bonis", "netdev")
_spec = importlib.util.spec_from_file_location("rs_lib", os.path.join(_COLL, "plugins", "module_utils", "routeros_service.py"))
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


def _device(text):
    class T(rs.Transport):
        def __init__(self):
            self.text, self.cmds = text, []

        def run(self, command):
            self.cmds.append(command)
            if command == rs.READ_CMD:
                return self.text
            m = _re.match(r'/ip service set \[find name=api\] disabled=(yes|no) address=(.*)$', command)
            addrs = [] if m.group(2) == '""' else m.group(2).split(",")
            self.text = ' 3 %sname="api" port=8728 address=%s\n' % ("X " if m.group(1) == "yes" else "  ", ",".join(addrs) or '""')
            return ""
    return T()


def _rescues(play):
    out = []

    def walk(tasks, in_rescue):
        for t in tasks:
            if "block" in t:
                walk(t["block"], in_rescue)
                walk(t.get("rescue", []), True)
            elif MOD in t:
                out.append((in_rescue, t))
    walk(play["tasks"], False)
    return out


def _p(a, **kw):
    """Module parameters as Ansible would pass them after templating the credentials."""
    return {**a, "username": "u", "password": "pw", "host": "10.0.0.23", "port": 22, "service": "api", **kw}


def _state_text(disabled, addrs):
    return ' 3 %sname="api" port=8728 address=%s\n' % ("X " if disabled else "  ", ",".join(addrs) or '""')


START_STATES = {
    "disabled+unrestricted": samples.SERVICE_API_REAL_6_OFF,
    "enabled+unrestricted": samples.SERVICE_API_REAL_7_OPEN,
    "enabled+other": samples.SERVICE_API_7_ON_OTHER,
    "enabled+wide": _state_text(False, ["10.0.0.0/8", "0.0.0.0/0"]),
    "enabled+host-bits": _state_text(False, ["192.168.1.5/24"]),
    "enabled+9-entries": _state_text(False, ["10.0.0.%d" % i for i in range(1, 10)]),
    "disabled+with-list": _state_text(True, ["10.1.1.0/24", "10.2.2.2"]),
}


@pytest.fixture
def writes_on(monkeypatch):
    monkeypatch.setattr(rs, "WRITES_ENABLED", True)


@pytest.mark.parametrize("name", sorted(START_STATES))
def test_every_rescue_task_restores_the_start_state(name, writes_on):
    text = START_STATES[name]
    dp = planned(7, text)
    assert dp.changes == 1 and not dp.unsupported, (name, dp.unsupported)
    play = yaml.safe_load(render_playbook(Plan(MgmtApi((J,)).as_dict(), [dp]), check_only=False))[0]
    tasks = _rescues(play)
    assert [r for r, _ in tasks].count(True) == 1
    (_, rescue), = [x for x in tasks if x[0]]
    assert rescue[MOD]["restore"] is True
    # the device is in the state the forward change created; the rescue module call must restore the start state
    dev = _device(_state_text(False, [J]))
    r = rs.run_module(_p(rescue[MOD]), False, dev)
    assert not r.get("failed") and r["changed"] is True, r
    assert dev.cmds[1] == dp.steps[1].rollback, dev.cmds
    assert dev.cmds[1].startswith("/ip service set [find name=api] disabled=")


def test_restore_of_unrestricted_issues_address_empty_string(writes_on):
    dp = planned(7, samples.SERVICE_API_REAL_7_OPEN)
    play = yaml.safe_load(render_playbook(Plan(MgmtApi((J,)).as_dict(), [dp]), check_only=False))[0]
    (_, rescue), = [x for x in _rescues(play) if x[0]]
    dev = _device(_state_text(False, [J]))
    rs.run_module(_p(rescue[MOD]), False, dev)
    assert dev.cmds[1] == '/ip service set [find name=api] disabled=no address=""'


def test_goldens_rescue_tasks_run_through_the_module(writes_on):
    play = yaml.safe_load(open(GOLDEN, encoding="utf-8").read())[0]
    n = 0
    for in_rescue, t in _rescues(play):
        if not in_rescue:
            continue
        n += 1
        a = t[MOD]
        # simulate the post-change device: from_* is exactly what the forward change created
        dev = _device(_state_text(a["from_disabled"], a["from_address"]))
        r = rs.run_module(_p(a), False, dev)
        assert not r.get("failed") and r["changed"], r
        assert dev.cmds[1] == rs.cmd_set(a["disabled"], a["address"])
        # without the drift guard the module refuses
        for drop in ("from_disabled", "from_address"):
            b = {k: v for k, v in a.items() if k != drop}
            assert rs.run_module(_p(b), False, _device(dev.text))["failed"]
    assert n == 3


def test_renderer_emits_restore_only_in_rescue():
    for check_only in (True, False):
        play = yaml.safe_load(render_playbook(api_plan(), check_only=check_only))[0]
        for in_rescue, t in _rescues(play):
            assert (t[MOD].get("restore") is True) == in_rescue, t["name"]
    assert "restore: false" not in render_playbook(api_plan())


def test_verify_rejects_restore_flag_on_forward_tasks():
    p = api_plan()
    for idx, path in ((0, "gate"), (1, "change")):
        def fn(pl, idx=idx):
            body = devblock(pl, "R6-off")
            (body[0] if idx == 0 else body[1]["block"][0])[MOD]["restore"] = True
        v = verify_playbook(doc_of(p), edited(p, fn))
        assert v, path
    def verify_task(pl):
        devblock(pl, "R6-off")[1]["block"][1][MOD]["restore"] = True
    assert verify_playbook(doc_of(p), edited(p, verify_task))
    def drop_restore(pl):
        devblock(pl, "R6-off")[1]["rescue"][0][MOD].pop("restore")
    assert verify_playbook(doc_of(p), edited(p, drop_restore))


def test_driver_refuses_wrapped_address_and_over_32_entries():
    wrapped = ' 0 name="api" address=1.2.3.4/32,5.6.7.8/32\n      ,9.9.9.9/32'
    dp = planned(7, wrapped)
    assert dp.steps == [] and "wrapped" in dp.unsupported[0]
    dp = planned(7, 'Flags: X\n 0 X  name="api" port=8728 address=10.0.0.0/24,\n      10.1.0.0/24 vrf=main\n')
    assert dp.steps == [] and "wrapped" in dp.unsupported[0]
    many = _state_text(False, ["10.0.%d.0/24" % i for i in range(33)])
    dp = planned(7, many)
    assert dp.steps == [] and "more than 32" in dp.unsupported[0]
    assert planned(7, _state_text(False, ["10.0.%d.0/24" % i for i in range(32)])).changes == 1
    # a later row after the api row is not a continuation
    two = ' 0   name="api" port=8728 address=10.1.1.0/24\n 1   name="ssh" port=22 address=""\n'
    assert planned(7, two).changes == 1


def test_airos_mgmt_api_is_invalid_for_missing_capability(tmp_path):
    (tmp_path / "d.yml").write_text("ap: {model: Ubiquiti LHG-5, templates: [api-bootstrap]}\n")
    res = compile_config(load_config(SEED, tmp_path / "d.yml"))
    assert res[0].status == "invalid" and "lacks capability mgmt.api" in res[0].reason


def test_driver_wrap_detection_uses_every_line_of_the_record():
    text = ' 0 name="api" port=8728\n      address=10.0.0.1/32,10.0.0.2/32\n      ,10.0.0.3/32 vrf=main\n'
    dp = planned(7, text)
    assert dp.steps == [] and "wrapped" in dp.unsupported[0]
    dp = planned(7, ' 0 name="api" port=8728\n      address=10.0.0.1/32 \n      vrf=main\n')    # documented false refusal
    assert dp.steps == [] and dp.unsupported


def test_driver_refuses_a_commented_api_row():
    dp = planned(7, ' 0 ;;; address=192.168.238.0/24 x\n      name="api" port=8728 vrf=main\n')
    assert dp.steps == [] and "comment" in dp.unsupported[0]
    dp = planned(7, ' 0   name="api" port=8728 address=""\n      ;;; note\n')
    assert dp.steps == [] and "comment" in dp.unsupported[0]
    # a comment on ANOTHER row does not matter
    other = ' 0   name="www" port=80\n      ;;; web\n 1   name="api" port=8728 address=""\n'
    assert planned(7, other).changes == 1
