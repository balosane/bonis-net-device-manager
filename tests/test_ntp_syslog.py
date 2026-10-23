"""ntp-standard and syslog-standard end to end (BONISISP-46): parse, intents, classify, drivers, renderer, templates, CLI.
All device output is recorded/canned; nothing here touches a device."""
import json
import os
import subprocess
import shutil

import pytest
import yaml

from bonis_netdev import cli, samples
from bonis_netdev.classify import Effect, effect_of
from bonis_netdev.drivers.airos import AirosDriver
from bonis_netdev.drivers.base import build_device_plan, detect_driver
from bonis_netdev.drivers.routeros import RouterOsDriver
from bonis_netdev.intents import IntentError, Ntp, Syslog
from bonis_netdev.parse import ros_print_map
from bonis_netdev.plan import Plan, Step
from bonis_netdev.render import RenderError, render_playbook, verify_playbook
from bonis_netdev.templates import OK, compile_config, load_config, ntp_intents, syslog_intents

HERE = os.path.dirname(__file__)
GOLDEN_NTP = os.path.join(HERE, "golden", "ntp.playbook.yml")
GOLDEN_SYSLOG = os.path.join(HERE, "golden", "syslog.playbook.yml")
SEED = os.path.join(HERE, "..", "config")
SHA = "a" * 64

A, B = "192.168.6.1", "195.113.144.201"


def plan_of(driver, intent, transport, name="D1"):
    return build_device_plan(driver, name, intent, transport)


def change(dp):
    return [s for s in dp.steps if s.phase == "change"]


def ros6(text=samples.NTP_PRINT_6):
    return samples.ros_ntp_transport(text)


def ros7(text=samples.NTP_PRINT_7):
    return samples.ros_ntp_transport(text, resource=samples.RESOURCE_7)


def xw(extra="", drop=()):
    lines = [l for l in samples.SYSTEM_CFG_XW.splitlines() if l.split("=")[0] not in drop]
    return samples.airos_cfg_transport("\n".join(lines) + "\n" + extra)


# ------------------------------------------------------------------ parser
def test_print_map_recorded_ros6_and_detail():
    m = ros_print_map(samples.NTP_PRINT_6)
    assert (m["enabled"], m["primary-ntp"], m["secondary-ntp"], m["server-dns-names"], m["mode"]) == ("yes", A, "0.0.0.0", "", "unicast")
    d = ros_print_map(samples.NTP_DETAIL_6)
    assert (d["enabled"], d["primary-ntp"], d["secondary-ntp"], d["server-dns-names"]) == ("yes", A, "0.0.0.0", "")


def test_print_map_ros7_continuation_lines_and_detail():
    assert ros_print_map(samples.NTP_PRINT_7)["servers"] == f"{A},{B}"
    assert ros_print_map(samples.NTP_PRINT_7)["vrf"] == "main"
    assert ros_print_map(samples.NTP_DETAIL_7)["servers"] == f"{A},{B}"
    assert ros_print_map("   enabled: yes\n    servers: 2001:db8::1\n             fe80::2\n   vrf: main\n")["servers"] == "2001:db8::1,fe80::2"
    assert ros_print_map("   servers: \n   vrf: main\n")["servers"] == ""


# ------------------------------------------------------------------ intents
def test_ntp_trailing_newline_is_refused():
    for bad in ("a.example\n", "a.example;reboot", "a b", "a{{b}}", "`x`", "$(x)", "a.example\nb"):
        with pytest.raises(IntentError):
            Ntp((bad,))


def test_syslog_intent_validation():
    assert Syslog("192.168.6.1").as_dict() == {"intent": "syslog", "host": "192.168.6.1", "port": 514, "reason": ""}
    for bad in ("tik.example", "2001:db8::1", "192.168.6.1\n", "192.168.06.1", "1.2.3"):
        with pytest.raises(IntentError):
            Syslog(bad)
    for port in (1, 65535):
        assert Syslog("10.0.0.1", port).port == port
    for port in (0, 65536, -1, "514", True, None):
        with pytest.raises(IntentError):
            Syslog("10.0.0.1", port)


# ------------------------------------------------------------------ classify
OK_CMDS = [
    ("/system ntp client print", Effect.READ),
    ("/system logging action print detail without-paging", Effect.READ),
    ("/system logging action print detail without-paging where name=netdev-remote", Effect.READ),
    ("/system ntp client set enabled=yes primary-ntp=192.168.6.1 secondary-ntp=0.0.0.0", Effect.UPDATE),
    ("/system ntp client set enabled=no primary-ntp=1.2.3.4 secondary-ntp=5.6.7.8", Effect.UPDATE),
    ("/system ntp client set enabled=yes servers=192.168.6.1", Effect.UPDATE),
    ("/system ntp client set enabled=yes servers=192.168.6.1,tik.skamanet.cz", Effect.UPDATE),
    ("/system logging action set [find name=netdev-remote] remote=10.0.0.1 remote-port=514", Effect.UPDATE),
    ("airos-cfg-set base=current ntpclient.1.server=tik.skamanet.cz ntpclient.status=enabled", Effect.UPDATE),
    ("airos-cfg-set base=" + SHA + " syslog.remote.ip=192.168.6.1 syslog.remote.port=514", Effect.UPDATE),
]


