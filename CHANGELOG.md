# Changelog

## Unreleased
- Templates of features with capability checking (BONISISP-40): `features.py`, `catalog.py`, `templates.py`, seed `config/`,
  `Ntp` intent (no driver), `Driver.implements`, `netdev check-templates [--config DIR] [--devices FILE] [--json]`.
- pyproject: package discovery limited to `bonis_netdev*` (new top-level `config/` and `ansible/` broke automatic discovery).
- Ansible collection `bonis.netdev` with module `airos_cfg` (BONISISP-42): drift guard on the config hash, check mode, key-only diff,
  deny-by-default command allowlist. Write path implemented but disabled (`WRITES_ENABLED = False`).

- `bonis_netdev.render`: `render_playbook(plan)` renders a stored plan as a deterministic Ansible playbook carrying the plan_id;
  `verify_playbook(plan_json, text)` refuses tampered plans, non-allowlisted modules, missing `check_mode` and any drift from the plan.
- CLI: `netdev render-playbook` (check-only by default) and `netdev verify-playbook`.

## 0.0.1 (prototype)
- Intent `Dns` (DHCP networks with exclusions, or the device resolver).
- Drivers `routeros` (6 and 7) and `airos`: read, plan. No writes.
- Plan: deterministic JSON, `plan_id` = sha256 of the content, precheck / change / verify steps with rollback.
- Command classification, deny by default; read-only SSH transport.
- CLI: `netdev plan | show | check`, plus `try`, `probe`, `demo`, `selftest` for quick testing.
