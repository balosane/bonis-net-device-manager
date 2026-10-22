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