@pytest.mark.parametrize("cmd,eff", OK_CMDS)
def test_classify_new_shapes_positive(cmd, eff):
    assert effect_of(cmd) == eff


INJECT = [";/system reboot", "\n/system reboot", " `reboot`", "$(reboot)", "{{ x }}", ",a b", " extra=1", "|x", "&x", "'x"]


@pytest.mark.parametrize("tail", INJECT)
def test_classify_new_shapes_negative_injection(tail):
    for base in ("/system ntp client set enabled=yes servers=a.example",
                 "/system ntp client set enabled=yes primary-ntp=1.2.3.4 secondary-ntp=0.0.0.0",
                 "/system logging action set [find name=netdev-remote] remote=10.0.0.1 remote-port=514"):
        assert effect_of(base + tail) != Effect.UPDATE, base + tail


@pytest.mark.parametrize("cmd", [
    "/system ntp client set enabled=yes servers=",
    "/system ntp client set enabled=yes servers=a_b",
    "/system ntp client set enabled=yes servers=a,,b",
    "/system ntp client set enabled=yes servers=-a",
    "/system ntp client set enabled=maybe servers=a",
    "/system ntp client set servers=a",
    "/system ntp client set enabled=yes primary-ntp=ntp.example secondary-ntp=0.0.0.0",
    "/system ntp client set enabled=yes primary-ntp=1.2.3.4",
    "/system ntp client set mode=broadcast",
    "/system ntp client set enabled=yes servers=a.example mode=broadcast",
    "/system logging action set [find name=remote] remote=10.0.0.1 remote-port=514",
    "/system logging action set [find] remote=10.0.0.1 remote-port=514",
    "/system logging action set [find name=netdev-remote] remote=host remote-port=514",
    "/system logging action set [find name=netdev-remote] remote=10.0.0.1 remote-port=",
    "/system logging action set [find name=netdev-remote] remote=10.0.0.1 remote-port=514 target=disk",
    "/system logging action add name=netdev-remote target=remote remote=10.0.0.1",
    "/system logging add topics=info action=netdev-remote",
    "/system logging action remove [find name=netdev-remote]",
    "/system ntp client servers add address=1.2.3.4",
])
def test_classify_new_shapes_negative_other(cmd):
    assert effect_of(cmd) == Effect.UNKNOWN


# ------------------------------------------------------------------ RouterOS 6 NTP
def test_ros6_ntp_changed_secondary_added():
    dp = plan_of(RouterOsDriver(6), Ntp((A, B)), ros6())
    assert [s.phase for s in dp.steps] == ["precheck", "change", "verify"]
    c = change(dp)[0]
    assert c.command == f"/system ntp client set enabled=yes primary-ntp={A} secondary-ntp={B}"
    assert c.rollback == f"/system ntp client set enabled=yes primary-ntp={A} secondary-ntp=0.0.0.0"
    assert dp.steps[0].expect == f"enabled=yes primary-ntp={A} secondary-ntp=0.0.0.0"
    assert dp.steps[2].expect == f"enabled=yes primary-ntp={A} secondary-ntp={B}"
    assert dp.steps[0].command == dp.steps[2].command == "/system ntp client print"


def test_ros6_ntp_unchanged_and_secondary_missing():
    dp = plan_of(RouterOsDriver(6), Ntp((A,)), ros6())          # one server, secondary stays 0.0.0.0
    assert dp.changes == 0 and dp.unchanged == [f"ntp primary-ntp={A} secondary-ntp=0.0.0.0"]


def test_ros6_ntp_one_server_clears_old_secondary():
    text = samples.NTP_PRINT_6.replace("secondary-ntp: 0.0.0.0", "secondary-ntp: 9.9.9.9")
    c = change(plan_of(RouterOsDriver(6), Ntp((A,)), ros6(text)))[0]
    assert c.command.endswith(f"primary-ntp={A} secondary-ntp=0.0.0.0")
    assert c.rollback.endswith(f"primary-ntp={A} secondary-ntp=9.9.9.9")


def test_ros6_ntp_disabled_gets_enabled_and_rollback_disables():
    text = samples.NTP_PRINT_6.replace("enabled: yes", "enabled: no")
    c = change(plan_of(RouterOsDriver(6), Ntp((A,)), ros6(text)))[0]
    assert c.command.startswith("/system ntp client set enabled=yes ")
    assert c.rollback.startswith("/system ntp client set enabled=no ")


def test_ros6_ntp_detail_output_is_equivalent():
    a = plan_of(RouterOsDriver(6), Ntp((A, B)), ros6())
    b = plan_of(RouterOsDriver(6), Ntp((A, B)), ros6(samples.NTP_DETAIL_6))
    assert [s.command for s in a.steps] == [s.command for s in b.steps]


