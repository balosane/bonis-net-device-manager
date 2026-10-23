# Design

## Why not plain Ansible or Terraform

Plain Ansible playbooks get hard to read once values differ per device. Terraform needs a state file and, for RouterOS, the
REST API (RouterOS 7); most of the fleet is on RouterOS 6.44-6.49, and Ubiquiti airOS has no provider.
So the logic lives in code with tests: an intent is turned into a concrete, stored plan per device. Ansible (or plain SSH) can
later be only the transport that runs an approved plan.

## Pipeline

1. **Intent** (`intents.py`): frozen dataclasses, validated on creation (IPs, prefixes, duplicates).
2. **Read**: the driver lists the read-only commands it needs (`read_commands`); the transport runs them.
3. **Plan** (`Driver.plan`): a pure function of (intent, what was read). Same input gives the same plan byte for byte.
4. **Store**: `plan.json`. `plan_id` = sha256 of the canonical content; `created_at` is stored beside it, not in it.
5. **Review/approve**: approval refers to the `plan_id`. `netdev check` recomputes it.
6. **Execute**: done through the generated Ansible playbook (see "Ansible executor" below): prechecks re-run, changes one at a
   time, stop at the first failure, verify. The rollback of a change group runs in a `rescue` only for a failure of the change
   or its verify, never for a failed precheck/gate. Writes stay disabled fleet-wide (`WRITES_ENABLED = False`, check-only default).

## Decisions

- **No state file.** The device is the truth; the plan embeds a fingerprint of what was read and `expect` values that the
  executor must re-check (compare-and-swap), so a hand edit between plan and apply is detected, not overwritten.
- **Drivers per device class**, chosen from what the device says about itself (`/system resource print`, or `/tmp/system.cfg`).
  RouterOS 6 and 7 share a class parameterised by major version.
- **airOS plans carry no config copy**, only `base` hash and key=value pairs (secrets stay on the device).
- **Deny by default** classification, a rule per allowed command shape. A new kind of write needs a new rule and a test.
- **Write flags are code constants**, as in olt-magix (`gpon_ops/ban.py`): turning on writes must be a reviewed commit. The
  prototype has none because it has no executor.
- **Exclusions are part of the intent**, with a reason, so they are visible in the stored plan.
- **Empty previous value = no rollback value.** Recorded in `notes` instead of guessing a command.

## Known limits

- Intents: `dns`, `ntp`, `syslog` (see "NTP and syslog" below). DHCP networks are not matched to their DHCP server; a network of a disabled server is still planned
  and the disabled servers are listed in `skipped`.
- airOS: only the resolver scope; clearing a key (fewer servers than before) is reported as unsupported.
- RouterOS output is parsed from `print detail`; tested on canned output and on a 21-router snapshot of the real fleet.
- The credential list is tried in order (the fleet has several inherited passwords). Per-device overrides are not implemented.
- Vars layers are validated strictly: every key in every layer must be declared by some feature, and a device may only name a
  region/site/role that is a key of the corresponding layer file when that file exists (a missing file or an unset field is not
  checked). See config/README.md.
- airOS rollback with `base: current` can overwrite an edit made on the device during the short window between a written change
  and the rescue.
- No LICENSE file yet; the owner has to choose one before the repo is shared outside the company.


## Templates and capabilities (`features.py`, `catalog.py`, `templates.py`)

- A template is a set of feature tags; a device's templates are unioned. A feature has a `domain`; two features of one domain on
  one device are a compile error. `protect` is reserved for features whose removal will need break-glass.
- Compile result per (device, feature): `invalid` if the model lacks a required capability (checked first, hard error),
  `n/a` if `Driver.implements` of the model's os_family lacks the feature (skipped with a reason), else `ok`. The implements table is
  code next to the drivers, not YAML. `ntp-standard` and `syslog-standard` are implemented by both drivers; `wifi-ap-5g` by none.
- Params are resolved only for `ok` results: layers `global < region < site < role < device`, then the device's own `params`; each
  param is type-checked against the feature (`ip`, `str`, `int`, `bool`, `list`; trailing `?` = optional).
- `templates.dns_intents()`, `ntp_intents()` and `syslog_intents()` convert `ok` results into `intents.Dns` / `Ntp` / `Syslog`. Nothing here applies anything.

## NTP and syslog (BONISISP-46)

- **Intents**: `Ntp(servers, reason)` (IPs or host names, validated with `fullmatch`, so a trailing newline is refused; the
  characters are limited to `[A-Za-z0-9.-]`) and `Syslog(host, port=514, reason)` (canonical IPv4, port 1-65535).
- **RouterOS NTP shape is chosen from the output**, not only the major version: `primary-ntp` present -> legacy shape (RouterOS 6
  and 7 before 7.15: IPv4 only, at most 2 servers, host names would need `server-dns-names`, reported `unsupported`); `servers`
  present -> list shape (7.15+). `parse.ros_print_map` reads `key: value` blocks (multi-line values are joined with `,`) as well
  as `key=value` detail output. Recorded captures are in `samples.py` (RouterOS 6.47.7, 7.18.2/7.24.2, airOS 5.6.11).
