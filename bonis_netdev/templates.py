"""Templates (lists of feature tags), layered variables and the capability check.

compile_config() answers, for every (device, feature): ok / invalid / n/a. Pure and offline."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Model, load_models
from .drivers.base import driver_class
from .features import ConfigError, Feature, check_keys, check_param, documents, load_features, read_yaml, string_list, yaml_files, NAME_RE
from .intents import DhcpNetworks, Dns, IntentError, Ntp, Resolver, Syslog

OK, INVALID, NA = "ok", "invalid", "n/a"
LAYERS = ("global", "region", "site", "role", "device")   # later layer wins


@dataclass(frozen=True)
class Device:
    name: str
    model: str
    templates: tuple[str, ...]
    region: str | None = None
    site: str | None = None
    role: str | None = None
    params: dict = field(default_factory=dict)   # explicit per-device override, beats every layer
    host: str | None = None                      # management address (from NetBox primary_ip4); not used by compile


@dataclass(frozen=True)
class Result:
    device: str
    model: str
    feature: str
    status: str            # ok | invalid | n/a
    reason: str
    params: dict
    intent: str

    def as_dict(self):
        return {"device": self.device, "model": self.model, "feature": self.feature, "result": self.status,
                "reason": self.reason, "params": self.params}


@dataclass(frozen=True)
class Config:
    features: dict[str, Feature]
    models: dict[str, Model]
    templates: dict[str, tuple[str, ...]]
    vars: dict[str, dict]            # layer -> {"": globals} or {name: params}
    devices: dict[str, Device]


def load_templates(directory, features: dict[str, Feature]) -> dict[str, tuple[str, ...]]:
    templates: dict[str, tuple[str, ...]] = {}
    for path in yaml_files(Path(directory)):
        for d in documents(path):
            for name, tags in d.items():
                if not isinstance(name, str) or not NAME_RE.match(name):
                    raise ConfigError(f"{path}: template name {name!r} is not valid")
                if name in templates:
                    raise ConfigError(f"{path}: template {name!r} is defined twice")
                tags = string_list(path, f"template {name}", tags)
                unknown = sorted(set(tags) - features.keys())
                if unknown:
                    raise ConfigError(f"{path}: template {name}: unknown feature tag {', '.join(unknown)}")
                templates[name] = tuple(sorted(set(tags)))
    return templates


def load_vars(directory) -> dict[str, dict]:
    """vars/global.yml is a plain mapping; region|site|role|device.yml map a name to a mapping."""
    base = Path(directory)
    out: dict[str, dict] = {}
    for layer in LAYERS:
        path = base / f"{layer}.yml"
        doc = read_yaml(path) if path.exists() else None
        doc = {} if doc is None else doc
        if not isinstance(doc, dict):
            raise ConfigError(f"{path}: expected a mapping")
        if layer == "global":
            out[layer] = {"": dict(doc)}
            continue
        for name, params in doc.items():
            if not isinstance(params, dict):
                raise ConfigError(f"{path}: {name}: expected a mapping of parameters")
            if not isinstance(name, str):
                raise ConfigError(f"{path}: name {name!r} must be a string (quote it)")
        out[layer] = {n: dict(p) for n, p in doc.items()}
    return out


def load_devices(path, templates: dict, models: dict) -> dict[str, Device]:
    path = Path(path)
    doc = read_yaml(path)
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: expected a mapping of device name to its settings")
    devices: dict[str, Device] = {}
    for name, d in doc.items():
        if not isinstance(name, str):
            raise ConfigError(f"{path}: device name {name!r} must be a string (quote it)")
        if not isinstance(d, dict):
            raise ConfigError(f"{path}: device {name}: expected a mapping")
        check_keys(path, d, {"model", "templates"}, {"site", "region", "role", "params"})
        for k in ("region", "site", "role"):
            if d.get(k) is not None and not isinstance(d[k], str):
                raise ConfigError(f"{path}: device {name}: {k} must be a string, got {d[k]!r} (quote it)")
        if d["model"] not in models:
            raise ConfigError(f"{path}: device {name}: unknown model {d['model']!r}")
        tpls = string_list(path, f"device {name}: templates", d["templates"])
        unknown = sorted(set(tpls) - templates.keys())
        if unknown:
            raise ConfigError(f"{path}: device {name}: unknown template {', '.join(unknown)}")
        if not isinstance(d.get("params", {}), dict):
            raise ConfigError(f"{path}: device {name}: params must be a mapping")
        devices[str(name)] = Device(str(name), d["model"], tuple(tpls), d.get("region"), d.get("site"), d.get("role"),
                                    dict(d.get("params", {})))
    return devices


def load_config(directory, devices_file=None, *, with_devices: bool = True) -> Config:
    """with_devices=False loads features, models, templates and vars only (the inventory comes from elsewhere, e.g. NetBox)."""
    base = Path(directory)
    features = load_features(base / "features")
    models = load_models(base / "models")
    templates = load_templates(base / "templates", features)
    path = devices_file or base / "devices.yml"
    devices = load_devices(path, templates, models) if with_devices else {}
    declared = {p.name for f in features.values() for p in f.params}
    for dev in devices.values():
        unknown = sorted(set(dev.params) - declared)
        if unknown:
            raise ConfigError(f"{path}: device {dev.name}: params {', '.join(map(repr, unknown))} not declared by any feature "
                              f"(typo?)")
    vars_dir = base / "vars"
    layers = load_vars(vars_dir)
    for layer, entries in layers.items():
        for name, mapping in entries.items():
            unknown = sorted(set(mapping) - declared)
            if unknown:
                where = "global" if layer == "global" else f"{name!r}"
                raise ConfigError(f"{vars_dir / (layer + '.yml')}: layer {layer}, {where}: key {', '.join(map(repr, unknown))} "
                                  f"not declared by any feature (typo?)")
    for dev in devices.values():
        for layer, value in (("region", dev.region), ("site", dev.site), ("role", dev.role)):
            layer_file = vars_dir / f"{layer}.yml"
            if value is not None and layer_file.exists() and value not in layers[layer]:
                raise ConfigError(f"{path}: device {dev.name}: {layer} {value!r} is not a key of {layer_file} "
                                  f"(typo? the layer file exists, so every {layer} a device names must be listed in it)")
    return Config(features, models, templates, layers, devices)


def layered_params(cfg: Config, dev: Device) -> dict:
    """global < region < site < role < device (vars) < the device's own `params`."""
    merged = dict(cfg.vars["global"].get("", {}))
    for layer, key in (("region", dev.region), ("site", dev.site), ("role", dev.role), ("device", dev.name)):
        if key is not None:
            merged.update(cfg.vars[layer].get(key, {}))
    merged.update(dev.params)
    return merged


