"""A driver turns an abstract intent into concrete steps for one class of device.

Contract: plan() is a pure function of (intent, what was read). It sends nothing."""
from __future__ import annotations

import hashlib
import re

from ..intents import Dns
from ..plan import DevicePlan
from ..transport import Transport


class Driver:
    name = "base"

    def read_commands(self, intent) -> list[str]:
        raise NotImplementedError

    def plan(self, device: str, intent, live: dict[str, str], host: str) -> DevicePlan:
        raise NotImplementedError

    @staticmethod
    def fingerprint(live: dict[str, str], redact=lambda text: text) -> str:
        blob = "\n".join(f"{k}\n{redact(live[k])}" for k in sorted(live))
        return hashlib.sha256(blob.encode()).hexdigest()


def build_device_plan(driver: Driver, device: str, intent, transport: Transport) -> DevicePlan:
    live = {cmd: transport.read(cmd) for cmd in driver.read_commands(intent)}
    return driver.plan(device, intent, live, transport.host)


def detect_driver(transport: Transport) -> Driver:
    """Pick the driver from what the device says about itself."""
    from .airos import AirosDriver
    from .routeros import RouterOsDriver
    try:
        res = transport.read("/system resource print")
        m = re.search(r"version:\s*(\d+)\.", res)
        if m and "uptime" in res:
            return RouterOsDriver(int(m.group(1)))
    except Exception:
        pass
    cfg = transport.read("cat /tmp/system.cfg")
    if "=" in cfg:
        return AirosDriver()
    raise RuntimeError(f"{transport.host}: unknown device class")
