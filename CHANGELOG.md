# Changelog

## Unreleased
- Features `ntp-standard` and `syslog-standard` end to end (BONISISP-46): intent `Syslog` (`Ntp` unchanged, now validated with
  `fullmatch`), RouterOS 6/7 and airOS drivers, three new RouterOS write shapes in `classify.py` (NTP legacy, NTP list,
  update of the existing `netdev-remote` logging action), renderer mappings (`api_modify` paths `system ntp client` and
  `system logging action`; airOS via `airos_cfg`), `parse.ros_print_map`, `load_intent` for `ntp`/`syslog`, `ntp_intents`/`syslog_intents`,
  seed capability `syslog.remote`, golden files `ntp.playbook.yml` / `syslog.playbook.yml`, two more `selftest` checks (14).
  RouterOS syslog reports `unsupported` when `netdev-remote` does not exist (no creation while writes are banned).
- Third review fix: `netdev run-playbook` (`render/runner.py`) is the only supported executor: single read + verify of the bytes,
  fresh empty temp dir with controller-owned `ansible.cfg`/`inventory.ini`, scrubbed environment, credentials via a 0600 vars file,
  always `--check --diff`, `RUN_APPLY_ENABLED = False`. `verify-playbook` warns (`--strict-dir` fails) on host_vars/, group_vars/,
  ansible.cfg, collections/, roles/ next to the playbook.
- Templates of features with capability checking (BONISISP-40): `features.py`, `catalog.py`, `templates.py`, seed `config/`,
  `Ntp` intent (no driver), `Driver.implements`, `netdev check-templates [--config DIR] [--devices FILE] [--json]`.
- pyproject: package discovery limited to `bonis_netdev*` (new top-level `config/` and `ansible/` broke automatic discovery).
- Ansible collection `bonis.netdev` with module `airos_cfg` (BONISISP-42): drift guard on the config hash, check mode, key-only diff,
  deny-by-default command allowlist. Write path implemented but disabled (`WRITES_ENABLED = False`).

- `bonis_netdev.render`: `render_playbook(plan)` renders a stored plan as a deterministic Ansible playbook carrying the plan_id;
  `verify_playbook(plan_json, text)` refuses tampered plans, non-allowlisted modules, missing `check_mode` and any drift from the plan.
- CLI: `netdev render-playbook` (check-only by default) and `netdev verify-playbook`.

- Review fixes: airOS precheck is a check_mode gate outside the rescued block (no rollback over a hand edit); airOS verify is
  always `check_mode: true`; every condition uses `netdev_check_only | bool`; `airos_cfg` refuses secret-looking keys and
  indented key lines; templates type-check region/site/role/names and reject undeclared device `params`; Dockerfile ships
  `config/`, the collection, ansible-core and community.routeros; CI checks the collection and the renderer.

## 0.0.1 (prototype)
- Intent `Dns` (DHCP networks with exclusions, or the device resolver).
- Drivers `routeros` (6 and 7) and `airos`: read, plan. No writes.
- Plan: deterministic JSON, `plan_id` = sha256 of the content, precheck / change / verify steps with rollback.
- Command classification, deny by default; read-only SSH transport.
- CLI: `netdev plan | show | check`, plus `try`, `probe`, `demo`, `selftest` for quick testing.
