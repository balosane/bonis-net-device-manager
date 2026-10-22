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
netdev selftest                 # 12 safety checks on fake devices: no network, no credentials
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
(it follows `netdev_check_only`, rendered as `false`) and prints a warning: writes on the fleet stay banned until the owner
lifts the ban. Details: [docs/DESIGN.md](docs/DESIGN.md#ansible-executor-bonisisp-41).

## Safety rules built in

- Every command is classified (`read` / `update`); an unclassified one is refused (deny by default), including anything with `;`,
  newlines, backticks or `$(`.
- A `change` step must be an update, `precheck` and `verify` steps must be reads, a rollback must be an update.
- Excluded prefixes (and anything inside them) are never planned, e.g. `192.168.88.0/24`, the MikroTik factory default LAN.
- If a value was empty before, there is no automatic rollback; the plan says so in `notes`.

## Layout

```
bonis_netdev/intents.py     intents (validated)
bonis_netdev/drivers/       base, routeros, airos
bonis_netdev/classify.py    what a command does; deny by default
bonis_netdev/plan.py        Step, DevicePlan, Plan (plan_id, JSON, summary)
bonis_netdev/transport.py   read-only SSH transport, fake transport for tests
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

See [docs/DESIGN.md](docs/DESIGN.md). Not built: the executor (write flags, step runner, rollback run), more intents (NTP, syslog,
firmware upgrade), inventory from NetBox, CI pipeline for running plans. Plane: BONISISP-39.
