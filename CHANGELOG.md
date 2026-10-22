# Changelog

## Unreleased
- Templates of features with capability checking (BONISISP-40): `features.py`, `catalog.py`, `templates.py`, seed `config/`,
  `Ntp` intent (no driver), `Driver.implements`, `netdev check-templates [--config DIR] [--devices FILE] [--json]`.
- pyproject: package discovery limited to `bonis_netdev*` (the new top-level `config/` broke automatic discovery).

## 0.0.1 (prototype)
- Intent `Dns` (DHCP networks with exclusions, or the device resolver).
- Drivers `routeros` (6 and 7) and `airos`: read, plan. No writes.
- Plan: deterministic JSON, `plan_id` = sha256 of the content, precheck / change / verify steps with rollback.
- Command classification, deny by default; read-only SSH transport.
- CLI: `netdev plan | show | check`, plus `try`, `probe`, `demo`, `selftest` for quick testing.