def test_ros6_ntp_hostname_and_three_servers_unsupported():
    dp = plan_of(RouterOsDriver(6), Ntp(("tik.skamanet.cz",)), ros6())
    assert dp.steps == [] and "IPv4 addresses only" in dp.unsupported[0]
    dp = plan_of(RouterOsDriver(6), Ntp((A, B, "10.0.0.1")), ros6())
    assert dp.steps == [] and "2 servers" in dp.unsupported[0]


def test_ros6_ntp_dns_names_noted_and_odd_mode_noted():
    text = samples.NTP_PRINT_6.replace("server-dns-names: ", "server-dns-names: pool.ntp.org").replace("unicast", "broadcast")
    dp = plan_of(RouterOsDriver(6), Ntp((A, B)), ros6(text))
    assert any("server-dns-names=pool.ntp.org" in n for n in dp.notes) and any("mode is 'broadcast'" in n for n in dp.notes)


def test_ros_ntp_unreadable_output_is_unsupported():
    dp = plan_of(RouterOsDriver(6), Ntp((A,)), ros6("bad command name\n"))
    assert dp.steps == [] and dp.unsupported
    dp = plan_of(RouterOsDriver(7), Ntp((A,)), ros7("enabled: yes\n mode: unicast\n"))
    assert dp.steps == [] and "neither" in dp.unsupported[0]


# ------------------------------------------------------------------ RouterOS 7 NTP
@pytest.mark.parametrize("text", [samples.NTP_PRINT_7, samples.NTP_DETAIL_7])
def test_ros7_ntp_unchanged(text):
    dp = plan_of(detect_driver(ros7(text)), Ntp((A, B)), ros7(text))
    assert dp.driver == "routeros7" and dp.changes == 0 and dp.unchanged == [f"ntp servers={A},{B}"]


@pytest.mark.parametrize("text", [samples.NTP_PRINT_7, samples.NTP_DETAIL_7])
def test_ros7_ntp_changed_with_hostname(text):
    dp = plan_of(RouterOsDriver(7), Ntp((A, "tik.skamanet.cz")), ros7(text))
    c = change(dp)[0]
    assert c.command == f"/system ntp client set enabled=yes servers={A},tik.skamanet.cz"
    assert c.rollback == f"/system ntp client set enabled=yes servers={A},{B}"
    assert dp.steps[0].expect == f"enabled=yes servers={A},{B}"


def test_ros7_ntp_order_matters_and_case_is_ignored():
    assert plan_of(RouterOsDriver(7), Ntp((B, A)), ros7()).changes == 1
    text = "enabled: yes\nmode: unicast\nservers: TIK.Skamanet.cz\nvrf: main\n"
    assert plan_of(RouterOsDriver(7), Ntp(("tik.skamanet.cz",)), ros7(text)).changes == 0


def test_ros7_ntp_empty_servers_no_rollback_and_odd_chars_unsupported():
    dp = plan_of(RouterOsDriver(7), Ntp((A,)), ros7("enabled: no\nmode: unicast\nservers: \nvrf: main\n"))
    c = change(dp)[0]
    assert c.rollback is None and any("no automatic rollback" in n for n in dp.notes)
    dp = plan_of(RouterOsDriver(7), Ntp((A,)), ros7("enabled: yes\nservers: 2001:db8::1\n"))
    assert dp.steps == [] and "outside" in dp.unsupported[0]


def test_ros7_pre_715_prints_primary_ntp_and_uses_the_legacy_shape():
    dp = plan_of(RouterOsDriver(7), Ntp((A, B)), ros7(samples.NTP_PRINT_6))
    assert change(dp)[0].command == f"/system ntp client set enabled=yes primary-ntp={A} secondary-ntp={B}"


# ------------------------------------------------------------------ airOS NTP / syslog
def test_airos_ntp_hostname_unchanged():
    dp = plan_of(AirosDriver(), Ntp(("tik.skamanet.cz",)), xw())
    assert dp.changes == 0 and dp.unchanged == ["ntp tik.skamanet.cz"]


def test_airos_ntp_changed_and_second_server_creates_keys():
    t = xw("ntpclient.2.status=disabled\n")        # the pair is created while its status is NOT enabled: a rollback leaves it inert
    dp = plan_of(AirosDriver(), Ntp((A, B)), t)
    c = change(dp)[0]
    base = dp.state_fingerprint
    assert c.command == (f"airos-cfg-set base={base} ntpclient.1.server={A} ntpclient.2.server={B} ntpclient.2.status=enabled")
    assert c.rollback == "airos-cfg-set base=current ntpclient.1.server=tik.skamanet.cz ntpclient.2.status=disabled"
    assert any("ntpclient.2.server" in n and "was unset" in n for n in dp.notes)
    assert dp.steps[2].expect == c.command.split(" ", 2)[2]


@pytest.mark.parametrize("extra", ["", "ntpclient.2.status=enabled\n", "ntpclient.2.status=\n"])
def test_airos_refuses_to_create_a_key_a_rollback_would_leave_active(extra):
    dp = plan_of(AirosDriver(), Ntp((A, B)), xw(extra))
    assert dp.steps == [] and "ntpclient.2.server does not exist yet" in dp.unsupported[0] and "rollback would leave" in dp.unsupported[0]


