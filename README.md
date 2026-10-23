# bonis-net-device-manager

Network device manager, part of the Bonis toolset. It changes configuration of MikroTik RouterOS and Ubiquiti airOS devices
in a controlled way:

```
intent  ->  driver  ->  plan (stored, hashed)  ->  review  ->  (later) execute
```

- **Intent**: vendor-neutral, says *what* must be true: `Dns(primary=..., secondary=..., scope=...)`.
- **Driver**: one per device class (`routeros6`, `routeros7`, `airos`). It reads the device and turns the intent into the
  concrete commands that class needs, replacing only the relevant values.
- **Plan**: every step, per device, with a precheck, the change, a verify step and a rollback. It is stored as JSON and its
  `plan_id` is the sha256 of its content, so an approval can refer to exactly what was reviewed.

**This is a prototype: it reads from devices and writes plans. There is no apply command.** The read-only transport refuses
any command that is not classified as a read, so the tool cannot change a device even by mistake.

## Install

```bash
pip install .                 # Python 3.10+, installs the `netdev` command
# SSH to the devices uses `sshpass` + `ssh` (apt install sshpass openssh-client)
# or the container: docker build -t netdev . && docker run --rm -v "$PWD:/w" -w /w netdev --help
```

## Try it in two minutes

```bash
netdev selftest                 # 14 safety checks on fake devices: no network, no credentials
netdev demo                     # a sample plan for a fake router and a fake AP
```

With a credentials file (`./credentials.yml`, `$NETDEV_CREDENTIALS` or `~/.config/netdev/credentials.yml`,
format in `examples/credentials.example.yml`), on a real device (read-only, nothing is changed):

```bash
netdev probe 10.0.0.23                                       # device class + the DNS values the tool sees
netdev try   10.0.0.23 --dns 192.168.238.119,192.168.6.1     # the plan for one device, no files needed
netdev try   192.168.130.66 --dns 192.168.238.119,192.168.6.1 --resolver   # an AP: its own resolver
netdev try   10.0.0.23 --dns 1.1.1.1 --exclude 10.15.5.0/24 --out plan.json
```

`192.168.88.0/24` (MikroTik factory LAN) is always excluded; `--exclude` adds more prefixes.

## Use

```bash
cp examples/credentials.example.yml ~/netdev-credentials.yml    # fill in, keep it out of git
netdev plan --intent examples/dns-intent.yml --targets examples/targets.yml \
            --credentials ~/netdev-credentials.yml --out plan.json
netdev show  plan.json          # what would change, per device, with rollback
netdev check plan.json          # exit 2 if the plan was edited after it was created
```

Intent files: `intent: dns` (`examples/dns-intent.yml`), `intent: ntp` (`examples/ntp-intent.yml`, `servers:` list of IPs or host names)
and `intent: syslog` (`examples/syslog-intent.yml`, `host:` IPv4, `port:` default 514). `netdev plan --intent` picks the planner by that key.

`netdev plan` exits 1 if any device could not be read; that device carries an `error` in the plan and the others are still planned.

### What a plan looks like

For RouterOS a change is a targeted `set` on the item that was found, guarded by what was read:

```
/ip dhcp-server network set [find address=192.168.80.0/26] dns-server=192.168.238.119,192.168.6.1
    192.168.80.0/26 dns-server 192.168.6.1,8.8.8.8 -> 192.168.238.119,192.168.6.1
    rollback: /ip dhcp-server network set [find address=192.168.80.0/26] dns-server=192.168.6.1,8.8.8.8
```

For airOS the running config is the flat file `/tmp/system.cfg`. The plan never contains a copy of it (it holds the wireless
PSK and password hashes). It holds `airos-cfg-set base=<sha256 of the file> key=value ...`: an executor must re-read the file,
refuse if the hash differs, replace exactly those keys in the whole file, and write the whole file back. A partial file is never written.

### NTP and syslog (BONISISP-46)

| | RouterOS | airOS |
|---|---|---|
| `ntp-standard` | `/system ntp client set enabled=yes primary-ntp=A secondary-ntp=B` (6.x and 7.x before 7.15; IPv4 only, max 2, `0.0.0.0` = unset) or `... servers=A,B` (7.15+; IPs or host names). The shape follows what `/system ntp client print` shows. | keys `ntpclient.N.server`, `ntpclient.N.status`, `ntpclient.status` (max 2 servers; host names allowed; only differing keys are set; the `airos_cfg` module appends keys missing from the file) |
| `syslog-standard` | `/system logging action set [find name=netdev-remote] remote=IP remote-port=N`: updates an EXISTING action only. If it does not exist the device is reported `unsupported` (creating actions/rules is not supported while writes are banned). Logging rules are never touched. | keys `syslog.remote.ip`, `syslog.remote.port`, `syslog.remote.status`, `syslog.status` |

A device that already holds the intent goes to `unchanged`, as with DNS.

## Ansible module for airOS

`ansible/collections/ansible_collections/bonis/netdev/` holds the collection `bonis.netdev` with the module `airos_cfg`: it
applies the `airos-cfg-set` step of a plan (`base` hash + key/value settings) with a drift guard, check mode and a key-only diff.
**Its write path is disabled** (`WRITES_ENABLED = False` in the module code); check mode is always read-only. See the
collection README.

