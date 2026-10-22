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
