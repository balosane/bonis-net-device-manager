import json

import pytest

from bonis_netdev.classify import Effect, effect_of
from bonis_netdev.drivers.base import build_device_plan, detect_driver
from bonis_netdev.drivers.airos import AirosDriver
from bonis_netdev.drivers.routeros import RouterOsDriver
from bonis_netdev.intents import DhcpNetworks, Dns, IntentError, Resolver
from bonis_netdev.plan import DevicePlan, Plan, Step
from bonis_netdev.transport import FakeTransport, WriteRefused


def dns_nets(**kw):
    return Dns("192.168.238.119", "192.168.6.1", DhcpNetworks(exclude=("192.168.88.0/24",)), **kw)


# --- intents
def test_intent_validates():
    with pytest.raises(IntentError):
        Dns("not-an-ip")
    with pytest.raises(IntentError):
        Dns("1.1.1.1", "1.1.1.1")
    with pytest.raises(IntentError):
        DhcpNetworks(exclude=("nope",))
    assert Dns("1.1.1.1", "8.8.8.8", extra=("2a09:9c07:6::1",)).servers == ("1.1.1.1", "8.8.8.8", "2a09:9c07:6::1")


# --- classification
@pytest.mark.parametrize("cmd,eff", [
    ("/ip dns print", Effect.READ),
    ("/ip dhcp-server network print detail without-paging", Effect.READ),
    ("/ip dhcp-server network print detail without-paging where address=10.0.0.0/24", Effect.READ),
    ("cat /tmp/system.cfg", Effect.READ),
    ("/ip dhcp-server network set [find address=10.0.0.0/24] dns-server=1.1.1.1,8.8.8.8", Effect.UPDATE),
    ("/ip dns set servers=1.1.1.1", Effect.UPDATE),
    ("airos-cfg-set base=" + "a" * 64 + " resolv.nameserver.1.ip=1.1.1.1", Effect.UPDATE),
    ("airos-cfg-set base=current resolv.nameserver.1.ip=1.1.1.1", Effect.UPDATE),
    ("/system reset-configuration", Effect.UNKNOWN),
    ("/ip dns set servers=1.1.1.1; /system reboot", Effect.UNKNOWN),
    ("/ip dns print\n/system reboot", Effect.UNKNOWN),
    ("cat /tmp/system.cfg `reboot`", Effect.UNKNOWN),
    ("/ip firewall filter remove [find]", Effect.UNKNOWN),
])
def test_classify(cmd, eff):
    assert effect_of(cmd) == eff


def test_step_refuses_unclassified_and_wrong_phase():
    with pytest.raises(ValueError):
        Step("change", "/system reboot", "x")
    with pytest.raises(ValueError):
        Step("change", "/ip dns print", "x")           # change must be an update
    with pytest.raises(ValueError):
        Step("verify", "/ip dns set servers=1.1.1.1", "x")  # verify must be a read


# --- RouterOS
def test_routeros_networks_plan(ros):
    drv = detect_driver(ros)
    assert isinstance(drv, RouterOsDriver) and drv.name == "routeros6"
    dp = build_device_plan(drv, "MOR-ZAH-RT-1", dns_nets(), ros)
    changes = [s for s in dp.steps if s.phase == "change"]
    assert [s.command for s in changes] == [
        "/ip dhcp-server network set [find address=192.168.80.0/26] dns-server=192.168.238.119,192.168.6.1",
        "/ip dhcp-server network set [find address=10.15.5.208/29] dns-server=192.168.238.119,192.168.6.1",
    ]
    assert changes[0].rollback == "/ip dhcp-server network set [find address=192.168.80.0/26] dns-server=192.168.6.1,8.8.8.8"
    assert dp.unchanged == ["212.119.38.192/29 dns-server=192.168.238.119,192.168.6.1"]
    assert any("192.168.88.0/24 excluded" in s for s in dp.skipped)
    assert any("disabled/invalid" in s and "dhcp2" in s for s in dp.skipped)
    assert not any("192.168.88.0/24" in s.command for s in dp.steps)
    # every change is bracketed by a precheck and a verify of the same network
    phases = [s.phase for s in dp.steps]
    assert phases == ["precheck", "change", "verify"] * 2