def test_airos_syslog_refuses_creating_remote_keys_while_enabled_or_unknown():
    for drop in (("syslog.remote.ip",), ("syslog.remote.port",)):
        dp = plan_of(AirosDriver(), Syslog("192.168.6.1", 514), xw(drop=drop))
        assert dp.steps == [] and "rollback would leave" in dp.unsupported[0]
    cfg = samples.SYSTEM_CFG_XW.replace("syslog.remote.status=enabled", "syslog.remote.status=disabled")
    cfg = "\n".join(l for l in cfg.splitlines() if not l.startswith("syslog.remote.ip"))
    dp = plan_of(AirosDriver(), Syslog("192.168.6.1"), samples.airos_cfg_transport(cfg + "\n"))
    assert change(dp)[0].rollback.endswith("syslog.remote.status=disabled")


def test_airos_ntp_enables_missing_status_keys():
    dp = plan_of(AirosDriver(), Ntp(("tik.skamanet.cz",)), xw(drop=("ntpclient.status", "ntpclient.1.status")))
    assert change(dp)[0].command.endswith("ntpclient.1.status=enabled ntpclient.status=enabled")


def test_airos_ntp_stale_second_server_and_too_many():
    dp = plan_of(AirosDriver(), Ntp((A,)), xw("ntpclient.2.server=old.example\n"))
    assert any("ntpclient.2.server" in u and "clearing" in u for u in dp.unsupported)
    dp = plan_of(AirosDriver(), Ntp((A, B, "10.0.0.1")), xw())
    assert dp.steps == [] and "holds 2 servers" in dp.unsupported[0]


def test_airos_syslog_unchanged_and_changed():
    assert plan_of(AirosDriver(), Syslog("192.168.6.1"), xw()).unchanged == ["syslog 192.168.6.1:514"]
    dp = plan_of(AirosDriver(), Syslog("10.9.9.9", 1514), xw())
    c = change(dp)[0]
    assert c.command.endswith("syslog.remote.ip=10.9.9.9 syslog.remote.port=1514")
    assert c.rollback == "airos-cfg-set base=current syslog.remote.ip=192.168.6.1 syslog.remote.port=514"


def test_airos_syslog_enables_when_disabled():
    t = samples.airos_cfg_transport(samples.SYSTEM_CFG_XW.replace("syslog.remote.status=enabled", "syslog.remote.status=disabled"))
    c = change(plan_of(AirosDriver(), Syslog("192.168.6.1"), t))[0]
    assert c.command.endswith("syslog.remote.status=enabled") and c.rollback.endswith("syslog.remote.status=disabled")


def test_airos_plans_hold_no_secrets():
    for intent in (Ntp((A, B)), Syslog("10.9.9.9")):
        blob = json.dumps(plan_of(AirosDriver(), intent, xw()).__dict__, default=lambda o: o.__dict__)
        assert "SuperSecretPsk" not in blob and "$1$hash" not in blob and "GMT+12" not in blob


# ------------------------------------------------------------------ RouterOS syslog
def test_ros_syslog_unsupported_when_action_missing():
    dp = plan_of(RouterOsDriver(7), Syslog("10.9.9.9"), samples.ros_syslog_transport(samples.LOG_ACTIONS))
    assert dp.steps == [] and dp.changes == 0
    assert "netdev-remote" in dp.unsupported[0] and "creation of logging actions/rules is not supported" in dp.unsupported[0]


def test_ros_syslog_updates_existing_action_only():
    dp = plan_of(RouterOsDriver(7), Syslog("192.168.6.1"), samples.ros_syslog_transport(samples.LOG_ACTIONS_NETDEV))
    c = change(dp)[0]
    assert c.command == "/system logging action set [find name=netdev-remote] remote=192.168.6.1 remote-port=514"
    assert c.rollback == "/system logging action set [find name=netdev-remote] remote=10.9.9.9 remote-port=1514"
    assert dp.steps[0].command == dp.steps[2].command == "/system logging action print detail without-paging where name=netdev-remote"
    assert dp.steps[0].expect == "remote=10.9.9.9 remote-port=1514" and dp.steps[2].expect == "remote=192.168.6.1 remote-port=514"
    assert not any("action=remote" in s.command or "logging add" in s.command for s in dp.steps)    # rules untouched


def test_ros_syslog_unchanged_and_wrong_target():
    dp = plan_of(RouterOsDriver(7), Syslog("10.9.9.9", 1514), samples.ros_syslog_transport(samples.LOG_ACTIONS_NETDEV))
    assert dp.changes == 0 and dp.unchanged
    bad = samples.LOG_ACTIONS_NETDEV.replace('name="netdev-remote" target=remote', 'name="netdev-remote" target=disk')
    dp = plan_of(RouterOsDriver(6), Syslog("10.9.9.9"), samples.ros_syslog_transport(bad))
    assert dp.steps == [] and "not remote" in dp.unsupported[0]


