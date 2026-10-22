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
    assert all(t["check_mode"] == "{{ netdev_check_only | bool }}" for t in flat(play["tasks"]) if "block" not in t)


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
    verify = [t for t in find(text, "ansible.builtin.assert") if t.get("when") == "not netdev_check_only"]
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


def test_no_rollback_when_old_value_empty():
    r = Plan({"intent": "dns"}, [DevicePlan("D", "10.0.0.1", "routeros6", "x", steps=[
        Step("precheck", "/ip dns print", "p"),
        Step("change", "/ip dns set servers=1.1.1.1", "c"),
        Step("verify", "/ip dns print", "v", expect="servers: 1.1.1.1")])])
    group = next(t for t in tasks_of(render_playbook(r))[1]["block"] if "block" in t)
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
    text = edited(plan, lambda p: p["tasks"][1]["block"].append({"name": "x", "raw": "reboot", "check_mode": True}))
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
