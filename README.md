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
