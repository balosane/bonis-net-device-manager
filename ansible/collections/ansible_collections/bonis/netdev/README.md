# bonis.netdev collection

Modules of the Bonis network device manager (`bonis-net-device-manager`).

## bonis.netdev.airos_cfg

Sets keys in the flat config `/tmp/system.cfg` of a Ubiquiti airOS device.

- Runs on the controller (`connection: local`); needs `ssh` and `sshpass` there. The device has no Python.
- Arguments: `host`, `username`, `password` (no_log), `port` (22), `base`, `settings` (key -> string), `state: present`.
- `base` is the sha256 of `/tmp/system.cfg` the plan was built against (drift guard), or `current` for no guard.
- Supports `check_mode` and `diff`; the diff holds only the touched keys, values of credential-like keys are masked.
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
