"""Device inventory from NetBox: the desired state is the template / feature tags on the NetBox device.

Convention: a tag `tpl-<template>` selects a template from git, a tag `feat-<feature>` adds one feature. Only devices with at
least one such tag are managed. Read-only: nothing is written to NetBox or to a device."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from .catalog import find_model
from .templates import Device

SYNTHETIC_PREFIX = "feature:"   # name of the one-feature template made for a `feat-` tag (not a valid git template name)


@dataclass
class InventoryResult:
    devices: list[Device] = field(default_factory=list)                 # managed, sorted by name
    unmanaged: list[tuple[str, str]] = field(default_factory=list)      # (name, reason), sorted
    problems: list[tuple[str, str]] = field(default_factory=list)       # (name, reason): hard errors, sorted
    synthetic_templates: dict[str, tuple[str, ...]] = field(default_factory=dict)   # "feature:<f>" -> (f,)


def _tag_names(dev: dict) -> list[str]:
    return sorted({(t.get("slug") or t.get("name") or "").lower() for t in dev.get("tags") or [] if isinstance(t, dict)} - {""})


def _is_placeholder(tags: list[str], manufacturer: dict | None, model: str) -> bool:
    return ("placeholder" in tags or (manufacturer or {}).get("slug", "").lower() == "generic"
            or model.lower().startswith("generic"))


def devices_from_netbox(client, catalog, *, template_prefix: str = "tpl-", feature_prefix: str = "feat-") -> InventoryResult:
    """`catalog` is a templates.Config (features, models, templates are used; its devices are ignored)."""
    res = InventoryResult()
    seen: set[str] = set()
    raw = sorted(client.list_devices(), key=lambda d: (str(d.get("name") or ""), d.get("id") or 0))
    for d in raw:
        name = d.get("name")
        tags = _tag_names(d)
        tpl_tags = [t[len(template_prefix):] for t in tags if t.startswith(template_prefix)]
        feat_tags = [t[len(feature_prefix):] for t in tags if t.startswith(feature_prefix)]
        if not name:
            if tpl_tags or feat_tags:
                res.problems.append((f"(id {d.get('id')})", "device has no name"))
            else:
                res.unmanaged.append((f"(id {d.get('id')})", "no template/feature tag"))
            continue
        if not tpl_tags and not feat_tags:
            res.unmanaged.append((name, f"no {template_prefix}/{feature_prefix} tag"))
            continue
        if name in seen:
            res.problems.append((name, "duplicate device name in NetBox"))
            continue
        seen.add(name)
        why: list[str] = []

        dtype = d.get("device_type") or {}
        manufacturer = dtype.get("manufacturer")
        if dtype.get("id") is not None and manufacturer is None:
            manufacturer = client.get_device_type(dtype["id"]).get("manufacturer")
        nb_model = dtype.get("model") or ""
        placeholder = _is_placeholder(tags, manufacturer, nb_model)
        model = None
        if placeholder:
            why.append("placeholder/Generic device carries a template/feature tag (assign a real device type first)")
        else:
            model = find_model(catalog.models, (manufacturer or {}).get("name"), nb_model)
            if model is None:
                why.append(f"unknown model {((manufacturer or {}).get('name') or '?')} / {nb_model or '?'} (not in the model catalog, "
                           f"no matching alias)")

        host = None
        ip4 = (d.get("primary_ip4") or {}).get("address")
        if not ip4:
            why.append("no primary_ip4")
        else:
            try:
                host = str(ipaddress.ip_interface(ip4).ip)
            except ValueError:
                why.append(f"primary_ip4 {ip4!r} is not an address")

        for t in tpl_tags:
            if t not in catalog.templates:
                why.append(f"unknown template tag {template_prefix}{t}")
        for t in feat_tags:
            if t not in catalog.features:
                why.append(f"unknown feature tag {feature_prefix}{t}")
        if why:
            res.problems.append((name, "; ".join(why)))
            continue

        site = d.get("site") or {}
        region = None
        if site.get("id") is not None:
            r = (client.get_site(site["id"]).get("region") or {}).get("slug")
            region = r.upper() if r else None
        templates = sorted(set(tpl_tags))
        for f in sorted(set(feat_tags)):
            syn = SYNTHETIC_PREFIX + f
            res.synthetic_templates[syn] = (f,)
            templates.append(syn)
        res.devices.append(Device(name, model.model, tuple(templates), region, site.get("name"),
                                  (d.get("role") or {}).get("slug"), {}, host))
    res.devices.sort(key=lambda x: x.name)
    res.unmanaged.sort()
    res.problems.sort()
    return res