def test_routeros_nothing_to_do_when_already_set(ros):
    intent = Dns("192.168.6.1", "8.8.8.8", DhcpNetworks(exclude=("192.168.88.0/24", "212.119.38.192/29", "10.15.5.208/29")))
    dp = build_device_plan(RouterOsDriver(6), "x", intent, ros)
    assert dp.changes == 0 and dp.unchanged == ["192.168.80.0/26 dns-server=192.168.6.1,8.8.8.8"]


def test_routeros_resolver(ros):
    dp = build_device_plan(RouterOsDriver(6), "x", Dns("192.168.238.119", "192.168.6.1"), ros)
    ch = [s for s in dp.steps if s.phase == "change"]
    assert [s.command for s in ch] == ["/ip dns set servers=192.168.238.119,192.168.6.1"]
    assert ch[0].rollback == "/ip dns set servers=192.168.6.1,8.8.8.8"


def test_routeros_dns_prefix_exclusion_covers_subnets(ros):
    intent = Dns("1.1.1.1", None, DhcpNetworks(exclude=("192.168.0.0/16",)))
    dp = build_device_plan(RouterOsDriver(6), "x", intent, ros)
    assert [s.command.split("address=")[1].split("]")[0] for s in dp.steps if s.phase == "change"] == ["212.119.38.192/29", "10.15.5.208/29"]


# --- airOS
def test_airos_plan_has_no_secrets(airos):
    drv = detect_driver(airos)
    assert isinstance(drv, AirosDriver)
    dp = build_device_plan(drv, "AP-1", Dns("192.168.238.119", "192.168.6.1"), airos)
    ch = [s for s in dp.steps if s.phase == "change"]
    assert len(ch) == 1
    assert ch[0].command.endswith("resolv.nameserver.1.ip=192.168.238.119 resolv.nameserver.2.ip=192.168.6.1")
    assert ch[0].rollback == "airos-cfg-set base=current resolv.nameserver.1.ip=192.168.6.1"
    blob = json.dumps(dp.__dict__, default=lambda o: o.__dict__)
    assert "SuperSecretPsk" not in blob and "$1$hash" not in blob


def test_airos_dhcp_scope_unsupported(airos):
    dp = build_device_plan(AirosDriver(), "AP-1", dns_nets(), airos)
    assert dp.steps == [] and "no DHCP server" in dp.unsupported[0]


def test_airos_unchanged(airos):
    dp = build_device_plan(AirosDriver(), "AP-1", Dns("192.168.6.1"), airos)
    assert dp.changes == 0 and dp.unchanged


# --- plan
def make_plan(ros):
    dp = build_device_plan(RouterOsDriver(6), "MOR-ZAH-RT-1", dns_nets(), ros)
    return Plan(dns_nets().as_dict(), [dp, DevicePlan("DOWN-1", "10.0.0.99", "?", "", error="timeout")])


def test_plan_is_deterministic(ros):
    a, b = make_plan(ros), make_plan(ros)
    assert a.plan_id == b.plan_id
    assert a.to_json("t1") != a.to_json("t2") and a.plan_id == json.loads(a.to_json("t2"))["plan_id"]
    assert json.loads(a.to_json())["writes_allowed"] is False


def test_plan_id_changes_with_content(ros):
    a = make_plan(ros)
    a.devices[0].steps[1].command = "/ip dns set servers=9.9.9.9"
    assert a.plan_id != make_plan(ros).plan_id


# --- transport
def test_read_transport_refuses_writes(ros):
    with pytest.raises(WriteRefused):
        ros.read("/ip dns set servers=1.1.1.1")
    with pytest.raises(WriteRefused):
        FakeTransport("h", {}).read("/system reboot")


def test_empty_previous_value_is_noted_not_rolled_back(ros):
    dp = build_device_plan(RouterOsDriver(6), "x", Dns("1.1.1.1", None, DhcpNetworks()), ros)
    assert any("192.168.88.0/24" in n and "no automatic rollback" in n for n in dp.notes)
    c = next(s for s in dp.steps if s.phase == "change" and "192.168.88.0/24" in s.command)
    assert c.rollback is None