def device_features(cfg: Config, dev: Device) -> list[Feature]:
    """The union of the device's templates, sorted by name; two features of one domain are an error."""
    names = sorted({t for tpl in dev.templates for t in cfg.templates[tpl]})
    by_domain: dict[str, Feature] = {}
    for n in names:
        f = cfg.features[n]
        if f.domain in by_domain:
            raise ConfigError(f"device {dev.name}: features {by_domain[f.domain].name} and {f.name} "
                              f"both manage domain {f.domain!r}")
        by_domain[f.domain] = f
    return [cfg.features[n] for n in names]


def _feature_params(f: Feature, layered: dict, dev: Device) -> dict:
    out = {}
    for p in f.params:
        if p.name in layered:
            out[p.name] = check_param(f, p, layered[p.name])
        elif p.required:
            raise ConfigError(f"device {dev.name}: feature {f.name}: required param {p.name!r} is not set in any layer")
    return out


def compile_device(cfg: Config, dev: Device) -> list[Result]:
    model = cfg.models[dev.model]
    layered = layered_params(cfg, dev)
    results = []
    for f in device_features(cfg, dev):
        missing = sorted(set(f.requires) - model.capabilities)
        if missing:
            status, reason, params = INVALID, f"{dev.model} lacks capability {', '.join(missing)}", {}
        elif f.name not in driver_class(model.os_family).implements:
            status, reason, params = NA, "driver not implemented", {}
        else:
            status, reason, params = OK, "", _feature_params(f, layered, dev)
        results.append(Result(dev.name, dev.model, f.name, status, reason, params, f.intent))
    return results


def compile_config(cfg: Config) -> list[Result]:
    """Every (device, feature), sorted by device then feature: the same input gives the same list."""
    return [r for name in sorted(cfg.devices) for r in compile_device(cfg, cfg.devices[name])]


def dns_intents(results: list[Result]) -> list[tuple[str, Dns]]:
    """(device, Dns intent) for each ok `dns` result, ready for the existing drivers."""
    out = []
    for r in results:
        if r.status != OK or r.intent != "dns":
            continue
        p = r.params
        scope = p.get("scope", "resolver")
        if scope == "resolver":
            sc = Resolver()
        elif scope == "dhcp-networks":
            sc = DhcpNetworks(exclude=tuple(p.get("exclude", ())))
        else:
            raise ConfigError(f"device {r.device}: feature {r.feature}: scope {scope!r} must be resolver or dhcp-networks")
        out.append((r.device, Dns(p["primary"], p.get("secondary"), sc, tuple(p.get("extra", ())),
                                  reason=f"template feature {r.feature}")))
    return out


def ntp_intents(results: list[Result]) -> list[tuple[str, Ntp]]:
    """(device, Ntp intent) for each ok `ntp` result."""
    out = []
    for r in results:
        if r.status == OK and r.intent == "ntp":
            try:
                out.append((r.device, Ntp(tuple(r.params["servers"]), reason=f"template feature {r.feature}")))
            except IntentError as e:
                raise ConfigError(f"device {r.device}: feature {r.feature}: {e}") from None
    return out


def syslog_intents(results: list[Result]) -> list[tuple[str, Syslog]]:
    """(device, Syslog intent) for each ok `syslog` result."""
    out = []
    for r in results:
        if r.status == OK and r.intent == "syslog":
            try:
                out.append((r.device, Syslog(r.params["syslog_host"], r.params.get("syslog_port", 514),
                                             reason=f"template feature {r.feature}")))
            except IntentError as e:
                raise ConfigError(f"device {r.device}: feature {r.feature}: {e}") from None
    return out


def format_matrix(results: list[Result]) -> str:
    """The device x feature table printed by check-templates and reconcile."""
    rows = [("DEVICE", "MODEL", "FEATURE", "RESULT", "REASON")] + [
        (r.device, r.model, r.feature, r.status, r.reason) for r in results]
    w = [max(len(row[i]) for row in rows) for i in range(4)]
    return "\n".join(("  ".join(c.ljust(w[i]) for i, c in enumerate(row[:4])) + "  " + row[4]).rstrip() for row in rows)
