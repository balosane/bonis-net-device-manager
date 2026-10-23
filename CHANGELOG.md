# Changelog

## Unreleased
- Ansible module `bonis.netdev.routeros_service` (BONISISP-47): enables/disables the plain API service of a RouterOS device over SSH
  (`disabled`, `address` list with strict validation, drift guard `from_disabled`/`from_address`, check mode, diff). Write path
  disabled by the code constant `WRITES_ENABLED = False`; deny-by-default command classifier (one read, one write shape); verify and
  restore after a write. Tests use fakes only. Real captures show RouterOS flags disabled services `XI`: flag I is
  ignored when X is present; I without X is still refused.
- Renderer: every `api_info` task passes `hide_defaults: false`. Found by a live `--check` run on RouterOS 6.47.7: `api_info` hides values equal to the
  collection defaults (`secondary-ntp` 0.0.0.0, empty `servers`, `enabled` no), so the NTP precheck asserted a value that was not returned.
- Review fixes for ntp/syslog: empty RouterOS 7.15+ server list renders (`servers=` expect); `Ntp`/`Syslog` reject 0.0.0.0, loopback,
  multicast, broadcast/reserved, link-local; classify/renderer octets 0-255 and ports 1-65535; airOS refuses to create a key
  that a rollback would leave active; explicit IPv6-NTP limit.
- Features `ntp-standard` and `syslog-standard` end to end (BONISISP-46): intent `Syslog` (`Ntp` unchanged, now validated with
  `fullmatch`), RouterOS 6/7 and airOS drivers, three new RouterOS write shapes in `classify.py` (NTP legacy, NTP list,
  update of the existing `netdev-remote` logging action), renderer mappings (`api_modify` paths `system ntp client` and
  `system logging action`; airOS via `airos_cfg`), `parse.ros_print_map`, `load_intent` for `ntp`/`syslog`, `ntp_intents`/`syslog_intents`,
  seed capability `syslog.remote`, golden files `ntp.playbook.yml` / `syslog.playbook.yml`, two more `selftest` checks (14).
  RouterOS syslog reports `unsupported` when `netdev-remote` does not exist (no creation while writes are banned).

- NetBox inventory, phase 1, read-only (BONISISP-44): `netbox.py` (GET-only client), `inventory.py` (`devices_from_netbox`: tags
  `tpl-*` / `feat-*`), `reconcile.py` (`netdev reconcile [--json] [--emit DIR]`), model `aliases:` in the catalog, optional
  `Device.host`, `load_config(..., with_devices=False)`, `templates.format_matrix`. Tested against a fake NetBox only.
  Only `active` devices are managed, region slug is used as-is, the vars-layer key check applies to NetBox devices, `--emit` refuses
  when there are problems/invalid results, seed models for the real NetBox device types added.
  Review fixes: `next` links and every request target are pinned to the configured host (no token leak), token/host header validated
  and never echoed, list responses validated (count, size, page cap, repeated `next`), IPv4-only `primary_ip4`, status exactly `active`,
  `--emit` refuses symlinks and creates files with O_EXCL|O_NOFOLLOW.
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
