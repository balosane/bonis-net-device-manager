import json
import os
import stat
import sys

import pytest

from bonis_netdev import cli
from bonis_netdev.render import render_playbook
from bonis_netdev.render import runner
from bonis_netdev.render.runner import RunRefused, dir_hazards, run_playbook

from test_render import demo_plan, doc_of

FAKE = """#!{py}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
rec = {{"argv": sys.argv, "cwd": os.getcwd(), "env": dict(os.environ), "files": sorted(os.listdir(".")),
        "playbook": open("playbook.yml", "rb").read().decode(), "cwd_mode": oct(os.stat(".").st_mode & 0o777),
        "creds_mode": oct(os.stat("creds.yml").st_mode & 0o777) if os.path.exists("creds.yml") else None,
        "creds": open("creds.yml").read() if os.path.exists("creds.yml") else None,
        "cfg": open("ansible.cfg").read()}}
json.dump(rec, open(os.path.join(here, "record.json"), "w"))
"""


@pytest.fixture
def setup(tmp_path, monkeypatch):
    plan = demo_plan()
    src = tmp_path / "src"
    src.mkdir()
    (src / "plan.json").write_text(json.dumps(doc_of(plan)))
    (src / "playbook.yml").write_text(render_playbook(plan))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "ansible-playbook"
    fake.write_text(FAKE.format(py=sys.executable))
    fake.chmod(0o755)
    for k in list(os.environ):
        if k.startswith("ANSIBLE_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("NETDEV_USER", "admin-user")
    monkeypatch.setenv("NETDEV_PASS", "s3cret-pass")
    return src / "plan.json", src / "playbook.yml", str(bindir), bindir / "record.json"


def run(setup, **kw):
    plan, play, bindir, rec = setup
    rc = run_playbook(str(plan), str(play), ansible_venv_bin=bindir, **kw)
    return rc, json.loads(rec.read_text()) if rec.exists() else None


def test_apply_constant_is_off():
    assert runner.RUN_APPLY_ENABLED is False


def test_scrubbed_env_and_clean_cwd(setup, monkeypatch):
    monkeypatch.setenv("ANSIBLE_STRATEGY", "evil")
    monkeypatch.setenv("ANSIBLE_LIBRARY", "/evil")
    monkeypatch.setenv("ANSIBLE_CONFIG", "/evil/ansible.cfg")
    monkeypatch.setenv("SOMETHING_ELSE", "x")
    rc, rec = run(setup)
    assert rc == 0
    env = rec["env"]
    assert set(env) <= {"PATH", "HOME", "LANG", "ANSIBLE_CONFIG"}
    assert env["ANSIBLE_CONFIG"] == os.path.join(rec["cwd"], "ansible.cfg")
    assert env["HOME"] == rec["cwd"]
    assert rec["files"] == ["ansible.cfg", "creds.yml", "inventory.ini", "plan.json", "playbook.yml"]
    assert rec["cwd_mode"] == "0o700" and rec["creds_mode"] == "0o600"
    assert not os.path.exists(rec["cwd"])


def test_check_always_and_creds_not_in_argv(setup):
    rc, rec = run(setup, limit_tags="dev_DEMO_AP")
    argv = rec["argv"]
    assert "--check" in argv and "--diff" in argv
    assert argv[argv.index("--tags") + 1] == "dev_DEMO_AP"
    assert "@creds.yml" in argv
    assert not any("s3cret-pass" in x or "admin-user" in x for x in argv)
    assert "s3cret-pass" not in json.dumps(rec["env"])


def test_host_vars_not_copied_and_warned(setup, capsys):
    plan, play, bindir, rec = setup
    (play.parent / "host_vars").mkdir()
    (play.parent / "host_vars" / "localhost.yml").write_text("x: 1\n")
    (play.parent / "ansible.cfg").write_text("[defaults]\n")
    assert dir_hazards(str(play)) == ["host_vars", "ansible.cfg"]
    assert cli.main(["verify-playbook", str(plan), str(play)]) == 0
    assert "host_vars exists next to the playbook" in capsys.readouterr().err
    assert cli.main(["verify-playbook", str(plan), str(play), "--strict-dir"]) == 1
    rc, r = run(setup)
    assert "host_vars" not in r["files"]


def test_tampered_playbook_refused_before_process(setup):
    plan, play, bindir, rec = setup
    play.write_text(play.read_text().replace("ansible.builtin.assert", "ansible.builtin.shell", 1))
    with pytest.raises(RunRefused):
        run_playbook(str(plan), str(play), ansible_venv_bin=bindir)
    assert not rec.exists()
    assert cli.main(["run-playbook", str(plan), str(play), "--ansible-venv-bin", bindir]) == 1
    assert not rec.exists()


def test_apply_mode_playbook_refused(setup):
    plan, play, bindir, rec = setup
    play.write_text(render_playbook(demo_plan(), check_only=False))
    with pytest.raises(RunRefused, match="RUN_APPLY_ENABLED"):
        run_playbook(str(plan), str(play), ansible_venv_bin=bindir)
    assert not rec.exists()


def test_apply_flag_refused(setup):
    plan, play, bindir, rec = setup
    with pytest.raises(RunRefused, match="RUN_APPLY_ENABLED"):
        run_playbook(str(plan), str(play), ansible_venv_bin=bindir, apply=True)
    assert not rec.exists()
    assert cli.main(["run-playbook", str(plan), str(play), "--ansible-venv-bin", bindir, "--apply"]) == 1
    assert not rec.exists()


def test_missing_binary_refused(setup, tmp_path, monkeypatch):
    plan, play, _, _ = setup
    with pytest.raises(RunRefused, match="not found"):
        run_playbook(str(plan), str(play), ansible_venv_bin=str(tmp_path / "nowhere"))
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    with pytest.raises(RunRefused, match="not found"):
        run_playbook(str(plan), str(play))


def test_missing_credentials_refused(setup, monkeypatch):
    monkeypatch.delenv("NETDEV_PASS")
    plan, play, bindir, rec = setup
    with pytest.raises(RunRefused, match="NETDEV_USER"):
        run_playbook(str(plan), str(play), ansible_venv_bin=bindir)
    assert not rec.exists()


def test_verified_bytes_are_executed(setup):
    plan, play, bindir, rec = setup
    good = play.read_text()

    def mutate():
        play.write_text(good + "\n- hosts: localhost\n  tasks: [{ansible.builtin.shell: touch PWNED}]\n")

    rc = run_playbook(str(plan), str(play), ansible_venv_bin=bindir, _after_verify=mutate)
    assert rc == 0
    assert json.loads(rec.read_text())["playbook"] == good


def test_tempdir_removed_on_error(setup, monkeypatch):
    plan, play, bindir, rec = setup
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    made = []
    orig = runner.tempfile.mkdtemp
    monkeypatch.setattr(runner.tempfile, "mkdtemp", lambda **k: made.append(orig(**k)) or made[-1])
    with pytest.raises(OSError):
        run_playbook(str(plan), str(play), ansible_venv_bin=bindir)
    assert made and not os.path.exists(made[0])


SPIKE = "/tmp/claude-1000/-mnt-c--workdir-prj/55dc347f-582d-463c-84c7-1a51786a1f9b/scratchpad/spike/venv/bin"


def test_real_ansible_syntax_check(tmp_path):
    venv = os.environ.get("NETDEV_ANSIBLE_VENV", SPIKE)
    coll = os.environ.get("ANSIBLE_COLLECTIONS_PATH")
    if not os.path.exists(os.path.join(venv, "ansible-playbook")) or not coll:
        pytest.skip("ansible-playbook or ANSIBLE_COLLECTIONS_PATH unavailable")
    plan = demo_plan()
    (tmp_path / "plan.json").write_text(json.dumps(doc_of(plan)))
    (tmp_path / "playbook.yml").write_text(render_playbook(plan))
    rc = run_playbook(str(tmp_path / "plan.json"), str(tmp_path / "playbook.yml"), ansible_venv_bin=venv,
                      collections_path=coll, syntax_only=True)
    assert rc == 0


def test_credentials_are_unsafe_tagged_and_sys_path_scan_off(setup, monkeypatch):
    monkeypatch.setenv("NETDEV_PASS", 'p{{ lookup("pipe", "id") }}w')
    rc, rec = run(setup)
    assert rc == 0
    assert "netdev_user: !unsafe " in rec["creds"] and "netdev_pass: !unsafe " in rec["creds"]
    assert "{{" in rec["creds"]  # kept literally, never templated because of the tag
    assert "collections_scan_sys_path = False" in rec["cfg"]
