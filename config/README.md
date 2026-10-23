# Seed configuration for `netdev check-templates`

- `features/`   one feature per file: name, domain, requires (capabilities), params, intent, protect
- `models/`     model catalog: os_family and capabilities. Capabilities will later be read from NetBox device types.
- `templates/`  template name -> list of feature tags
- `vars/`       layered parameters: global, region, site, role, device (later layer wins)
- `devices.yml` demo inventory with one `invalid` device; `devices.example.yml` is a valid one

## Vars layer rules

- Every key in every vars layer (global, region, site, role, device) must be a parameter declared by some feature; a typo is a
  `ConfigError` naming the file, the layer and the key.
- If `vars/region.yml`, `site.yml` or `role.yml` exists, every region/site/role a device names must be a key of it (use `name: {}`
  for "no overrides"). A device that leaves the field unset, or a layer whose file does not exist, is not checked.

## Seed features

`dns-standard`, `ntp-standard` (param `servers`), `syslog-standard` (params `syslog_host`, `syslog_port?`; capability
`syslog.remote`) are implemented by the RouterOS and airOS drivers; `wifi-ap-5g` has no driver (`n/a`).

`mgmt-api` (param `api_allowed`, a list of IPv4 / IPv4/prefix; capability `mgmt.api`, seeded on all RouterOS models, not airOS) is
`protect: true`: it guards the controller's own access path (enforcement of `protect` is future work). The template `api-bootstrap`
carries it; global `api_allowed` is the management jumphost that runs the controller. It must run before any `api_modify` feature on a
router whose API is off.

## NetBox tags (`netdev reconcile`)

The inventory can come from NetBox. A tag on a NetBox device selects what git config applies (desired state only):

- `tpl-<template>` selects the template `<template>` from `templates/` (`tpl-core-router` -> `core-router`).
- `feat-<feature>` adds the single feature `<feature>` from `features/` (`feat-dns-standard`).
- Only devices with at least one such tag are managed; other devices are ignored (only counted). Other tags (`src-network-yml`,
  `needs-review`, ...) are not ours.
- A device is a **problem** (hard error, exit 1) if: its model is not in the catalog, it has no `primary_ip4`, a tag names an
  unknown template/feature, or it is a placeholder (`placeholder` tag, manufacturer `Generic`, model `Generic ...`).
- Model match: `<manufacturer> <model>` (e.g. `MikroTik hEX S`), then `<model>` alone, then the optional `aliases:` list of a
  model in `models/*.yml` (an alias may not belong to two models).
- Only devices with NetBox status `active` are managed; a tagged device with another status is listed as unmanaged
  (`status <value>`).
- Layers: region = NetBox site region slug as-is (lower-case, e.g. `mor`; none if the site has no region), site = site
  name, role = role slug. The management address (`host`) is `primary_ip4` without the prefix length.
- The strict layer check applies: if `vars/region.yml`, `site.yml` or `role.yml` exists, every region/site/role a NetBox device
  uses must be a key of it (an empty entry `name: {}` is fine). Otherwise the device is a problem naming the missing key. So
  every NetBox site name and role slug in use needs an entry.
- `--emit` writes nothing when there is any problem or `invalid` result.
- The seed model entries for the real NetBox device types (hEX, hEX PoE lite, RB951G-2HnD, RB433AH, CRS326, CCR1016, LHG 60G,
  PowerBeam, PowerBox Pro) carry conservative default capabilities, to be reviewed by the network owner.
- Names are matched lower-cased: NetBox tags are lower-cased before matching, so template and feature names in git must be
  lower-case (the loader already rejects anything else).
- region = the NetBox region slug, role = the role slug, but site = the NetBox site **name**: `vars/site.yml` keys must equal the
  NetBox site name exactly (case and spaces included).
- Status must be exactly `active`; a missing status is unmanaged (`no status`). `primary_ip4` must be an IPv4 address.