def test_ros_syslog_existing_remote_action_is_never_touched():
    # the fleet's existing action is `remote`; asking for a different host must not produce a command for it
    dp = plan_of(RouterOsDriver(7), Syslog("10.9.9.9"), samples.ros_syslog_transport(samples.LOG_ACTIONS))
    assert all("name=remote" not in s.command for s in dp.steps)


# ------------------------------------------------------------------ renderer
def ntp_plan() -> Plan:
    intent = Ntp((A, B), reason="demo")
    devs = [plan_of(RouterOsDriver(6), intent, ros6(), "R6"),
            plan_of(RouterOsDriver(7), Ntp((A, "tik.skamanet.cz"), reason="demo"), ros7(), "R7"),
            plan_of(AirosDriver(), intent, xw("ntpclient.2.status=disabled\n"), "AP1")]
    return Plan(intent.as_dict(), devs)


def syslog_plan() -> Plan:
    intent = Syslog("192.168.6.1", 514, reason="demo")
    devs = [plan_of(RouterOsDriver(7), intent, samples.ros_syslog_transport(samples.LOG_ACTIONS_NETDEV), "R7"),
            plan_of(RouterOsDriver(7), intent, samples.ros_syslog_transport(samples.LOG_ACTIONS), "R7-nolog"),
            plan_of(AirosDriver(), Syslog("10.9.9.9", 1514), xw(), "AP1")]
    return Plan(intent.as_dict(), devs)


def doc_of(plan):
    return json.loads(plan.to_json("2026-10-23T00:00:00+00:00"))


def leaves(tasks):
    for t in tasks:
        if "block" in t:
            yield from leaves(t["block"])
            yield from leaves(t.get("rescue", []))
        else:
            yield t


def mods(text, module):
    return [t[module] for t in leaves(yaml.safe_load(text)[0]["tasks"]) if module in t]


def test_render_ntp_mapping():
    text = render_playbook(ntp_plan())
    modify = mods(text, "community.routeros.api_modify")
    assert len(modify) == 4                                      # R6 change + rollback, R7 change + rollback
    r6, r6_back, r7, r7_back = modify
    assert r6["path"] == "system ntp client" and r6["data"] == [{"enabled": True, "primary-ntp": A, "secondary-ntp": B}]
    assert r6_back["data"] == [{"enabled": True, "primary-ntp": A, "secondary-ntp": "0.0.0.0"}]
    assert r7["data"] == [{"enabled": True, "servers": f"{A},tik.skamanet.cz"}]
    assert r7_back["data"] == [{"enabled": True, "servers": f"{A},{B}"}]
    assert all(m["handle_absent_entries"] == "ignore" and m["handle_entries_content"] == "ignore" for m in modify)
    infos = mods(text, "community.routeros.api_info")
    assert len(infos) == 4 and all(i["path"] == "system ntp client" for i in infos)
    asserts = [c for a in mods(text, "ansible.builtin.assert") for c in a["that"]]
    assert any("['secondary-ntp']" in c and "'0.0.0.0'" in c for c in asserts)       # R6 precheck
    assert any(".servers" in c and f"'{A},{B}'" in c for c in asserts)               # R7 precheck
    air = mods(text, "bonis.netdev.airos_cfg")
    assert any(a["settings"].get("ntpclient.2.server") == B for a in air)


def test_render_syslog_mapping_requires_existing_action():
    text = render_playbook(syslog_plan())
    modify = mods(text, "community.routeros.api_modify")
    assert len(modify) == 2 and all(m["path"] == "system logging action" for m in modify)
    assert modify[0]["data"] == [{"name": "netdev-remote", "target": "remote", "remote": "192.168.6.1", "remote-port": 514}]
    assert modify[1]["data"][0]["remote"] == "10.9.9.9" and modify[1]["data"][0]["remote-port"] == 1514
    text_all = text
    assert "R7-nolog" not in text_all                                    # unsupported device renders nothing
    asserts = [c for a in mods(text, "ansible.builtin.assert") for c in a["that"]]
    assert any("selectattr('name', 'equalto', 'netdev-remote')" in c and "length == 1" in c for c in asserts)
    assert any("target" in c and "'remote'" in c for c in asserts)


@pytest.mark.parametrize("mk", [ntp_plan, syslog_plan])
@pytest.mark.parametrize("check_only", [True, False])
def test_verify_roundtrip_and_determinism(mk, check_only):
    text = render_playbook(mk(), check_only=check_only)
    assert text == render_playbook(mk(), check_only=check_only)
    assert verify_playbook(doc_of(mk()), text) == []


@pytest.mark.parametrize("mk", [ntp_plan, syslog_plan])
def test_every_task_is_check_mode_in_check_only(mk):
    for t in leaves(yaml.safe_load(render_playbook(mk()))[0]["tasks"]):
        assert t["check_mode"] is True


def edited(plan, fn):
    play = yaml.safe_load(render_playbook(plan))
    fn(play[0])
    return yaml.safe_dump(play, sort_keys=False)


