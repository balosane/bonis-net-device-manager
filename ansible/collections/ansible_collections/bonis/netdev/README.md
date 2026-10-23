# bonis.netdev collection

Modules of the Bonis network device manager (`bonis-net-device-manager`).

## bonis.netdev.airos_cfg

Sets keys in the flat config `/tmp/system.cfg` of a Ubiquiti airOS device.

- Runs on the controller (`connection: local`); needs `ssh` and `sshpass` there. The device has no Python.
- Arguments: `host`, `username`, `password` (no_log), `port` (22), `base`, `settings` (key -> string), `state: present`.
- `base` is the sha256 of `/tmp/system.cfg` the plan was built against (drift guard), or `current` for no guard.
- Supports `check_mode` and `diff`; the diff holds only the touched keys, values of credential-like keys are masked.
- `settings` keys that look like credentials (psk, password, secret, hash, token, `*key`, community) are refused: Ansible logs
  `module_args` (`-v`), so the values would leak. A key whose existing line has leading whitespace is refused when it must change.
- Returns `changed`, `msg`, `diff`, `base_sha256`. The whole config is never returned.
- **Writes are disabled**: the code constant `WRITES_ENABLED` in `plugins/module_utils/airos_cfg.py` is `False` (fleet write ban).
  A non-check run that would change the device fails with a clear message. Enabling is a reviewed commit.
- Only the fixed read `cat /tmp/system.cfg` and generated write shapes (when enabled) are ever executed; everything else
  is refused before it reaches the wire.

```yaml
- bonis.netdev.airos_cfg:
    host: 192.168.130.66
    username: "{{ ap_user }}"
    password: "{{ ap_password }}"
    base: current
    settings: {resolv.nameserver.1.ip: 192.168.238.119}
  connection: local
  check_mode: true
  diff: true
```

`plugins/module_utils/airos_cfg.py` is self-contained (it must not import `bonis_netdev`); the parts that mirror the tool are
guarded against drift by `tests/test_airos_module.py`.

## bonis.netdev.routeros_service

Enables or disables the plain RouterOS API service (`/ip service`, row `api`) over SSH. Exists because the API is disabled on
part of the fleet, so the API-based modules cannot reach those routers. Deliberately narrow: one service, two properties.

- Runs on the controller (`connection: local`); needs `ssh` and `sshpass`.
- Arguments: `host`, `username`, `password` (no_log), `port` (22), `service` (only `api`; `api-ssl` needs a certificate and is not
  supported), `disabled` (required), `address` (list of IPv4 or IPv4/prefix), `from_disabled` / `from_address` (drift guard).
- `address` is required and non-empty when `disabled: false`. Every entry is validated: canonical IPv4 (no leading zeros), prefix
  /24 or longer, no 0.0.0.0/8, loopback, multicast, reserved or broadcast, no duplicates, at most 8. With `disabled: true` it is
  optional (validated and written when given, otherwise the current restriction is kept).
- Drift guard: if `from_disabled` / `from_address` is given and the device differs (`from_address: []` = no restriction),
  the module fails with `changed: false` right after the read.
- Returns `changed`, `msg`, `diff` (`before`/`after` with only `disabled` and `address` of `api`), `current`. Supports `check_mode` and `diff`.
- Reads `/ip service print detail without-paging where name=api` (RouterOS 6.x and 7.x layouts); a missing, ambiguous or
  invalid-flagged row is an error.
- **Writes are disabled**: code constant `WRITES_ENABLED = False` in `plugins/module_utils/routeros_service.py`. A non-check run
  that would change the device fails naming the constant, after the read only. When enabled, exactly one command shape is ever
  written, `/ip service set [find name=api] disabled=(yes|no) address=<list>`, followed by a re-read; on verification failure the previous
  values are restored (`address=""` only for that restore of a previously empty list). All commands pass a deny-by-default
  classifier before reaching the wire.

```yaml
- bonis.netdev.routeros_service:
    host: 192.168.130.1
    username: "{{ ros_user }}"
    password: "{{ ros_password }}"
    disabled: false
    address: [192.168.238.0/24]
    from_disabled: true
    from_address: []
  connection: local
  check_mode: true
  diff: true
```
