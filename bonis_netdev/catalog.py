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


def parse_model(path: Path, d: dict) -> Model:
    check_keys(path, d, {"model", "os_family", "capabilities"})
    if not isinstance(d["model"], str) or not d["model"]:
        raise ConfigError(f"{path}: model must be a non-empty string")
    if d["os_family"] not in OS_FAMILIES:
        raise ConfigError(f"{path}: model {d['model']}: os_family {d['os_family']!r} must be one of {', '.join(OS_FAMILIES)}")
    caps = string_list(path, f"model {d['model']}: capabilities", d["capabilities"], CAP_RE)
    return Model(d["model"], d["os_family"], frozenset(caps))


def load_models(directory) -> dict[str, Model]:
    models: dict[str, Model] = {}
    for path in yaml_files(Path(directory)):
        for d in documents(path):
            m = parse_model(path, d)
            if m.model in models:
                raise ConfigError(f"{path}: model {m.model!r} is defined twice")
            models[m.model] = m
    return models
