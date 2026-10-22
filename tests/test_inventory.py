from pathlib import Path

import pytest

from bonis_netdev.catalog import find_model, load_models
from bonis_netdev.features import ConfigError
from bonis_netdev.inventory import devices_from_netbox
from bonis_netdev.netbox import NetBoxClient
from bonis_netdev.templates import load_config
from fakenetbox import DEVICES, FakeNetBox, dev

SEED = Path(__file__).resolve().parent.parent / "config"
CFG = load_config(SEED, with_devices=False)


def run(devices=None, **kw):
    with FakeNetBox(devices) as srv:
        c = NetBoxClient(srv.url, "s3cret-token", sleep=lambda s: None)
        return devices_from_netbox(c, CFG, **kw), srv


def test_managed_unmanaged_problems():
    inv, _ = run()
    assert [d.name for d in inv.devices] == ["ap-1", "core-1", "feat-1", "radio-1"]
    assert [n for n, _ in inv.unmanaged] == ["untagged", "untagged-2"]
    assert [n for n, _ in inv.problems] == ["bad-tags", "generic-sw", "no-ip", "unknown-model"]


def test_device_fields():
    inv, _ = run()
    d = {x.name: x for x in inv.devices}
    core = d["core-1"]
    assert (core.model, core.templates, core.region, core.site, core.role, core.host) == (
        "MikroTik hEX S", ("tpl-core-router"[4:],), "cz-east", "site-a", "router", "10.0.0.1")   # prefix length dropped
    assert d["radio-1"].region is None and d["radio-1"].model == "Ubiquiti LHG-5"          # alias, site without region
    assert d["feat-1"].templates == ("feature:dns-standard",)
    assert d["ap-1"].model == "Ubiquiti NanoStation 5AC Loco" and d["ap-1"].params == {}
    assert inv.synthetic_templates == {"feature:dns-standard": ("dns-standard",)}


def test_problem_reasons():
    inv, _ = run()
    p = dict(inv.problems)
    assert "unknown model MikroTik / RB2011UiAS" in p["unknown-model"]
    assert p["no-ip"] == "no primary_ip4"
    assert "unknown template tag tpl-nope" in p["bad-tags"] and "unknown feature tag feat-nope" in p["bad-tags"]
    assert "placeholder/Generic" in p["generic-sw"] and "unknown model" not in p["generic-sw"]


def test_generic_without_placeholder_tag_and_by_model_name():
    inv, _ = run([dev(1, "g1", 5, ["tpl-core-router"])])
    assert "placeholder/Generic" in inv.problems[0][1]


def test_deterministic_and_order_independent():
    a, _ = run()
    b, _ = run(list(reversed(DEVICES)))
    assert a == b


def test_resolution_is_lazy_and_cached():
    _, srv = run()
    paths = [(r[1], tuple(r[2].get("id", []))) for r in srv.requests if r[1] != "/api/dcim/devices/"]
    assert ("/api/dcim/device-types/", ("1",)) in paths and paths.count(("/api/dcim/device-types/", ("1",))) == 1
    assert not any(p[0] == "/api/dcim/sites/" and p[1] == ("9",) for p in paths)


def test_custom_prefixes():
    inv, _ = run([dev(1, "x", 1, ["t-core-router"])], template_prefix="t-")
    assert [d.name for d in inv.devices] == ["x"]


def test_invalid_ip_and_duplicate_and_nameless():
    bad = dev(1, "badip", 1, ["tpl-core-router"], ip="nonsense%d")
    inv, _ = run([bad, dev(2, "dup", 1, ["tpl-core-router"]), dev(3, "dup", 1, ["tpl-core-router"]),
                  {**dev(4, "x", 1, ["tpl-core-router"]), "name": None}])
    p = dict(inv.problems)
    assert "not an address" in p["badip"] and p["dup"] == "duplicate device name in NetBox" and "(id 4)" in p


def test_catalog_aliases():
    models = load_models(SEED / "models")
    assert find_model(models, "MikroTik", "hEX S").model == "MikroTik hEX S"
    assert find_model(models, None, "LHG 5").model == "Ubiquiti LHG-5"
    assert find_model(models, "Ubiquiti", "NS-5AC-Loco").model == "Ubiquiti NanoStation 5AC Loco"
    assert find_model(models, "MikroTik", "hEX Lite") is None


def test_alias_conflict_is_config_error(tmp_path):
    (tmp_path / "m.yml").write_text(
        "- {model: A a, os_family: airos, capabilities: [x.y], aliases: [dup]}\n"
        "- {model: B b, os_family: airos, capabilities: [x.y], aliases: [dup]}\n")
    with pytest.raises(ConfigError, match="ambiguous"):
        load_models(tmp_path)
    (tmp_path / "m.yml").write_text("- {model: A a, os_family: airos, capabilities: [x.y], aliases: 3}\n")
    with pytest.raises(ConfigError, match="aliases"):
        load_models(tmp_path)


def test_device_constructor_compatible():
    from bonis_netdev.templates import Device
    assert Device("a", "m", ("t",)).host is None


def test_only_active_devices_managed():
    off = dev(1, "off", 1, ["tpl-core-router"])
    off["status"] = {"value": "offline"}
    inv, _ = run([off, dev(2, "on", 1, ["tpl-core-router"]), dev(3, "bare", 1, [])])
    assert [d.name for d in inv.devices] == ["on"]
    assert ("off", "status offline") in inv.unmanaged and inv.problems == []


def test_region_slug_kept_lowercase():
    inv, _ = run([dev(1, "x", 1, ["tpl-core-router"])])
    assert inv.devices[0].region == "cz-east"


def test_seed_real_netbox_models_present():
    models = load_models(SEED / "models")
    for nb_mfr, nb_model, cap in (("MikroTik", "hEX", "dns.resolver"), ("MikroTik", "hEX PoE lite", "dns.resolver"),
                                  ("MikroTik", "RB951G-2HnD", "wifi.2ghz.ap"), ("MikroTik", "RB433AH", "dns.resolver"),
                                  ("MikroTik", "CRS326-24G-2S+RM", "ntp.client"), ("MikroTik", "CCR1016-12S-1S+", "routing.ospf"),
                                  ("MikroTik", "LHG 60G", "wifi.60ghz.ap"), ("Ubiquiti", "PowerBeam 5AC Gen2", "wifi.5ghz.ap"),
                                  ("Ubiquiti", "PowerBeam 5AC 300", "wifi.5ghz.sta"), ("Ubiquiti", "PowerBox Pro", "dns.resolver")):
        assert cap in find_model(models, nb_mfr, nb_model).capabilities
