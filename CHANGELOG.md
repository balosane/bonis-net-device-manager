# Changelog

## Unreleased
- Ansible collection `bonis.netdev` with module `airos_cfg` (BONISISP-42): drift guard on the config hash, check mode, key-only diff,
  deny-by-default command allowlist. Write path implemented but disabled (`WRITES_ENABLED = False`).
- pyproject: limit package discovery to `bonis_netdev*`.

## 0.0.1 (prototype)
- Intent `Dns` (DHCP networks with exclusions, or the device resolver).
- Drivers `routeros` (6 and 7) and `airos`: read, plan. No writes.
- Plan: deterministic JSON, `plan_id` = sha256 of the content, precheck / change / verify steps with rollback.
- Command classification, deny by default; read-only SSH transport.
- CLI: `netdev plan | show | check`, plus `try`, `probe`, `demo`, `selftest` for quick testing.