def find_leaf(play, module, key, value):
    return next(t for t in leaves(play["tasks"]) if module in t and t[module].get(key) == value)


def test_verify_rejects_tampered_ntp_and_syslog_playbooks():
    p = ntp_plan()
    v = verify_playbook(doc_of(p), edited(p, lambda pl: find_leaf(pl, "community.routeros.api_modify", "path", "system ntp client")
                                          ["community.routeros.api_modify"]["data"][0].update(servers="9.9.9.9")))
    assert any("differs" in x for x in v)
    p = syslog_plan()
    v = verify_playbook(doc_of(p), edited(p, lambda pl: find_leaf(pl, "community.routeros.api_modify", "path", "system logging action")
                                          ["community.routeros.api_modify"]["data"][0].update(name="remote")))
    assert any("differs" in x for x in v)
    v = verify_playbook(doc_of(p), edited(p, lambda pl: find_leaf(pl, "community.routeros.api_modify", "path", "system logging action")
                                          ["community.routeros.api_modify"].update(path="system logging")))
    assert v


def test_verify_rejects_edited_plan_commands():
    p = ntp_plan()
    d = doc_of(p)
    d["devices"][1]["steps"][1]["command"] = "/system ntp client set enabled=yes servers=evil.example"
    assert any("modified" in x or "plan_id" in x for x in verify_playbook(d, render_playbook(p)))


def test_renderer_denies_unmapped_shapes():
    # a classified-but-unmapped RouterOS command stays a RenderError; ports outside 1-65535 and bad octets are not mapped
    dp = plan_of(RouterOsDriver(7), Syslog("192.168.6.1"), samples.ros_syslog_transport(samples.LOG_ACTIONS_NETDEV))
    dp.steps[1].command = "/system logging action set [find name=netdev-remote] remote=192.168.6.1 remote-port=99999"
    with pytest.raises(RenderError, match="no Ansible mapping"):
        render_playbook(Plan(Syslog("192.168.6.1").as_dict(), [dp]))
    dp = plan_of(RouterOsDriver(7), Ntp((A,)), ros7())
    dp.steps[0].expect = "enabled=yes servers=a;b"
    with pytest.raises(RenderError, match="cannot render expect"):
        render_playbook(Plan(Ntp((A,)).as_dict(), [dp]))
    dp = plan_of(RouterOsDriver(7), Syslog("192.168.6.1"), samples.ros_syslog_transport(samples.LOG_ACTIONS_NETDEV))
    dp.steps[0].expect = "remote=1.2.3.4 remote-port=514 target=disk"
    with pytest.raises(RenderError, match="cannot render expect"):
        render_playbook(Plan(Syslog("192.168.6.1").as_dict(), [dp]))


def test_renderer_refuses_jinja_in_airos_ntp_value():
    dp = plan_of(AirosDriver(), Ntp(("tik.skamanet.cz",)), xw())
    dp2 = plan_of(AirosDriver(), Ntp((A,)), xw())
    dp2.steps[1].command = dp2.steps[1].command + "{{x}}"
    with pytest.raises(RenderError, match="template syntax"):
        render_playbook(Plan(Ntp((A,)).as_dict(), [dp2]))
    assert dp.changes == 0


def test_golden_ntp_and_syslog():
    assert render_playbook(ntp_plan()) == open(GOLDEN_NTP, encoding="utf-8").read()
    assert render_playbook(syslog_plan()) == open(GOLDEN_SYSLOG, encoding="utf-8").read()


ANSIBLE = os.environ.get("NETDEV_ANSIBLE_VENV") or shutil.which("ansible-playbook")


@pytest.mark.skipif(not ANSIBLE, reason="ansible-playbook not available")
def test_ansible_syntax_check_ntp_syslog(tmp_path):
    exe = os.path.join(ANSIBLE, "ansible-playbook") if os.path.isdir(ANSIBLE) else ANSIBLE
    stub = tmp_path / "collections" / "ansible_collections" / "bonis" / "netdev"
    (stub / "plugins" / "modules").mkdir(parents=True)
    (stub / "galaxy.yml").write_text("namespace: bonis\nname: netdev\nversion: 0.0.0\nauthors: [x]\nreadme: README.md\n")
    (stub / "plugins" / "modules" / "airos_cfg.py").write_text(
        "DOCUMENTATION = '''\nmodule: airos_cfg\nshort_description: stub\noptions: {}\n'''\n")
    pb = tmp_path / "playbook.yml"
    for mk in (ntp_plan, syslog_plan):
        for mode in (True, False):
            pb.write_text(render_playbook(mk(), check_only=mode))
            env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": f"{tmp_path / 'collections'}:{os.path.expanduser('~/.ansible/collections')}",
                   "ANSIBLE_LOCAL_TEMP": str(tmp_path / "tmp"), "ANSIBLE_HOME": str(tmp_path / "home")}
            r = subprocess.run([exe, "--syntax-check", str(pb)], capture_output=True, text=True, env=env, cwd=tmp_path)
            assert r.returncode == 0, r.stdout + r.stderr


