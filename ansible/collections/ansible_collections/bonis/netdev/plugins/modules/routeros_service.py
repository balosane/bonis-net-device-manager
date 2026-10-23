#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: routeros_service
short_description: Enable or disable the plain API service of a MikroTik RouterOS device over SSH
version_added: "0.0.1"
description:
  - Reads C(/ip service) row C(api) over SSH and compares C(disabled) and C(address) with the requested state. Only when they
    differ, and only when writes are enabled in the module code, it runs exactly one command
    C(/ip service set [find name=api] disabled=... address=...), re-reads and verifies, and restores the previous values on failure.
  - Exists because the RouterOS API is disabled on part of the fleet, so the API-based modules cannot reach those routers.
  - Deliberately narrow, not a command runner. Other services (ssh, winbox, www, telnet, ftp, api-ssl) are never touched.
  - "Runs on the controller (use C(connection: local)); needs C(sshpass) and C(ssh)."
  - B(Writes are disabled in this version) (code constant C(WRITES_ENABLED)); only check mode and no-op runs succeed.
options:
  host:
    description: Device address.
    type: str
    required: true
  username:
    description: SSH user.
    type: str
    required: true
  password:
    description: SSH password.
    type: str
    required: true
  port:
    description: SSH port.
    type: int
    default: 22
  service:
    description: Only C(api) (plain API). C(api-ssl) is not supported because it needs a certificate.
    type: str
    default: api
    choices: [api]
  disabled:
    description: Desired state of the service.
    type: bool
    required: true
  address:
    description:
      - Allowed source addresses (IPv4 or IPv4/prefix), at most 8, no duplicates, prefix /24 or longer, no loopback,
        multicast, reserved, unspecified or broadcast.
      - Required and non-empty when I(disabled=false). When I(disabled=true) it is optional; if omitted the current value is kept.
    type: list
    elements: str
  from_disabled:
    description: Drift guard. If given and the device currently differs, the module fails before anything else.
    type: bool
  from_address:
    description: Drift guard for the address list (C([]) means no restriction currently). Same semantics as I(from_disabled).
    type: list
    elements: str
  restore:
    description:
      - Plain restore of a PREVIOUS state (used by the rollback of a generated playbook, only in C(rescue:)). Accepts as desired
        values anything the write shape can express (an empty I(address) list (no restriction), any prefix length /0-/32 with
        host bits, up to 32 entries, no duplicates, IPv4 only). Requires both I(from_disabled) and I(from_address), the drift
        guard to the state the forward change created; otherwise the module refuses. The forward path (default false) keeps all
        its validation.
    type: bool
    default: false
attributes:
  check_mode:
    support: full
  diff_mode:
    support: full
author:
  - Bonis
"""

EXAMPLES = r"""
- name: Enable the API, restricted to the management network (check first)
  bonis.netdev.routeros_service:
    host: 192.168.130.1
    username: "{{ ros_user }}"
    password: "{{ ros_password }}"
    disabled: false
    address:
      - 192.168.238.0/24
    from_disabled: true
    from_address: []
  connection: local
  check_mode: true
  diff: true
"""

RETURN = r"""
changed:
  description: Whether the service was (or in check mode would be) changed.
  returned: always
  type: bool
msg:
  description: Human readable result.
  returned: always
  type: str
diff:
  description: C(before) and C(after), each with the keys C(disabled) and C(address) of the C(api) service only.
  returned: always
  type: dict
restored:
  description: Present on a failed write only. True when the module restored the previous values itself (the message says so).
  returned: when a write failed
  type: bool
current:
  description: C(disabled) and C(address) as read from the device (empty when the read failed).
  returned: always
  type: dict
"""

from ansible.module_utils.basic import AnsibleModule

try:
    from ansible_collections.bonis.netdev.plugins.module_utils import routeros_service as lib
except ImportError:  # pragma: no cover - fallback for running the file from a checkout
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "module_utils"))
    import routeros_service as lib


def main():
    module = AnsibleModule(argument_spec=lib.ARGUMENT_SPEC, supports_check_mode=True)
    result = lib.run_module(module.params, module.check_mode, lib.make_transport(module.params))
    if result.pop("failed", False):
        module.fail_json(**result)
    module.exit_json(**result)


if __name__ == "__main__":
    main()
