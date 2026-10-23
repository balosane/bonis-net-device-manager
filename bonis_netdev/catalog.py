"""Model catalog (config/models/*.yml): which capabilities a device model has.

Later the capabilities will be read from NetBox device types instead of YAML."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .features import CAP_RE, ConfigError, check_keys, documents, string_list, yaml_files

OS_FAMILIES = ("routeros", "airos")


@dataclass(frozen=True)
class Model:
    model: str
    os_family: str
    capabilities: frozenset[str]
    aliases: tuple[str, ...] = ()    # other names of this model, e.g. the NetBox device type 'hEX S'


def parse_model(path: Path, d: dict) -> Model:
    check_keys(path, d, {"model", "os_family", "capabilities"}, {"aliases"})
    if not isinstance(d["model"], str) or not d["model"]:
        raise ConfigError(f"{path}: model must be a non-empty string")
    if d["os_family"] not in OS_FAMILIES:
        raise ConfigError(f"{path}: model {d['model']}: os_family {d['os_family']!r} must be one of {', '.join(OS_FAMILIES)}")
    caps = string_list(path, f"model {d['model']}: capabilities", d["capabilities"], CAP_RE)
    aliases = string_list(path, f"model {d['model']}: aliases", d.get("aliases", []))
    return Model(d["model"], d["os_family"], frozenset(caps), aliases)


def load_models(directory) -> dict[str, Model]:
    models: dict[str, Model] = {}
    for path in yaml_files(Path(directory)):
        for d in documents(path):
            m = parse_model(path, d)
            if m.model in models:
                raise ConfigError(f"{path}: model {m.model!r} is defined twice")
            models[m.model] = m
    seen: dict[str, str] = {}
    for m in models.values():
        for alias in m.aliases:
            if alias in models and alias != m.model or seen.setdefault(alias, m.model) != m.model:
                raise ConfigError(f"{directory}: alias {alias!r} of model {m.model!r} is ambiguous (another model or alias has it)")
    return models


def find_model(models: dict[str, Model], manufacturer: str | None, model: str) -> Model | None:
    """Match a NetBox device type: '<manufacturer> <model>', then '<model>', then an alias (also with the manufacturer prefix)."""
    candidates = ([f"{manufacturer} {model}"] if manufacturer else []) + [model]
    for c in candidates:
        if c in models:
            return models[c]
    for m in models.values():
        if any(a in candidates for a in m.aliases):
            return m
    return None
