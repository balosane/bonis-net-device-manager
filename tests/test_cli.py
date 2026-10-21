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


def test_selftest_and_demo_pass(capsys):
    assert cli.main(["selftest"]) == 0
    assert "12/12 checks passed" in capsys.readouterr().out
    assert cli.main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "DEMO-ROUTER" in out and "plan_id" in out and "SuperSecretPsk" not in out


def test_try_uses_fake_ssh_and_excludes_factory_lan(tmp_path, monkeypatch, capsys):
    from bonis_netdev import samples
    (tmp_path / "c.yml").write_text(yaml.safe_dump({"credential_sets": [{"username": "u", "password": "p"}]}))
    t = samples.ros_transport("10.0.0.23")
    monkeypatch.setattr(cli.SshReadTransport, "read", lambda self, cmd: t.read(cmd))
    monkeypatch.setattr(cli.SshReadTransport, "host", "10.0.0.23", raising=False)
    out = tmp_path / "p.json"
    rc = cli.main(["try", "10.0.0.23", "--dns", "192.168.238.119,192.168.6.1", "--credentials", str(tmp_path / "c.yml"),
                   "--out", str(out)])
    assert rc == 0
    text = capsys.readouterr().out
    assert "2 change(s)" in text and "192.168.88.0/24 excluded" in text
    assert cli.main(["check", str(out)]) == 0


def test_try_rejects_bad_dns(tmp_path, capsys):
    (tmp_path / "c.yml").write_text(yaml.safe_dump({"credential_sets": []}))
    assert cli.main(["try", "1.2.3.4", "--dns", "nonsense", "--credentials", str(tmp_path / "c.yml")]) == 2


def test_missing_credentials_is_a_clear_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NETDEV_CREDENTIALS", raising=False)
    assert cli.main(["probe", "1.2.3.4"]) == 2
    assert "no credentials file" in capsys.readouterr().err
