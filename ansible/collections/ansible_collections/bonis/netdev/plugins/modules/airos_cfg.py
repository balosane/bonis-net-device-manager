#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: airos_cfg
short_description: Set keys in the flat config /tmp/system.cfg of a Ubiquiti airOS device
version_added: "0.0.1"
description:
  - Reads C(/tmp/system.cfg) over SSH, compares the requested keys, and (only when writes are enabled in the module code)
    changes exactly those keys and persists with C(cfgmtd).
  - Guarded by a base hash (drift guard) so a plan made against one version of the config is never applied to another.
  - Runs on the controller (use C(connection: local)); needs C(sshpass) and C(ssh). The whole config is never returned.
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
  base:
    description:
      - sha256 (64 hex) of C(/tmp/system.cfg) the plan was built against, or the literal C(current) for no guard.
    type: str
    required: true
  settings:
    description: Config keys and their wanted values (strings). Keys match C([A-Za-z0-9_.-]+).
    type: dict
    required: true
  state:
    description: Only C(present).
    type: str
    default: present
    choices: [present]
attributes:
  check_mode:
    support: full
  diff_mode:
    support: full
author:
  - Bonis
"""

EXAMPLES = r"""
- name: Set resolver on an AP (check first)
  bonis.netdev.airos_cfg:
    host: 192.168.130.66
    username: "{{ ap_user }}"
    password: "{{ ap_password }}"
    base: "3f0c5d1d1d7b2c8d9a3e1b3c5a6f7e8d9c0b1a2f3e4d5c6b7a8f9e0d1c2b3a4f"
    settings:
      resolv.nameserver.1.ip: 192.168.238.119
      resolv.nameserver.2.ip: 192.168.6.1
  connection: local
  check_mode: true
  diff: true
"""

RETURN = r"""
changed:
  description: Whether the device config was (or in check mode would be) changed.
  returned: always
  type: bool
msg:
  description: Human readable result.
  returned: always
  type: str
base_sha256:
  description: sha256 of C(/tmp/system.cfg) as the module saw it before any change.
  returned: always
  type: str
diff:
  description: C(before) and C(after) text of the touched keys only (secret-looking keys are masked).
  returned: always
  type: dict
"""

from ansible.module_utils.basic import AnsibleModule

try:
    from ansible_collections.bonis.netdev.plugins.module_utils import airos_cfg as lib
except ImportError:  # pragma: no cover - fallback for running the file from a checkout
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "module_utils"))
    import airos_cfg as lib


def main():
    module = AnsibleModule(argument_spec=lib.ARGUMENT_SPEC, supports_check_mode=True)
    result = lib.run_module(module.params, module.check_mode, lib.make_transport(module.params))
    if result.pop("failed", False):
        module.fail_json(**result)
    module.exit_json(**result)


if __name__ == "__main__":
    main()
