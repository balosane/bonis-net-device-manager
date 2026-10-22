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
6. **Execute** (not built): re-run the prechecks, run the changes one at a time, stop at the first failure, verify, report the
   rollback for exactly what changed. No automatic rollback.

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

- `dns` is the only intent. DHCP networks are not matched to their DHCP server; a network of a disabled server is still planned
  and the disabled servers are listed in `skipped`.
- airOS: only the resolver scope; clearing a key (fewer servers than before) is reported as unsupported.
- RouterOS output is parsed from `print detail`; tested on canned output and on a 21-router snapshot of the real fleet.
- The credential list is tried in order (the fleet has several inherited passwords). Per-device overrides are not implemented.
- No LICENSE file yet; the owner has to choose one before the repo is shared outside the company.


## Templates and capabilities (`features.py`, `catalog.py`, `templates.py`)

- A template is a set of feature tags; a device's templates are unioned. A feature has a `domain`; two features of one domain on
  one device are a compile error. `protect` is reserved for features whose removal will need break-glass.
- Compile result per (device, feature): `invalid` if the model lacks a required capability (checked first, hard error),
  `n/a` if `Driver.implements` of the model's os_family lacks the feature (skipped with a reason), else `ok`. The implements table is
  code next to the drivers, not YAML. `ntp-standard` is implemented by no driver yet.
- Params are resolved only for `ok` results: layers `global < region < site < role < device`, then the device's own `params`; each
  param is type-checked against the feature (`ip`, `str`, `int`, `bool`, `list`; trailing `?` = optional).
- `templates.dns_intents()` converts `ok` results of intent `dns` into `intents.Dns`; no other mapping exists yet. Nothing here applies anything.

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
- **Not built**: the playbook renderer, a rollback run through the module (a rollback step is the same module with `base: current`),
  atomic whole-file replace (the sed edits are in place; a power loss between edit and `cfgmtd` leaves /tmp, not flash, changed).

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
| `airos-cfg-set base=<sha256\|current> k=v ...` | `bonis.netdev.airos_cfg` (host, username, password, base, settings, state: present) |
| airOS precheck | `debug` only: no read-only module exists; the `base` hash is enforced by `airos_cfg` on the change |
| airOS verify | `airos_cfg` with `base: current` and the expected values, then `assert not changed` |
| rollback | `rescue:` of the change group, followed by an `assert false` so the run stops |
| anything else | `RenderError` (deny by default) |

Each change forms an inner `block` (change + verify) with its own `rescue`, nested in the device block; prechecks stay outside it, so
a failed precheck (someone edited the device by hand) never triggers a rollback that would overwrite that edit. Plan text containing
Jinja syntax (`{{`, `{%`, `{#`) is refused because Ansible templates string arguments.

**Check-only.** By default every task has `check_mode: true` and the play var `netdev_check_only: true`. `--apply-mode` renders
`check_mode: "{{ netdev_check_only | bool }}"` with `netdev_check_only: false`; writes stay banned on the fleet until the owner lifts it.

**Verification** (`verify_playbook`, returns a list of violations): plan.json recomputes to its own plan_id; the playbook's
`netdev_plan_id` equals it; every module is in `ALLOWLIST`; no task lacks `check_mode: true` in a check-only playbook; no extra
play keys (roles, pre_tasks, imports ...); the whole play equals the playbook re-rendered from the plan. The last check makes the
others redundant, but they give clear messages.

**Contract for `bonis.netdev.airos_cfg`** (implemented elsewhere): args `host`, `username`, `password` (no_log), `base`
(64-hex sha256 of `/tmp/system.cfg` or `current`), `settings` (dict key -> str), `state: present`; supports check mode; returns
`changed` and `diff`. It must fail when `base` is a hash and the file no longer matches.
