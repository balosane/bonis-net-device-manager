import copy
import json
import os
import shutil
import subprocess

import pytest
import yaml

from bonis_netdev import cli
from bonis_netdev.drivers.airos import AirosDriver
from bonis_netdev.drivers.base import build_device_plan
from bonis_netdev.drivers.routeros import RouterOsDriver
from bonis_netdev.intents import DhcpNetworks, Dns
from bonis_netdev.plan import DevicePlan, Plan, Step
from bonis_netdev.render import RenderError, render_playbook, verify_playbook
from bonis_netdev import samples

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "demo.playbook.yml")


def demo_plan() -> Plan:
    intent = Dns("192.168.238.119", "192.168.6.1", DhcpNetworks(exclude=("192.168.88.0/24",)), reason="demo")
    devs = [build_device_plan(RouterOsDriver(6), "DEMO-ROUTER", intent, samples.ros_transport()),
            build_device_plan(AirosDriver(), "DEMO-AP", Dns("192.168.238.119", "192.168.6.1"), samples.airos_transport())]
    return Plan(intent.as_dict(), devs)


def resolver_plan() -> Plan:
    intent = Dns("1.1.1.1", "8.8.8.8")
    return Plan(intent.as_dict(), [build_device_plan(RouterOsDriver(6), "R1", intent, samples.ros_transport())])


def doc_of(plan) -> dict:
    return json.loads(plan.to_json("2026-10-22T00:00:00+00:00"))


def tasks_of(text):
    return yaml.safe_load(text)[0]["tasks"]


def flat(tasks):
    for t in tasks:
        yield t
        for k in ("block", "rescue"):
            if k in t:
                yield from flat(t[k])


def find(text, module):
    return [t for t in flat(tasks_of(text)) if module in t]


def test_deterministic():
    assert render_playbook(demo_plan()) == render_playbook(demo_plan())
    assert render_playbook(demo_plan(), check_only=False) != render_playbook(demo_plan())


def test_golden():
    assert render_playbook(demo_plan()) == open(GOLDEN, encoding="utf-8").read()


def test_header_vars_and_no_credentials():
    text = render_playbook(demo_plan())
    play = yaml.safe_load(text)[0]
    assert play["vars"]["netdev_plan_id"] == demo_plan().plan_id
    assert play["vars"]["netdev_check_only"] is True
    assert play["hosts"] == "localhost" and play["connection"] == "local"
    assert "SuperSecretPsk" not in text and "$1$hash" not in text
    assert "{{ netdev_user }}" in text and "{{ netdev_pass }}" in text


def test_every_task_has_check_mode_when_check_only():
    for t in flat(tasks_of(render_playbook(demo_plan()))):
        if "block" not in t:
            assert t["check_mode"] is True


def test_apply_mode_follows_variable():
    text = render_playbook(demo_plan(), check_only=False)
    play = yaml.safe_load(text)[0]
    assert play["vars"]["netdev_check_only"] is False
    leaves = [t for t in flat(play["tasks"]) if "block" not in t]
    literal = [t for t in leaves if t["check_mode"] is True]
    assert all(t["check_mode"] == "{{ netdev_check_only | bool }}" for t in leaves if t not in literal)
    # only the airOS gate and verify tasks are pinned to check mode in apply mode
    assert literal and all("bonis.netdev.airos_cfg" in t or "ansible.builtin.assert" in t for t in literal)
    # the very first task refuses a non-boolean netdev_check_only (e.g. the string "false")
    assert play["tasks"][0]["ansible.builtin.assert"]["that"] == ["netdev_check_only is boolean"]
    assert yaml.safe_load(render_playbook(demo_plan()))[0]["tasks"][0]["ansible.builtin.assert"]["that"] == ["netdev_check_only is boolean"]


def test_resolver_mapping():
    text = render_playbook(resolver_plan())
    mod = find(text, "community.routeros.api_modify")
    assert len(mod) == 2   # change + rollback
    a = mod[0]["community.routeros.api_modify"]
    assert a["path"] == "ip dns" and a["data"] == [{"servers": "1.1.1.1,8.8.8.8"}]
    assert a["handle_absent_entries"] == "ignore" and a["handle_entries_content"] == "ignore"
    assert a["hostname"] == "10.0.0.23"
    infos = find(text, "community.routeros.api_info")
    assert len(infos) == 2 and all(i["community.routeros.api_info"]["path"] == "ip dns" for i in infos)
    asserts = find(text, "ansible.builtin.assert")
    assert any("'192.168.6.1,8.8.8.8'" in c for t in asserts for c in t["ansible.builtin.assert"]["that"])  # precheck
    verify = [t for t in find(text, "ansible.builtin.assert") if t.get("when") == "not (netdev_check_only | bool)"]
    assert verify and "'1.1.1.1,8.8.8.8'" in verify[0]["ansible.builtin.assert"]["that"][0]