# ------------------------------------------------------------------ plan summary, step rules
def test_plan_summary_names_the_syslog_host():
    assert "192.168.6.1" in syslog_plan().summary().splitlines()[0]
    assert str([A, B]) in ntp_plan().summary().splitlines()[0]


def test_step_refuses_wrong_phase_for_new_shapes():
    with pytest.raises(ValueError):
        Step("verify", "/system ntp client set enabled=yes servers=a.example", "x")
    with pytest.raises(ValueError):
        Step("change", "/system ntp client print", "x")
    with pytest.raises(ValueError):
        Step("change", "/system ntp client set enabled=yes servers=a.example", "x", rollback="/system ntp client print")


# ------------------------------------------------------------------ templates and CLI
def test_seed_templates_compile_ok_for_ntp_and_syslog():
    cfg = load_config(SEED, os.path.join(SEED, "devices.example.yml"))
    res = compile_config(cfg)
    by = {(r.device, r.feature): r for r in res}
    assert all(r.status == OK for r in res if r.feature in ("ntp-standard", "syslog-standard"))
    assert any(r.feature == "syslog-standard" for r in res)
    ntp = dict(ntp_intents(res))
    sl = dict(syslog_intents(res))
    assert ntp and sl and all(isinstance(i, Ntp) for i in ntp.values()) and all(isinstance(i, Syslog) for i in sl.values())
    assert by  # not empty


def test_seed_demo_inventory_ntp_syslog_ok_even_when_wifi_invalid():
    res = compile_config(load_config(SEED))
    st = {(r.device, r.feature): r.status for r in res}
    assert st[("rt-site-b-1", "wifi-ap-5g")] == "invalid"
    assert st[("rt-site-b-1", "ntp-standard")] == "ok" and st[("rt-site-b-1", "syslog-standard")] == "ok"
    assert st[("ap-site-a-1", "wifi-ap-5g")] == "n/a"        # wifi stays "no driver"


def test_template_intents_raise_config_error_on_bad_values(tmp_path):
    from bonis_netdev.features import ConfigError
    from bonis_netdev.templates import Result
    bad = Result("d", "m", "syslog-standard", OK, "", {"syslog_host": "tik.example"}, "syslog")
    with pytest.raises(ConfigError, match="d: feature syslog-standard"):
        syslog_intents([bad])
    bad = Result("d", "m", "ntp-standard", OK, "", {"servers": ["bad host"]}, "ntp")
    with pytest.raises(ConfigError, match="d: feature ntp-standard"):
        ntp_intents([bad])


def test_cli_loads_ntp_and_syslog_intents(tmp_path):
    root = os.path.join(HERE, "..", "examples")
    n = cli.load_intent(os.path.join(root, "ntp-intent.yml"))
    assert isinstance(n, Ntp) and n.servers == (A, B)
    s = cli.load_intent(os.path.join(root, "syslog-intent.yml"))
    assert isinstance(s, Syslog) and (s.host, s.port) == ("192.168.6.1", 514)
    f = tmp_path / "i.yml"
    for text, msg in (("intent: ntp\nservers: a.example\n", "servers must be a list"),
                      ("intent: ntp\nservers: [a.example]\nserver: x\n", "unknown field server"),
                      ("intent: syslog\nport: 514\n", "host is required"),
                      ("intent: syslog\nhost: 1.2.3.4\nport: 0\n", "port"),
                      ("intent: ntp\nservers: ['a.example\\n']\n", "not an IPv4 address or host name"),
                      ("intent: firewall\n", "must be one of")):
        f.write_text(text)
        with pytest.raises(IntentError, match=msg):
            cli.load_intent(str(f))


def test_cli_plan_writes_ntp_plan(tmp_path, monkeypatch, capsys):
    transports = {"10.0.0.1": ros6(), "10.0.0.2": xw()}
    monkeypatch.setattr(cli, "SshReadTransport", lambda host, creds: transports[host])
    (tmp_path / "creds.yml").write_text("credential_sets: [{username: u, password: p}]\n")
    (tmp_path / "t.yml").write_text("targets:\n  - {name: R, host: 10.0.0.1}\n  - {name: AP, host: 10.0.0.2}\n")
    out = tmp_path / "plan.json"
    rc = cli.main(["plan", "--intent", os.path.join(HERE, "..", "examples", "ntp-intent.yml"), "--targets", str(tmp_path / "t.yml"),
                   "--credentials", str(tmp_path / "creds.yml"), "--out", str(out), "--workers", "1"])
    assert rc == 0
    doc = json.loads(out.read_text())
    assert doc["intent"]["intent"] == "ntp" and doc["writes_allowed"] is False
    assert {d["driver"] for d in doc["devices"]} == {"routeros6", "airos"}
    assert verify_playbook(doc, render_playbook(__import__("bonis_netdev.render", fromlist=["plan_from_dict"]).plan_from_dict(doc))) == []