## Ansible executor

Ansible is the primary executor; our own Python modules are the fallback where no Ansible module exists. The playbook is a
generated artefact, never hand-written, and it carries the `plan_id`:

```bash
netdev render-playbook plan.json --out playbook.yml     # check-only: every task has check_mode: true
netdev verify-playbook plan.json playbook.yml           # exit 0 ok, 1 violations (printed); run it before every run
```

Approval is a git MR containing `plan.json` + `playbook.yml`. The controller must run `verify-playbook` and refuse on any
violation: the playbook's `netdev_plan_id` must equal the recomputed plan id, `plan.json` must not be edited, only allowlisted
modules may be used (`community.routeros.api_info`, `community.routeros.api_modify`, `bonis.netdev.airos_cfg`, and
`ansible.builtin.debug|assert|set_fact`), and the tasks must equal the playbook re-rendered from the plan. Credentials come
from the variables `netdev_user` / `netdev_pass`, never from the file. `--apply-mode` renders without forcing `check_mode`
(it follows `netdev_check_only`, rendered as `false`; override with `-e '{"netdev_check_only": false}'`, a JSON boolean: the first task asserts `netdev_check_only is boolean`, so a string like `"false"` aborts the run) and
prints a warning: writes on the fleet stay banned until the owner lifts the ban. The airOS precheck gate and verify steps are
always `check_mode: true`. Details: [docs/DESIGN.md](docs/DESIGN.md#ansible-executor-bonisisp-41).

**Controller flow: `netdev run-playbook plan.json playbook.yml [--limit-tags TAGS] [--ansible-venv-bin DIR]` is the only
supported way to run a generated playbook.** It reads both files once, verifies those bytes (check-only required), copies them
into a fresh empty mode-700 temp directory with a controller-owned `ansible.cfg` and `inventory.ini`, runs `ansible-playbook`
there with a scrubbed environment (no `ANSIBLE_*` from the caller) and always `--check --diff`; credentials come from
`NETDEV_USER` / `NETDEV_PASS` via a temporary 0600 vars file, never argv. Apply is impossible (`RUN_APPLY_ENABLED = False` in
`bonis_netdev/render/runner.py`). Running `ansible-playbook` directly on an MR checkout is unsupported and unsafe: Ansible also
loads `host_vars/`, `group_vars/`, `ansible.cfg`, `collections/`, `roles/`, inventory and `ANSIBLE_*` variables that
`verify-playbook` does not cover (a `host_vars` file can run a shell command on the controller even in `--check`).
`verify-playbook` warns when such names sit next to the playbook (`--strict-dir` makes that a failure).

## Safety rules built in

- Every command is classified (`read` / `update`); an unclassified one is refused (deny by default), including anything with `;`,
  newlines, backticks or `$(`.
- A `change` step must be an update, `precheck` and `verify` steps must be reads, a rollback must be an update.
- Excluded prefixes (and anything inside them) are never planned, e.g. `192.168.88.0/24`, the MikroTik factory default LAN.
- If a value was empty before, there is no automatic rollback; the plan says so in `notes`.

## Templates and capabilities

A **template** is an unordered list of feature tags (`core-router: [dns-standard, ntp-standard]`); a device may have several, they are
merged. A **feature** (`config/features/*.yml`) declares its `domain`, the capabilities it `requires`, its `params` and the `intent` it
produces. The **model catalog** (`config/models/*.yml`) lists each model's `os_family` and capabilities (to be read from NetBox device
types later). Parameters are layered, the later layer wins: `global < region < site < role < device` (`config/vars/*.yml`), then the
device's own `params`.

```bash
netdev check-templates                                   # ./config, devices from config/devices.yml
netdev check-templates --devices config/devices.example.yml --json
```

Per (device, feature) the result is `ok` (capabilities present and a driver implements it), `invalid` (a required capability is
missing: error, exit 1) or `n/a` (no driver for that OS family: skipped with a reason, never an error). Unknown tag, template or model
and two features of one domain on one device are errors (exit 1). Offline: nothing is read from or sent to a device.

## Layout

```
bonis_netdev/intents.py     intents (validated)
bonis_netdev/drivers/       base, routeros, airos
bonis_netdev/classify.py    what a command does; deny by default
bonis_netdev/plan.py        Step, DevicePlan, Plan (plan_id, JSON, summary)
bonis_netdev/transport.py   read-only SSH transport, fake transport for tests
bonis_netdev/features.py    feature YAML, ConfigError
bonis_netdev/catalog.py     model catalog
bonis_netdev/templates.py   templates, layered vars, capability check
config/                     seed features, models, templates, vars, devices
bonis_netdev/cli.py         netdev plan | show | check | check-templates

bonis_netdev/render/        plan -> Ansible playbook, playbook verification
bonis_netdev/cli.py         netdev plan | show | check | render-playbook | verify-playbook
docs/DESIGN.md              design, decisions, what is not built yet
```

## Tests

```bash
pip install pytest && pytest -q
```

Tests run against canned device output; they need no device and no network.

## Status and next steps

See [docs/DESIGN.md](docs/DESIGN.md). Not built: write flags (the executor is the generated playbook, writes disabled), more intents (timezone, firmware upgrade), inventory from NetBox, CI pipeline for running plans. Plane: BONISISP-39.