def test_dhcp_mapping_and_exists_precheck():
    text = render_playbook(demo_plan())
    mods = [t["community.routeros.api_modify"] for t in find(text, "community.routeros.api_modify")]
    assert {"address": "192.168.80.0/26", "dns-server": "192.168.238.119,192.168.6.1"} in [m["data"][0] for m in mods]
    assert all(m["path"] == "ip dhcp-server network" for m in mods)
    assert not any(m["data"][0]["address"] == "192.168.88.0/24" for m in mods)
    # the change block starts with a read + an existence assert before api_modify
    blocks = [t for t in flat(tasks_of(text)) if "block" in t and "rescue" in t and "community.routeros.api_info" in str(t["block"][0])]
    assert blocks
    kinds = [next(k for k in t if k not in ("name", "register", "check_mode", "when")) for t in blocks[0]["block"][:3]]
    assert kinds == ["community.routeros.api_info", "ansible.builtin.assert", "community.routeros.api_modify"]
    assert "length == 1" in blocks[0]["block"][1]["ansible.builtin.assert"]["that"][0]


def test_airos_mapping():
    text = render_playbook(demo_plan())
    mods = [t for t in find(text, "bonis.netdev.airos_cfg")]
    first = mods[0]["bonis.netdev.airos_cfg"]
    assert first["host"] == "192.168.1.20" and first["state"] == "present" and len(first["base"]) == 64
    assert first["settings"] == {"resolv.nameserver.1.ip": "192.168.238.119", "resolv.nameserver.2.ip": "192.168.6.1"}
    assert first["username"] == "{{ netdev_user }}" and first["password"] == "{{ netdev_pass }}"
    assert any(m["bonis.netdev.airos_cfg"]["base"] == "current" for m in mods)


def test_rollback_in_rescue_and_precheck_outside():
    tasks = tasks_of(render_playbook(demo_plan()))
    router = next(t for t in tasks if t["name"].startswith("DEMO-ROUTER"))
    groups = [t for t in router["block"] if "rescue" in t]
    assert groups
    for g in groups:
        assert any("community.routeros.api_modify" in t for t in g["rescue"])
        assert g["rescue"][-1]["ansible.builtin.assert"]["that"] == ["false"]
        assert not any("rescue" in t for t in g["block"])
    ap = next(t for t in tasks if t["name"].startswith("DEMO-AP"))
    rb = next(t for t in ap["block"] if "rescue" in t)["rescue"][0]["bonis.netdev.airos_cfg"]
    assert rb["base"] == "current" and rb["settings"] == {"resolv.nameserver.1.ip": "192.168.6.1"}
    # prechecks sit in the device block, outside any rescue-protected group
    assert "ansible.builtin.assert" in router["block"][1] or "community.routeros.api_info" in router["block"][0]


def _ancestors(tasks, path=()):
    """Yield (task, ancestor blocks) for every task, walking block/rescue/always."""
    for t in tasks:
        yield t, path
        for k in ("block", "rescue", "always"):
            if k in t:
                yield from _ancestors(t[k], path + (t,))


def test_airos_gate_is_a_real_check_mode_task_outside_every_rescue():
    for check_only in (True, False):
        text = render_playbook(demo_plan(), check_only=check_only)
        gates = [(t, anc) for t, anc in _ancestors(tasks_of(text))
                 if "bonis.netdev.airos_cfg" in t and t["name"].startswith("DEMO-AP: gate")]
        assert len(gates) == 1
        gate, anc = gates[0]
        args = gate["bonis.netdev.airos_cfg"]
        assert len(args["base"]) == 64 and args["settings"] == {"resolv.nameserver.1.ip": "192.168.238.119",
                                                                "resolv.nameserver.2.ip": "192.168.6.1"}
        assert gate["check_mode"] is True                  # literal, also in apply mode
        # a drift failure of the gate cannot reach a rescue: no ancestor block has one
        assert anc and not any("rescue" in a for a in anc)
        # and it precedes the rescued group in the device block
        dev = anc[-1]["block"]
        group = next(t for t in dev if "rescue" in t)
        assert dev.index(gate) < dev.index(group)
        # the change itself is the same settings/base inside the rescued block
        inner = group["block"][0]["bonis.netdev.airos_cfg"]
        assert inner["base"] == args["base"] and inner["settings"] == args["settings"]
        # no more debug-only airOS precheck
        assert not any("ansible.builtin.debug" in t and "DEMO-AP" in t["name"] for t, _ in _ancestors(tasks_of(text)))


