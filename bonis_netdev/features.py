"""Feature definitions (config/features/*.yml): what a tag in a template means and what it needs."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


class ConfigError(ValueError):
    """The templates / features / catalog configuration is wrong; the message names file and field."""


NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CAP_RE = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)*$")
PARAM_TYPES = ("ip", "str", "int", "bool", "list")


def read_yaml(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from None


def yaml_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise ConfigError(f"{directory}: directory not found")
    return sorted(directory.glob("*.yml"))


def documents(path: Path) -> list[dict]:
    """A file holds one mapping or a list of mappings."""
    doc = read_yaml(path)
    docs = doc if isinstance(doc, list) else [doc]
    for d in docs:
        if not isinstance(d, dict):
            raise ConfigError(f"{path}: expected a mapping or a list of mappings")
    return docs


def check_keys(path: Path, doc: dict, required: set[str], optional: set[str] = frozenset()):
    missing = sorted(required - doc.keys())
    if missing:
        raise ConfigError(f"{path}: missing {', '.join(missing)}")
    unknown = sorted(doc.keys() - required - optional)
    if unknown:
        raise ConfigError(f"{path}: unknown field {', '.join(unknown)}")


def string_list(path: Path, what: str, value, pattern: re.Pattern | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise ConfigError(f"{path}: {what} must be a list of strings")
    if pattern:
        for x in value:
            if not pattern.match(x):
                raise ConfigError(f"{path}: {what}: {x!r} is not valid")
    return tuple(value)


@dataclass(frozen=True)
class Param:
    name: str
    type: str
    required: bool


@dataclass(frozen=True)
class Feature:
    name: str
    domain: str
    requires: tuple[str, ...]
    params: tuple[Param, ...]
    intent: str
    protect: bool = False   # reserved: removal of the feature will need break-glass


def _param(path: Path, name, spec) -> Param:
    if not isinstance(name, str) or not isinstance(spec, str):
        raise ConfigError(f"{path}: params must map a name to a type like 'ip' or 'ip?'")
    optional = spec.endswith("?")
    typ = spec.rstrip("?")
    if typ not in PARAM_TYPES:
        raise ConfigError(f"{path}: param {name}: unknown type {typ!r} (use {', '.join(PARAM_TYPES)})")
    return Param(name, typ, not optional)


def parse_feature(path: Path, d: dict) -> Feature:
    check_keys(path, d, {"name", "domain", "requires", "intent"}, {"params", "protect"})
    for k in ("name", "domain", "intent"):
        if not isinstance(d[k], str) or not NAME_RE.match(d[k]):
            raise ConfigError(f"{path}: {k}: {d[k]!r} is not a valid name (lowercase letters, digits, '-')")
    if not isinstance(d.get("protect", False), bool):
        raise ConfigError(f"{path}: protect must be true or false")
    params = d.get("params") or {}
    if not isinstance(params, dict):
        raise ConfigError(f"{path}: params must be a mapping")
    return Feature(
        name=d["name"], domain=d["domain"],
        requires=tuple(sorted(set(string_list(path, "requires", d["requires"], CAP_RE)))),
        params=tuple(_param(path, n, s) for n, s in sorted(params.items())),
        intent=d["intent"], protect=d.get("protect", False))


def load_features(directory) -> dict[str, Feature]:
    features: dict[str, Feature] = {}
    for path in yaml_files(Path(directory)):
        for d in documents(path):
            f = parse_feature(path, d)
            if f.name in features:
                raise ConfigError(f"{path}: feature {f.name!r} is defined twice")
            features[f.name] = f
    return features


def check_param(feature: Feature, p: Param, value):
    """Return the value if it fits the declared type, else raise ConfigError."""
    ok = {"str": isinstance(value, str), "bool": isinstance(value, bool),
          "int": isinstance(value, int) and not isinstance(value, bool),
          "list": isinstance(value, list) and all(isinstance(x, str) for x in value),
          "ip": False}[p.type]
    if p.type == "ip":
        try:
            ok = isinstance(value, str) and bool(ipaddress.ip_address(value))
        except ValueError:
            ok = False
    if not ok:
        raise ConfigError(f"feature {feature.name}: param {p.name}={value!r} is not of type {p.type}")
    return value
