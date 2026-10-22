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
