import json

import yaml

from bonis_netdev import cli
from bonis_netdev.drivers.base import build_device_plan
from bonis_netdev.drivers.routeros import RouterOsDriver
from bonis_netdev.plan import Plan


def test_load_intent(tmp_path):
    p = tmp_path / "i.yml"
    p.write_text(yaml.safe_dump({"intent": "dns", "primary": "1.1.1.1", "secondary": "8.8.8.8",
                                 "scope": {"kind": "dhcp-networks", "exclude": ["192.168.88.0/24"]},
                                 "reason": "wave1"}))
    i = cli.load_intent(str(p))
    assert i.servers == ("1.1.1.1", "8.8.8.8") and i.scope.exclude == ("192.168.88.0/24",)


def test_show_and_check_detect_tampering(tmp_path, ros, capsys):
    from bonis_netdev.intents import DhcpNetworks, Dns
    intent = Dns("192.168.238.119", "192.168.6.1", DhcpNetworks(exclude=("192.168.88.0/24",)))
    plan = Plan(intent.as_dict(), [build_device_plan(RouterOsDriver(6), "MOR-ZAH-RT-1", intent, ros)])
    f = tmp_path / "plan.json"
    f.write_text(plan.to_json("2026-10-21T00:00:00+00:00"))
    assert cli.main(["check", str(f)]) == 0
    assert cli.main(["show", str(f)]) == 0
    out = capsys.readouterr().out
    assert "dns-server=192.168.238.119,192.168.6.1" in out and "rollback:" in out
    doc = json.loads(f.read_text())
    doc["devices"][0]["steps"][1]["command"] = "/ip dns set servers=9.9.9.9"
    f.write_text(json.dumps(doc))
    assert cli.main(["check", str(f)]) == 2


def test_plan_command_reports_unreachable_device(tmp_path, monkeypatch):
    for n, c in (("i.yml", {"intent": "dns", "primary": "1.1.1.1"}),
                 ("t.yml", {"targets": [{"name": "X", "host": "10.9.9.9"}]}),
                 ("c.yml", {"credential_sets": [{"username": "u", "password": "p"}]})):
        (tmp_path / n).write_text(yaml.safe_dump(c))

    def boom(self, command):
        raise RuntimeError("unreachable")
    monkeypatch.setattr(cli.SshReadTransport, "read", boom)
    out = tmp_path / "plan.json"
    rc = cli.main(["plan", "--intent", str(tmp_path / "i.yml"), "--targets", str(tmp_path / "t.yml"),
                   "--credentials", str(tmp_path / "c.yml"), "--out", str(out)])
    assert rc == 1
    assert json.loads(out.read_text())["devices"][0]["error"].startswith("RuntimeError")
