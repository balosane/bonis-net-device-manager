# Seed configuration for `netdev check-templates`

- `features/`   one feature per file: name, domain, requires (capabilities), params, intent, protect
- `models/`     model catalog: os_family and capabilities. Capabilities will later be read from NetBox device types.
- `templates/`  template name -> list of feature tags
- `vars/`       layered parameters: global, region, site, role, device (later layer wins)
- `devices.yml` demo inventory with one `invalid` device; `devices.example.yml` is a valid one