def test_verify_rejects_playbook_without_the_gate():
    plan = demo_plan()
    text = render_playbook(plan)
    play = yaml.safe_load(text)
    ap = next(t for t in play[0]["tasks"] if t["name"].startswith("DEMO-AP"))
    ap["block"] = [t for t in ap["block"] if not t.get("name", "").startswith("DEMO-AP: gate")]
    assert verify_playbook(doc_of(plan), yaml.safe_dump(play), require_check_only=True)


def test_airos_verify_is_always_literal_check_mode():
    for check_only in (True, False):
        tasks = tasks_of(render_playbook(demo_plan(), check_only=check_only))
        ap = next(t for t in tasks if t["name"].startswith("DEMO-AP"))
        group = next(t for t in ap["block"] if "rescue" in t)
        i = next(i for i, t in enumerate(group["block"]) if "bonis.netdev.airos_cfg" in t and t["bonis.netdev.airos_cfg"]["base"] == "current")
        verify, assrt = group["block"][i], group["block"][i + 1]
        assert verify["check_mode"] is True
        assert assrt["ansible.builtin.assert"]["that"] == [f"not {verify['register']}.changed"]
        assert verify["when"] == assrt["when"] == "not (netdev_check_only | bool)"
        # the change task itself still follows the variable in apply mode
        change = group["block"][0]
        assert change["check_mode"] == (True if check_only else "{{ netdev_check_only | bool }}")


def test_check_only_conditions_use_bool_filter_everywhere():
    import re
    text = render_playbook(demo_plan(), check_only=False)
    for t, _ in _ancestors(tasks_of(text)):
        for key in ("when", "check_mode"):
            v = t.get(key)
            if isinstance(v, str):
                assert re.sub(r"netdev_check_only \| bool", "", v).count("netdev_check_only") == 0, (t["name"], v)
    assert any(t.get("when") for t, _ in _ancestors(tasks_of(text)))


def test_conditions_evaluate_correctly_for_bool_and_string_values():
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment()
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "on", "1")
    tasks = list(_ancestors(tasks_of(render_playbook(demo_plan(), check_only=False))))
    whens = [t["when"] for t, _ in tasks if "when" in t]
    checks = [t["check_mode"] for t, _ in tasks if isinstance(t.get("check_mode"), str)]
    assert whens and checks
    for value, writing in ((False, True), ("false", True), ("False", True), (True, False), ("true", False)):
        for w in whens:   # verify steps must run exactly when writes are on
            assert env.compile_expression(w)(netdev_check_only=value) is writing, (w, value)
        for c in checks:  # check_mode must be on exactly when check-only
            assert env.from_string(c).render(netdev_check_only=value) == str(not writing), (c, value)


def test_no_rollback_when_old_value_empty():
    r = Plan({"intent": "dns"}, [DevicePlan("D", "10.0.0.1", "routeros6", "x", steps=[
        Step("precheck", "/ip dns print", "p"),
        Step("change", "/ip dns set servers=1.1.1.1", "c"),
        Step("verify", "/ip dns print", "v", expect="servers: 1.1.1.1")])])
    group = next(t for t in tasks_of(render_playbook(r))[2]["block"] if "block" in t)
    assert "rescue" not in group


def test_deny_by_default_unknown_step():
    plan = demo_plan()
    step = plan.devices[0].steps[0]
    step.command = "/user remove [find]"      # bypasses Step validation on purpose
    with pytest.raises(RenderError, match="no Ansible mapping"):
        render_playbook(plan)


