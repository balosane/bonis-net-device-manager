"""The ONLY supported way to run a generated playbook: `netdev run-playbook PLAN.json PLAYBOOK.yml`.

`verify_playbook` proves the playbook text, but Ansible also loads host_vars/, group_vars/, ansible.cfg, collections/,
roles/, inventory and ANSIBLE_* variables from the working directory and the environment (a host_vars file can run
`lookup('pipe', ...)` on the controller even in --check). So the runner:

* reads both files ONCE into memory, verifies exactly those bytes (check-only required) and executes exactly those bytes;
* runs in a fresh EMPTY temp directory (mode 700) holding only plan.json, playbook.yml, a controller-owned ansible.cfg,
  a generated inventory.ini and (for the duration of the run) a 0600 credentials file;
* runs with a scrubbed environment (PATH, HOME, LANG, ANSIBLE_CONFIG only);
* always passes --check --diff. Apply is impossible: RUN_APPLY_ENABLED is a code constant, no flag changes it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from .ansible import verify_playbook

# Fleet-wide write ban: turning this on is a code change that needs the owner's explicit approval.
RUN_APPLY_ENABLED = False

# Trust boundary: the playbook bytes are verified, the collection code that executes them is not. A change under
# ansible/collections (or a --collections-path) is write-capable code and needs the owner's review under the write ban.
DEFAULT_COLLECTIONS_PATH = str(Path(__file__).resolve().parents[2] / "ansible" / "collections")
HAZARD_NAMES = ("host_vars", "group_vars", "ansible.cfg", "collections", "roles")


class RunRefused(Exception):
    """The runner refuses to start ansible-playbook."""


def dir_hazards(playbook_path: str) -> list[str]:
    """Names next to the playbook file that Ansible would pick up (host_vars/, ansible.cfg, ...)."""
    base = os.path.dirname(os.path.abspath(playbook_path))
    return [n for n in HAZARD_NAMES if os.path.lexists(os.path.join(base, n))]


def _resolve_binary(ansible_venv_bin: str | None) -> str:
    if ansible_venv_bin:
        cand = os.path.join(ansible_venv_bin, "ansible-playbook")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
        raise RunRefused(f"ansible-playbook not found or not executable in {ansible_venv_bin}")
    found = shutil.which("ansible-playbook")
    if not found:
        raise RunRefused("ansible-playbook binary not found (use --ansible-venv-bin or put it on PATH)")
    return found


def _write(path: str, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def run_playbook(plan_path: str, playbook_path: str, *, limit_tags: str | None = None, ansible_venv_bin: str | None = None,
                 collections_path: str | None = None, apply: bool = False, syntax_only: bool = False,
                 _after_verify: Callable[[], None] | None = None) -> int:
    """Verify and run the playbook in check mode. Returns the ansible-playbook exit code.

    Raises RunRefused before any process is started when something is not acceptable.
    `syntax_only` is for tests only (the CLI never exposes it): --syntax-check instead of a run.
    `_after_verify` is a test hook called between verification and execution (TOCTOU test)."""
    if apply or RUN_APPLY_ENABLED:
        raise RunRefused("apply is refused: RUN_APPLY_ENABLED is False in bonis_netdev/render/runner.py "
                         "(fleet-wide write ban); run-playbook only runs --check")
    with open(plan_path, "rb") as f:
        plan_bytes = f.read()
    with open(playbook_path, "rb") as f:
        play_bytes = f.read()
    try:
        plan_doc = json.loads(plan_bytes.decode("utf-8"))
        play_text = play_bytes.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        raise RunRefused(f"cannot read plan/playbook: {e}")
    violations = verify_playbook(plan_doc, play_text, require_check_only=True)
    if violations:
        if not verify_playbook(plan_doc, play_text):
            violations.append("the playbook is an apply-mode playbook; RUN_APPLY_ENABLED is False in "
                              "bonis_netdev/render/runner.py, run-playbook only runs check-only playbooks")
        raise RunRefused("playbook refused:\n" + "\n".join(f"VIOLATION: {v}" for v in violations))
    binary = _resolve_binary(ansible_venv_bin)
    if _after_verify:
        _after_verify()

    coll = DEFAULT_COLLECTIONS_PATH + (os.pathsep + collections_path if collections_path else "")
    user, pw = os.environ.get("NETDEV_USER"), os.environ.get("NETDEV_PASS")
    if not syntax_only and not (user and pw):
        raise RunRefused("NETDEV_USER and NETDEV_PASS must be set in the environment")
    tmp = tempfile.mkdtemp(prefix="netdev-run-")
    os.chmod(tmp, 0o700)
    try:
        _write(os.path.join(tmp, "plan.json"), plan_bytes)
        _write(os.path.join(tmp, "playbook.yml"), play_bytes)
        _write(os.path.join(tmp, "inventory.ini"), b"localhost ansible_connection=local\n")
        _write(os.path.join(tmp, "ansible.cfg"), (
            "[defaults]\n"
            "inventory = inventory.ini\n"
            f"collections_path = {coll}\n"
            "retry_files_enabled = False\n"
            "collections_scan_sys_path = False\n"
            "stdout_callback = default\n"
            "deprecation_warnings = False\n").encode())
        cmd = [binary, "-i", "inventory.ini", "playbook.yml", "--check", "--diff"]
        if limit_tags:
            cmd += ["--tags", limit_tags]
        if syntax_only:
            cmd.append("--syntax-check")
        else:
            # !unsafe: Ansible must not run Jinja over the credential values
            _write(os.path.join(tmp, "creds.yml"),
                   f"netdev_user: !unsafe {json.dumps(user)}\nnetdev_pass: !unsafe {json.dumps(pw)}\n".encode())
            cmd += ["-e", "@creds.yml"]
        env = {"PATH": os.pathsep.join(p for p in (os.path.dirname(binary), os.environ.get("PATH", "/usr/bin:/bin")) if p),
               "HOME": tmp, "LANG": os.environ.get("LANG", "C.UTF-8"),
               "ANSIBLE_CONFIG": os.path.join(tmp, "ansible.cfg")}
        return subprocess.run(cmd, cwd=tmp, env=env).returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_run_playbook(a) -> int:
    for h in dir_hazards(a.playbook):
        print(f"WARNING: {h} exists next to the playbook; run-playbook ignores it (empty temp dir)", file=sys.stderr)
    try:
        return run_playbook(a.plan, a.playbook, limit_tags=a.limit_tags, ansible_venv_bin=a.ansible_venv_bin,
                            collections_path=a.collections_path, apply=a.apply)
    except RunRefused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