# ------------------------------------------------------------------ empty server lists (RouterOS factory defaults)
EMPTY7 = "         enabled: no\n            mode: unicast\n         servers: \n             vrf: main\n"
LEGACY_OFF = samples.NTP_PRINT_6.replace("enabled: yes", "enabled: no").replace(f"primary-ntp: {A}", "primary-ntp: 0.0.0.0")


def ntp_roundtrip(driver, transport, intent=Ntp((A,))):
    dp = plan_of(driver, intent, transport)
    plan = Plan(intent.as_dict(), [dp])
    text = render_playbook(plan)
    assert verify_playbook(doc_of(plan), text) == []
    assert verify_playbook(doc_of(plan), render_playbook(plan, check_only=False)) == []
    return dp, text


def test_empty_server_list_ros7_renders_and_verifies():
    dp, text = ntp_roundtrip(RouterOsDriver(7), ros7(EMPTY7))
    assert dp.steps[0].expect == "enabled=no servers=" and change(dp)[0].rollback is None
    asserts = [c for a in mods(text, "ansible.builtin.assert") for c in a["that"]]
    assert any(".servers | default('', true) | string) == ''" in c for c in asserts)         # precheck: really empty
    assert any(f"== '{A}'" in c and ".servers" in c for c in asserts)                          # verify: the new list
    assert any("enabled | default(false) | bool) == false" in c for c in asserts)


def test_empty_server_list_jinja_evaluates_for_missing_none_and_empty():
    jinja2 = pytest.importorskip("jinja2")
    dp, text = ntp_roundtrip(RouterOsDriver(7), ros7(EMPTY7))
    cond = next(c for a in mods(text, "ansible.builtin.assert") for c in a["that"] if "default('', true)" in c and "== ''" in c)
    expr = jinja2.Environment().compile_expression(cond)
    for row in ({"enabled": False}, {"enabled": False, "servers": ""}, {"enabled": False, "servers": None}):
        assert expr(netdev_r_0_1={"result": [row]}) is True
    assert expr(netdev_r_0_1={"result": [{"enabled": False, "servers": "1.2.3.4"}]}) is False


def test_legacy_unset_primary_and_disabled_renders_and_verifies():
    dp, text = ntp_roundtrip(RouterOsDriver(6), ros6(LEGACY_OFF))
    assert dp.steps[0].expect == "enabled=no primary-ntp=0.0.0.0 secondary-ntp=0.0.0.0"
    assert change(dp)[0].rollback == "/system ntp client set enabled=no primary-ntp=0.0.0.0 secondary-ntp=0.0.0.0"
    asserts = [c for a in mods(text, "ansible.builtin.assert") for c in a["that"]]
    assert any("['primary-ntp']" in c and "'0.0.0.0'" in c for c in asserts)
    assert mods(text, "community.routeros.api_modify")[1]["data"] == [{"enabled": False, "primary-ntp": "0.0.0.0", "secondary-ntp": "0.0.0.0"}]


# ------------------------------------------------------------------ unusable addresses
@pytest.mark.parametrize("bad", ["0.0.0.0", "255.255.255.255", "127.0.0.1", "127.255.255.254", "224.0.0.1", "239.255.255.250",
                                 "240.0.0.1", "169.254.1.1", "192.168.06.1"])
def test_unusable_addresses_are_rejected_in_both_intents(bad):
    with pytest.raises(IntentError):
        Ntp((bad,))
    with pytest.raises(IntentError):
        Syslog(bad)


def test_ntp_ipv6_message_is_explicit_and_hostnames_stay_allowed():
    with pytest.raises(IntentError, match="IPv6 addresses are not supported"):
        Ntp(("2001:db8::1",))
    assert Ntp(("tik.skamanet.cz", "10.0.0.1", "192.168.6.1")).servers[0] == "tik.skamanet.cz"


@pytest.mark.parametrize("cmd", [
    "/system ntp client set enabled=yes primary-ntp=256.1.1.1 secondary-ntp=0.0.0.0",
    "/system ntp client set enabled=yes primary-ntp=1.2.3.999 secondary-ntp=0.0.0.0",
    "/system ntp client set enabled=yes primary-ntp=01.2.3.4 secondary-ntp=0.0.0.0",
    "/system logging action set [find name=netdev-remote] remote=10.0.0.1 remote-port=65536",
    "/system logging action set [find name=netdev-remote] remote=10.0.0.1 remote-port=0",
    "/system logging action set [find name=netdev-remote] remote=10.0.0.1 remote-port=99999",
    "/system logging action set [find name=netdev-remote] remote=10.0.0.1 remote-port=0514",
    "/system logging action set [find name=netdev-remote] remote=300.0.0.1 remote-port=514",
])
def test_classify_tightened_octets_and_ports(cmd):
    assert effect_of(cmd) == Effect.UNKNOWN


@pytest.mark.parametrize("port", [1, 9, 10, 514, 9999, 10000, 65535, 65500, 60000])
def test_classify_port_boundaries_accepted(port):
    assert effect_of(f"/system logging action set [find name=netdev-remote] remote=255.255.255.255 remote-port={port}") == Effect.UPDATE