- **Rollback of NTP**: legacy shape always has one (`0.0.0.0` = unset); list shape has none when the old list was empty (noted).
- **RouterOS syslog is deliberately narrow**: creating logging actions/rules is a risk while writes are banned and needs a
  create-step design (open). Only an existing action `netdev-remote` with `target=remote` is updated; otherwise the device is
  `unsupported` with that reason. Existing rules (e.g. action `remote`, prefix = host name) are never touched; rules for
  `critical,error,warning,info -> netdev-remote` are part of that future create-step design.
- **airOS**: only differing keys are set; `ntpclient.2.*` is created only when the intent has a second server (the module appends
  missing keys). Fewer servers than configured (a stale `ntpclient.2.server`) is reported `unsupported`, as for DNS.
- **Not implemented, candidate feature `timezone-standard`**: router MOR-N29-RT-1 has time-zone-name `manual` GMT+00:00, and
  airOS APs report `system.timezone=GMT+12` with a date in 2019; NTP alone does not fix wrong zones.

## Ansible module for airOS (`bonis.netdev.airos_cfg`)

- **Why a module**: the plan's airOS change step is `base` hash + key/value pairs. Ansible only transports it; the logic
  (drift guard, diff, verify, restore) stays in tested Python. The module runs on the controller (`connection: local`) over
  `sshpass`/`ssh`, because the AP has no Python. The password goes in `SSHPASS`, not in argv.
- **Self-contained**: modules run from a zip of the collection, so `module_utils/airos_cfg.py` copies the small pieces it needs
  (`cfg_parse`, sha256, SSH options, read command) instead of importing `bonis_netdev`; a test runs both implementations on the same inputs.
- **Flow**: read `/tmp/system.cfg` -> hash -> fail if `base` is not `current` and differs -> diff only differing keys -> no diff, no write.
  Credential-like keys (psk, password, hash...) are masked in the diff; the whole file is never returned.
- **Write path** (implemented, OFF): `WRITES_ENABLED = False` is a code constant. When on: `sed -i` on the exact key line or an
  appended `printf` line, then `cfgmtd -w -p /etc/persistent/`, re-read, verify; on a failed verify the previous values are written
  back (missing keys deleted) and the run fails. Keys `[A-Za-z0-9_.-]+`; values reject newline, CR, NUL, backtick, `;`, `$(`.
- **Deny by default**: a `GuardedTransport` runs only `cat /tmp/system.cfg` and, with writes on, commands matching the generated write shapes.
  Check mode never reaches a write command even with the flag on.
- A rollback is the same module with `base: current` (rendered into the `rescue` of the change group).
- Secret-looking keys (`SECRET_KEY_RX`: psk, passw, secret, hash, token, key$, community) are REFUSED in `settings`: Ansible
  logs `module_args` (`-v`, callbacks), so their values would leak. A proper design (vault/no_log values) does not exist yet.
- A key whose line in `system.cfg` has leading whitespace is refused when it must change: the write regex only matches column 0
  and would otherwise be a silent no-op.
- **Not built**: atomic whole-file replace (the sed edits are in place; a power loss between edit and `cfgmtd` leaves /tmp, not flash, changed).

## Ansible executor (BONISISP-41)

Decision with the owner: Ansible is the PRIMARY executor; our own Python modules are the fallback where no Ansible module exists.
The playbook is generated from a stored plan (`bonis_netdev/render/ansible.py`), never hand-written. Approval is a git MR
containing `plan.json` + `playbook.yml`; the controller runs `verify_playbook` and refuses the run on any violation.

**Rendering.** One play, `hosts: localhost`, `connection: local`; one `block` per device in canonical plan order (sorted by device
name, the order of `plan.json`), tagged `dev_<name>`. Play vars: `netdev_plan_id`, `netdev_check_only`, `netdev_waves` (a single
wave listing all device tags; informational, pick a wave with `--tags`), `netdev_api_tls`, `netdev_validate_certs`. Credentials are
only the variables `netdev_user` / `netdev_pass`; the device address is the plan's `host`.

