import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from bonis_netdev import cli
from bonis_netdev.cli import load_intent
from fakenetbox import FakeNetBox, dev

SEED = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("NB_TEST_TOKEN", "s3cret-token")


def args(srv, *extra):
    return ["reconcile", "--netbox", srv.url, "--token-env", "NB_TEST_TOKEN", "--config", str(SEED), *extra]


def test_exit_1_on_problems_and_output(capsys):
    with FakeNetBox() as srv:
        assert cli.main(args(srv)) == 1
    out = capsys.readouterr().out
    assert "core-1" in out and "dns-standard" in out
    assert "PROBLEM  no-ip: no primary_ip4" in out
    assert "4 managed, 4 with problems, 2 unmanaged" in out
    assert "s3cret-token" not in out


def test_json_output_deterministic(capsys):
    with FakeNetBox() as srv:
        cli.main(args(srv, "--json"))
        first = capsys.readouterr().out
        cli.main(args(srv, "--json"))
        assert capsys.readouterr().out == first
    doc = json.loads(first)
    assert doc["unmanaged_count"] == 2
    assert [p["device"] for p in doc["problems"]] == ["bad-tags", "generic-sw", "no-ip", "unknown-model"]
    keys = [(r["device"], r["feature"]) for r in doc["results"]]
    assert keys == sorted(keys) and ("core-1", "dns-standard") in keys
    assert {r["result"] for r in doc["results"]} == {"ok", "n/a"}


def clean_devices():
    return [dev(1, "core-1", 1, ["tpl-core-router"]), dev(2, "radio-1", 2, ["tpl-core-router"], role="ptp"),
            dev(3, "core-2", 1, ["feat-dns-standard"]), dev(4, "other", 1, [])]


def test_clean_run_exit_0(capsys):
    with FakeNetBox(clean_devices()) as srv:
        assert cli.main(args(srv)) == 0
    assert "3 managed, 0 with problems, 1 unmanaged" in capsys.readouterr().out


def test_invalid_capability_exit_1(capsys):
    with FakeNetBox([dev(1, "r", 1, ["tpl-ap-5g"])]) as srv:       # hEX S has no wifi.5ghz.ap
        assert cli.main(args(srv)) == 1
    assert "invalid" in capsys.readouterr().out


def test_netbox_error_exit_1_no_token_leak(capsys):
    with FakeNetBox(token="other") as srv:
        assert cli.main(args(srv)) == 1
    err = capsys.readouterr().err
    assert "HTTP 403" in err and "s3cret-token" not in err


def test_missing_token_env_exit_1(monkeypatch, capsys):
    monkeypatch.delenv("NB_TEST_TOKEN")
    with FakeNetBox() as srv:
        assert cli.main(args(srv)) == 1
        assert srv.requests == []
    assert "NB_TEST_TOKEN" in capsys.readouterr().err


def test_config_error_exit_1(tmp_path, capsys):
    shutil.copytree(SEED, tmp_path / "c")
    (tmp_path / "c" / "templates" / "default.yml").write_text("x: [nope]\n")
    with FakeNetBox() as srv:
        assert cli.main(["reconcile", "--netbox", srv.url, "--token-env", "NB_TEST_TOKEN", "--config", str(tmp_path / "c")]) == 1
        assert srv.requests == []
    assert "unknown feature tag" in capsys.readouterr().err


def test_missing_required_param_becomes_problem(tmp_path, capsys):
    shutil.copytree(SEED, tmp_path / "c")
    (tmp_path / "c" / "vars" / "global.yml").write_text("scope: resolver\n")      # no primary anywhere
    with FakeNetBox(clean_devices()) as srv:
        assert cli.main(["reconcile", "--netbox", srv.url, "--token-env", "NB_TEST_TOKEN", "--config", str(tmp_path / "c")]) == 1
    assert "required param 'primary'" in capsys.readouterr().out


def test_token_kind_and_host_header_passed():
    with FakeNetBox(clean_devices()) as srv:
        cli.main(args(srv, "--token-kind", "token", "--host-header", "ipam2.skamanet.cz"))
        h = srv.requests[0][3]
        assert h["Authorization"] == "Token s3cret-token" and h["Host"] == "ipam2.skamanet.cz"
        assert {r[0] for r in srv.requests} == {"GET"}


def test_emit_round_trip(tmp_path, capsys):
    out = tmp_path / "emit"
    with FakeNetBox(clean_devices()) as srv:
        assert cli.main(args(srv, "--emit", str(out))) == 0
    idx = yaml.safe_load((out / "index.yml").read_text())["groups"]
    assert len(idx) == 2
    assert sorted(f.name for f in out.iterdir()) == ["dns-1.yml", "dns-2.yml", "index.yml", "targets-1.yml", "targets-2.yml"]
    by_scope = {}
    for g in idx:
        intent = load_intent(str(out / g["intent"]))                 # the existing `netdev plan` loader
        targets = yaml.safe_load((out / g["targets"]).read_text())["targets"]
        by_scope[intent.scope.as_dict()["kind"]] = (intent, targets)
        assert [t["name"] for t in targets] == g["devices"]
    intent, targets = by_scope["dhcp-networks"]
    assert intent.primary == "192.168.238.119" and intent.scope.exclude == ("192.168.88.0/24",)
    assert targets == [{"name": "core-1", "host": "10.0.0.1"}, {"name": "core-2", "host": "10.0.0.3"}]
    assert by_scope["resolver"][1] == [{"name": "radio-1", "host": "10.0.0.2"}]


def test_emit_single_group_names(tmp_path):
    out = tmp_path / "e"
    with FakeNetBox([dev(1, "core-1", 1, ["tpl-core-router"])]) as srv:
        assert cli.main(args(srv, "--emit", str(out))) == 0
    assert sorted(f.name for f in out.iterdir()) == ["dns-intent.yml", "index.yml", "targets.yml"]
    assert load_intent(str(out / "dns-intent.yml")).primary == "192.168.238.119"


def test_emit_refuses_non_empty_dir_before_any_request(tmp_path, capsys):
    out = tmp_path / "e"
    out.mkdir()
    (out / "keep.txt").write_text("x")
    with FakeNetBox(clean_devices()) as srv:
        assert cli.main(args(srv, "--emit", str(out))) == 1
        assert srv.requests == []
    assert os.listdir(out) == ["keep.txt"]
    assert "not an empty directory" in capsys.readouterr().err
    # an existing EMPTY directory is fine
    out2 = tmp_path / "e2"
    out2.mkdir()
    with FakeNetBox(clean_devices()) as srv:
        assert cli.main(args(srv, "--emit", str(out2))) == 0
    assert (out2 / "index.yml").exists()


def test_reconcile_writes_nothing_without_emit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with FakeNetBox(clean_devices()) as srv:
        cli.main(args(srv))
    assert list(tmp_path.iterdir()) == []


def test_reconcile_never_runs_plan(monkeypatch):
    monkeypatch.setattr(cli, "plan_target", lambda *a, **k: pytest.fail("plan must not run"))
    monkeypatch.setattr(cli, "SshReadTransport", lambda *a, **k: pytest.fail("no device access"))
    with FakeNetBox(clean_devices()) as srv:
        assert cli.main(args(srv)) == 0