def test_unmappable_and_hostile_inputs():
    plan = demo_plan()
    plan.devices[1].steps[1].command = "airos-cfg-set base=current a.b={{lookup('pipe','id')}}"
    with pytest.raises(RenderError, match="template"):
        render_playbook(plan)
    plan = demo_plan()
    plan.devices[0].steps[0].purpose = "x {{ evil }}"
    with pytest.raises(RenderError, match="template"):
        render_playbook(plan)
    plan = demo_plan()
    plan.devices[0].error = "boom"
    with pytest.raises(RenderError, match="error"):
        render_playbook(plan)
    plan = demo_plan()
    plan.devices[0].driver = "cisco"
    with pytest.raises(RenderError, match="no Ansible mapping"):
        render_playbook(plan)


def test_round_trip_ok():
    plan = demo_plan()
    assert verify_playbook(doc_of(plan), render_playbook(plan)) == []
    assert verify_playbook(doc_of(plan), render_playbook(plan, check_only=False)) == []
    assert verify_playbook(doc_of(plan), render_playbook(plan, check_only=False), require_check_only=True)


def test_verify_catches_changed_plan_id():
    plan = demo_plan()
    text = render_playbook(plan)
    assert any("plan_id" in v for v in verify_playbook(doc_of(plan), text.replace(plan.plan_id, "0" * 64)))
    doc = doc_of(plan)
    doc["devices"][0]["steps"][1]["command"] = "/ip dns set servers=9.9.9.9"   # plan edited, id kept
    assert any("modified" in v for v in verify_playbook(doc, text))
    doc = doc_of(plan)
    doc["plan_id"] = "0" * 64
    assert any("modified" in v for v in verify_playbook(doc, text))


def edited(plan, fn):
    pb = yaml.safe_load(render_playbook(plan))
    fn(pb[0])
    return yaml.safe_dump(pb, sort_keys=False)


def test_verify_catches_extra_shell_task():
    plan = demo_plan()
    text = edited(plan, lambda p: p["tasks"].append({"name": "x", "ansible.builtin.shell": "reboot", "check_mode": True}))
    v = verify_playbook(doc_of(plan), text)
    assert any("ansible.builtin.shell" in x and "allowlist" in x for x in v)
    text = edited(plan, lambda p: p["tasks"][2]["block"].append({"name": "x", "raw": "reboot", "check_mode": True}))
    assert any("'raw'" in x for x in verify_playbook(doc_of(plan), text))


def test_verify_catches_missing_check_mode():
    plan = demo_plan()
    text = edited(plan, lambda p: p["tasks"][0].pop("check_mode"))
    v = verify_playbook(doc_of(plan), text)
    assert any("lacks check_mode" in x for x in v)
    text = edited(plan, lambda p: p["vars"].pop("netdev_check_only"))
    assert any("netdev_check_only" in x for x in verify_playbook(doc_of(plan), text))


def test_verify_catches_reorder_and_modified_command():
    plan = demo_plan()
    text = edited(plan, lambda p: p["tasks"].reverse())
    assert any("differs" in x for x in verify_playbook(doc_of(plan), text))

    def mod(p):
        for t in flat(p["tasks"]):
            if "community.routeros.api_modify" in t and t["community.routeros.api_modify"]["path"] == "ip dhcp-server network":
                t["community.routeros.api_modify"]["data"][0]["dns-server"] = "9.9.9.9"
                return
    assert any("differs" in x for x in verify_playbook(doc_of(plan), edited(plan, mod)))
    text = edited(plan, lambda p: p.update(roles=["x"]))
    assert any("roles" in x for x in verify_playbook(doc_of(plan), text))
    text = edited(plan, lambda p: p["vars"].update(netdev_check_only=False))
    assert verify_playbook(doc_of(plan), text)       # flipped flag but check_mode: true tasks differ


def test_cli_render_and_verify(tmp_path, capsys):
    plan = demo_plan()
    f, out = tmp_path / "plan.json", tmp_path / "playbook.yml"
    f.write_text(plan.to_json("2026-10-22T00:00:00+00:00"))
    assert cli.main(["render-playbook", str(f), "--out", str(out)]) == 0
    assert out.read_text() == render_playbook(plan)
    assert cli.main(["verify-playbook", str(f), str(out)]) == 0
    assert "OK" in capsys.readouterr().out
    out.write_text(out.read_text().replace("\n    check_mode: true", "\n    check_mode: false", 1))
    assert cli.main(["verify-playbook", str(f), str(out)]) == 1
    assert "VIOLATION" in capsys.readouterr().out
    assert cli.main(["render-playbook", str(f), "--apply-mode"]) == 0
    assert "WARNING" in capsys.readouterr().err
    doc = json.loads(f.read_text())
    doc["devices"][0]["host"] = "6.6.6.6"
    f.write_text(json.dumps(doc))
    assert cli.main(["render-playbook", str(f)]) == 2