| plan step | task |
|---|---|
| `/ip dns set servers=A,B` | `community.routeros.api_modify` path `ip dns`, `handle_absent_entries/handle_entries_content: ignore` |
| `/ip dhcp-server network set [find address=P] dns-server=A,B` | `api_info` + `assert` that P exists (api_modify would create it), then `api_modify` with `data: [{address: P, dns-server: A,B}]` |
| RouterOS precheck / verify (reads) | `api_info` + `assert` against `expect`; verify tasks carry `when: not netdev_check_only` |
| `/system ntp client set enabled=E primary-ntp=A secondary-ntp=B` / `... servers=A,B` | `api_modify` path `system ntp client` (`enabled` bool, singleton; `ignore` flags); read/verify via `api_info` + `assert` on the first row |
| `/system logging action set [find name=netdev-remote] remote=IP remote-port=N` | `api_info` + `assert` that the action exists and has `target == remote` (api_modify would create it), then `api_modify` path `system logging action`, `data: [{name, target: remote, remote, remote-port}]` (`target` is a required key of the module for this path) |
| `airos-cfg-set base=<sha256\|current> k=v ...` | `bonis.netdev.airos_cfg` (host, username, password, base, settings, state: present) |
| airOS precheck | a GATE: `airos_cfg` with the plan's `base` hash and target settings, literal `check_mode: true` (also in apply mode), placed in the device block BEFORE the rescued group |
| airOS verify | `airos_cfg` with `base: current` and the expected values, literal `check_mode: true`, then `assert not changed` |
| rollback | `rescue:` of the change group, followed by an `assert false` so the run stops |
| anything else | `RenderError` (deny by default) |

Each change forms an inner `block` (change + verify) with its own `rescue`, nested in the device block; prechecks and the airOS
gate stay outside it. A hand edit between plan and apply therefore fails the gate (base hash mismatch) or a RouterOS precheck
assert BEFORE anything is changed, and no rollback runs that would overwrite the edit. (Inside the rescued block the module's own
base-hash check is only a second line of defence; a failure there does trigger the rollback, which is why the gate comes first.)
A hand edit made after the gate but during the run is not detected. Plan text containing
Jinja syntax (`{{`, `{%`, `{#`) is refused because Ansible templates string arguments.

**Check-only.** By default every task has `check_mode: true` and the play var `netdev_check_only: true`. `--apply-mode` renders
`check_mode: "{{ netdev_check_only | bool }}"` with `netdev_check_only: false`; writes stay banned on the fleet until the owner lifts it.
Every condition uses `netdev_check_only | bool`, and verify tasks run `when: not (netdev_check_only | bool)`. Pass the override as a
JSON boolean, `-e '{"netdev_check_only": false}'`: the first task of every playbook asserts `netdev_check_only is boolean`, so
a string such as `-e netdev_check_only=false` aborts the run before anything else happens.

**Verification** (`verify_playbook`, returns a list of violations): plan.json recomputes to its own plan_id; the playbook's
`netdev_plan_id` equals it; every module is in `ALLOWLIST`; no task lacks `check_mode: true` in a check-only playbook; no extra
play keys (roles, pre_tasks, imports ...); the whole play equals the playbook re-rendered from the plan. The last check makes the
others redundant, but they give clear messages. The comparison covers every play key including `name` (Ansible templates it). As defence in depth, any `{{`, `{%` or `{#` in any key or value of the loaded playbook that the re-render does not contain at the same path is a violation.

**Contract for `bonis.netdev.airos_cfg`** (implemented elsewhere): args `host`, `username`, `password` (no_log), `base`
(64-hex sha256 of `/tmp/system.cfg` or `current`), `settings` (dict key -> str), `state: present`; supports check mode; returns
`changed` and `diff`. It must fail when `base` is a hash and the file no longer matches.

### Controller flow: `netdev run-playbook` only

`verify_playbook` proves only the playbook text. Ansible also loads `host_vars/`, `group_vars/`, `ansible.cfg`, `collections/`,
`roles/`, inventory and `ANSIBLE_*` environment variables from the playbook directory / working directory (reviewer proof: a
`host_vars/localhost.yml` with `ansible_python_interpreter: "{{ lookup('pipe', ...) }}"` ran a command on the controller even
with `--check`), and verify-then-run on a path is a TOCTOU window. Therefore `netdev run-playbook` (`render/runner.py`) is the only
supported executor: bytes are read once, verified (`require_check_only=True`) and executed from memory; the run happens in a fresh
empty 0700 temp dir holding `plan.json`, `playbook.yml`, a generated `ansible.cfg` (fixed collections path), `inventory.ini`
(`localhost ansible_connection=local`) and a 0600 `creds.yml` (from `NETDEV_USER` / `NETDEV_PASS`, removed with the dir; values are written as `!unsafe` so Ansible never templates them);
the environment contains only PATH, HOME, LANG and ANSIBLE_CONFIG. `--check --diff` is always passed and `RUN_APPLY_ENABLED = False`
(a code constant, no flag changes it) refuses apply-mode playbooks. Running `ansible-playbook` directly on an MR checkout is
unsupported and unsafe. `verify-playbook` warns about hazard names next to the playbook (`--strict-dir` fails).

- Trust boundary of `run-playbook`: the playbook is verified byte for byte, the collection code that runs it (`ansible/collections`, `--collections-path`) is not. A change to that code is write-capable and needs the owner's review while the write ban lasts.