ANSIBLE = os.environ.get("NETDEV_ANSIBLE_VENV") or shutil.which("ansible-playbook")


@pytest.mark.skipif(not ANSIBLE, reason="ansible-playbook not available (set NETDEV_ANSIBLE_VENV to a bin dir or put it on PATH)")
def test_ansible_syntax_check(tmp_path):
    exe = os.path.join(ANSIBLE, "ansible-playbook") if os.path.isdir(ANSIBLE) else ANSIBLE
    stub = tmp_path / "collections" / "ansible_collections" / "bonis" / "netdev"
    (stub / "plugins" / "modules").mkdir(parents=True)
    (stub / "galaxy.yml").write_text("namespace: bonis\nname: netdev\nversion: 0.0.0\nauthors: [x]\nreadme: README.md\n")
    (stub / "plugins" / "modules" / "airos_cfg.py").write_text(
        "DOCUMENTATION = '''\nmodule: airos_cfg\nshort_description: stub\noptions: {}\n'''\n")
    pb = tmp_path / "playbook.yml"
    for mode in (True, False):
        pb.write_text(render_playbook(demo_plan(), check_only=mode))
        env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": f"{tmp_path / 'collections'}:{os.path.expanduser('~/.ansible/collections')}",
               "ANSIBLE_LOCAL_TEMP": str(tmp_path / "tmp"), "ANSIBLE_HOME": str(tmp_path / "home")}
        r = subprocess.run([exe, "--syntax-check", str(pb)], capture_output=True, text=True, env=env, cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr


def test_check_only_boolean_assert_evaluates_with_jinja():
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment()
    expr = env.compile_expression("netdev_check_only is boolean")
    assert expr(netdev_check_only=True) is True and expr(netdev_check_only=False) is True
    for bad in ("false", "true", "False", 0, 1, None, "no"):
        assert expr(netdev_check_only=bad) is False


def test_verify_rejects_tampered_play_name_with_jinja():
    plan = demo_plan()
    text = edited(plan, lambda p: p.update(name="{{ lookup('pipe','id') }}"))
    v = verify_playbook(doc_of(plan), text)
    assert any("play name" in x for x in v) and any("Jinja" in x for x in v)
    text = edited(plan, lambda p: p.update(name="harmless but different"))
    assert any("play name" in x for x in verify_playbook(doc_of(plan), text))


def test_verify_rejects_tampered_task_name():
    plan = demo_plan()
    text = edited(plan, lambda p: p["tasks"][2]["block"][0].update(name="{{ lookup('pipe','id') }}"))
    v = verify_playbook(doc_of(plan), text)
    assert any("differs" in x for x in v) and any("Jinja" in x for x in v)
    text = edited(plan, lambda p: p["tasks"][2]["block"][0].update(name="renamed"))
    assert any("differs" in x for x in verify_playbook(doc_of(plan), text))


@pytest.mark.parametrize("marker", ["{{ lookup('pipe','id') }}", "{% if true %}x{% endif %}", "{# c #}"])
def test_verify_rejects_injected_jinja_marker_in_vars_task_and_key(marker):
    plan = demo_plan()
    for fn in (lambda p: p["vars"].update(netdev_api_tls=marker),
               lambda p: p["tasks"][1].update(name=marker),
               lambda p: p.update({"# " + marker: "x"})):
        v = verify_playbook(doc_of(plan), edited(plan, fn))
        assert any("Jinja marker" in x for x in v), v


def test_jinja_scan_walks_keys_and_values():
    from bonis_netdev.render.ansible import _jinja_strings
    assert set(_jinja_strings({"a": ["x {{ y }}"], "{#k": 1})) == {(("<key>", "{#k"), "{#k"), (("a", 0), "x {{ y }}")}


def test_verify_legit_render_with_renderer_jinja_still_ok():
    plan = demo_plan()
    text = render_playbook(plan, check_only=False)
    assert "{{ netdev_check_only | bool }}" in text
    assert verify_playbook(doc_of(plan), text) == []
