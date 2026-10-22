import json
import shutil
from pathlib import Path

import pytest
import yaml

from bonis_netdev import cli
from bonis_netdev.features import ConfigError, load_features
from bonis_netdev.catalog import load_models
from bonis_netdev.intents import DhcpNetworks, IntentError, Ntp, Resolver
from bonis_netdev.templates import (compile_config, compile_device, dns_intents, layered_params, load_config)

SEED = Path(__file__).resolve().parent.parent / "config"

FEATURES = {
    "dns-standard": {"name": "dns-standard", "domain": "dns", "requires": ["dns.resolver"], "intent": "dns",
                     "params": {"primary": "ip", "secondary": "ip?", "scope": "str?", "exclude": "list?"}},
    "ntp-standard": {"name": "ntp-standard", "domain": "ntp", "requires": ["ntp.client"], "intent": "ntp",
                     "params": {"servers": "list"}},
    "ntp-public": {"name": "ntp-public", "domain": "ntp", "requires": ["ntp.client"], "intent": "ntp"},
    "wifi-ap-5g": {"name": "wifi-ap-5g", "domain": "wifi", "requires": ["wifi.5ghz.ap"], "intent": "wifi"},
}
MODELS = [
    {"model": "R", "os_family": "routeros", "capabilities": ["dns.resolver", "ntp.client"]},
    {"model": "A", "os_family": "airos", "capabilities": ["dns.resolver", "ntp.client", "wifi.5ghz.ap"]},
]
TEMPLATES = {"core": ["dns-standard"], "time": ["ntp-standard"], "ap": ["wifi-ap-5g"], "pub": ["ntp-public"]}
VARS = {"global.yml": {"primary": "10.0.0.1", "servers": ["t.example"]},
        "region.yml": {"r1": {"primary": "10.0.0.2"}},
        "site.yml": {"s1": {"primary": "10.0.0.3"}},
        "role.yml": {"edge": {"primary": "10.0.0.4"}},
        "device.yml": {"d1": {"primary": "10.0.0.5"}}}
DEVICES = {"d1": {"model": "R", "region": "r1", "site": "s1", "role": "edge", "templates": ["core"]}}


def write(base, rel, data):
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data))


@pytest.fixture
def conf(tmp_path):
    def build(devices=None, templates=None, models=None, features=None, vars_=None):
        for n, f in (features or FEATURES).items():
            write(tmp_path, f"features/{n}.yml", f)
        write(tmp_path, "models/m.yml", models or MODELS)
        write(tmp_path, "templates/t.yml", templates or TEMPLATES)
        for n, v in (vars_ or VARS).items():
            write(tmp_path, f"vars/{n}", v)
        write(tmp_path, "devices.yml", devices or DEVICES)
        return tmp_path
    return build


def status(results):
    return {(r.device, r.feature): (r.status, r.reason) for r in results}


def test_layering_order(conf):
    base = conf()
    cfg = load_config(base)
    dev = cfg.devices["d1"]
    assert layered_params(cfg, dev)["primary"] == "10.0.0.5"
    for drop, expect in (("device.yml", "10.0.0.4"), ("role.yml", "10.0.0.3"), ("site.yml", "10.0.0.2"),
                         ("region.yml", "10.0.0.1")):
        (base / "vars" / drop).unlink()
        assert layered_params(load_config(base), dev)["primary"] == expect


def test_device_params_override_every_layer(conf):
    d = {"d1": {**DEVICES["d1"], "params": {"primary": "10.9.9.9"}}}
    r = compile_config(load_config(conf(devices=d)))
    assert r[0].params == {"primary": "10.9.9.9"}


def test_multiple_templates_union(conf):
    d = {"d1": {"model": "R", "templates": ["core", "time", "core"]}}
    r = compile_config(load_config(conf(devices=d)))
    assert [x.feature for x in r] == ["dns-standard", "ntp-standard"]


def test_ok_invalid_na(conf):
    d = {"r": {"model": "R", "templates": ["core", "ap", "time"]}, "a": {"model": "A", "templates": ["ap"]}}
    st = status(compile_config(load_config(conf(devices=d))))
    assert st[("r", "dns-standard")] == ("ok", "")
    assert st[("r", "wifi-ap-5g")] == ("invalid", "R lacks capability wifi.5ghz.ap")
    assert st[("r", "ntp-standard")] == ("n/a", "driver not implemented")
    assert st[("a", "wifi-ap-5g")] == ("n/a", "driver not implemented")


def test_domain_conflict_names_both(conf):
    d = {"d1": {"model": "R", "templates": ["time", "pub"]}}
    with pytest.raises(ConfigError, match=r"ntp-public and ntp-standard|ntp-standard and ntp-public"):
        compile_config(load_config(conf(devices=d)))


def test_unknown_tag_template_and_model(conf):
    with pytest.raises(ConfigError, match="unknown feature tag nope"):
        load_config(conf(templates={"x": ["nope"]}, devices={"d": {"model": "R", "templates": ["x"]}}))


def test_unknown_model_and_template(conf):
    with pytest.raises(ConfigError, match="unknown model 'Z'"):
        load_config(conf(devices={"d": {"model": "Z", "templates": ["core"]}}))
    with pytest.raises(ConfigError, match="unknown template zzz"):
        load_config(conf(devices={"d": {"model": "R", "templates": ["zzz"]}}))


def test_missing_required_param(conf):
    v = {"global.yml": {}}
    with pytest.raises(ConfigError, match="required param 'primary'"):
        compile_config(load_config(conf(vars_=v)))


def test_bad_param_type(conf):
    v = {"global.yml": {"primary": "not-an-ip"}}
    with pytest.raises(ConfigError, match="primary='not-an-ip' is not of type ip"):
        compile_config(load_config(conf(vars_=v)))


def test_deterministic(conf):
    d = {"z": {"model": "R", "templates": ["time", "core"]}, "a": {"model": "A", "templates": ["ap", "core"]}}
    cfg = load_config(conf(devices=d))
    first = [r.as_dict() for r in compile_config(cfg)]
    assert first == [r.as_dict() for r in compile_config(cfg)]
    keys = [(r["device"], r["feature"]) for r in first]
    assert keys == sorted(keys)


def test_schema_errors(tmp_path):
    write(tmp_path, "f/a.yml", {"name": "x", "domain": "d", "intent": "dns"})
    with pytest.raises(ConfigError, match="a.yml: missing requires"):
        load_features(tmp_path / "f")
    write(tmp_path, "f/a.yml", {"name": "x", "domain": "d", "intent": "dns", "requires": [], "bogus": 1})
    with pytest.raises(ConfigError, match="unknown field bogus"):
        load_features(tmp_path / "f")
    write(tmp_path, "f/a.yml", {"name": "x", "domain": "d", "intent": "dns", "requires": [], "params": {"p": "blob"}})
    with pytest.raises(ConfigError, match="unknown type 'blob'"):
        load_features(tmp_path / "f")
    write(tmp_path, "m/a.yml", {"model": "M", "os_family": "cisco", "capabilities": []})
    with pytest.raises(ConfigError, match="os_family 'cisco'"):
        load_models(tmp_path / "m")
    (tmp_path / "m" / "a.yml").write_text("model: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_models(tmp_path / "m")
    with pytest.raises(ConfigError, match="directory not found"):
        load_models(tmp_path / "nope")


def test_dns_intents(conf):
    d = {"d1": {"model": "R", "templates": ["core"], "params": {"scope": "dhcp-networks", "exclude": ["192.168.88.0/24"]}},
         "d2": {"model": "R", "templates": ["core", "time"]}}
    out = dict(dns_intents(compile_config(load_config(conf(devices=d)))))
    assert out["d1"].scope == DhcpNetworks(exclude=("192.168.88.0/24",))
    assert out["d2"].scope == Resolver() and out["d2"].primary == "10.0.0.1"
    assert len(out) == 2


def test_ntp_intent():
    assert Ntp(("a.example", "10.0.0.1")).as_dict()["servers"] == ["a.example", "10.0.0.1"]
    for bad in ((), ("bad host",), ("a", "A")):
        with pytest.raises(IntentError):
            Ntp(bad)


def test_seed_loads_and_matrix():
    cfg = load_config(SEED)
    st = status(compile_config(cfg))
    assert st[("rt-site-b-1", "wifi-ap-5g")][0] == "invalid"
    assert st[("rt-site-a-1", "dns-standard")][0] == "ok"
    ex = status(compile_config(load_config(SEED, SEED / "devices.example.yml")))
    assert "invalid" not in {s for s, _ in ex.values()}


def test_seed_dns_values_come_from_layers():
    cfg = load_config(SEED, SEED / "devices.example.yml")
    out = dict(dns_intents(compile_config(cfg)))
    assert out["rt-site-a-1"].servers == ("192.168.238.120", "192.168.6.2")
    assert isinstance(out["rt-site-a-1"].scope, DhcpNetworks)
    assert out["rt-site-a-2"].servers == ("192.168.238.119", "192.168.6.9")


def test_cli_exit_codes(capsys):
    assert cli.main(["check-templates", "--config", str(SEED)]) == 1
    assert "invalid" in capsys.readouterr().out
    assert cli.main(["check-templates", "--config", str(SEED), "--devices", str(SEED / "devices.example.yml")]) == 0
    capsys.readouterr()
    assert cli.main(["check-templates", "--config", str(SEED), "--json"]) == 1
    rows = json.loads(capsys.readouterr().out)
    assert {r["result"] for r in rows} == {"ok", "invalid", "n/a"}


def test_cli_config_error_exits_1(tmp_path, capsys):
    shutil.copytree(SEED, tmp_path / "c")
    (tmp_path / "c" / "templates" / "default.yml").write_text("x: [nope]\n")
    assert cli.main(["check-templates", "--config", str(tmp_path / "c")]) == 1
    assert "unknown feature tag nope" in capsys.readouterr().err


@pytest.mark.parametrize("field,value", [("region", 7), ("site", ["s1"]), ("role", True)])
def test_region_site_role_must_be_strings(conf, field, value):
    d = {"d1": {**DEVICES["d1"], field: value}}
    with pytest.raises(ConfigError, match=rf"{field} must be a string"):
        load_config(conf(devices=d))


def test_non_string_device_and_layer_names_rejected(conf):
    with pytest.raises(ConfigError, match="device name 12 must be a string"):
        load_config(conf(devices={12: {"model": "R", "templates": ["core"]}}))
    with pytest.raises(ConfigError, match="name 5 must be a string"):
        load_config(conf(vars_={**VARS, "site.yml": {5: {"primary": "10.0.0.3"}}}))


def test_unknown_device_param_key_is_an_error_naming_it(conf):
    d = {"d1": {**DEVICES["d1"], "params": {"primary": "10.0.0.9", "primry": "typo"}}}
    with pytest.raises(ConfigError, match="'primry' not declared by any feature"):
        load_config(conf(devices=d))
    # a param declared by ANY feature (even one the device does not use) is accepted
    d = {"d1": {**DEVICES["d1"], "params": {"servers": ["x.example"]}}}
    assert load_config(conf(devices=d)).devices["d1"].params == {"servers": ["x.example"]}


@pytest.mark.parametrize("layer,content", [
    ("global.yml", {"primary": "10.0.0.1", "secundary": "10.0.0.2"}),
    ("region.yml", {"r1": {"secundary": "10.0.0.2"}}),
    ("site.yml", {"s1": {"secundary": "10.0.0.2"}}),
    ("role.yml", {"edge": {"secundary": "10.0.0.2"}}),
    ("device.yml", {"d1": {"secundary": "10.0.0.2"}}),
])
def test_unknown_key_in_any_vars_layer_is_rejected(conf, layer, content):
    vars_ = {**VARS, layer: content}
    with pytest.raises(ConfigError) as e:
        load_config(conf(vars_=vars_))
    msg = str(e.value)
    assert layer in msg and f"layer {layer[:-4]}" in msg and "'secundary'" in msg


@pytest.mark.parametrize("field,layer", [("region", "region.yml"), ("site", "site.yml"), ("role", "role.yml")])
def test_device_naming_absent_key_of_existing_layer_file_is_rejected(conf, field, layer):
    d = {"d1": {**DEVICES["d1"], field: "nope"}}
    with pytest.raises(ConfigError, match=f"{field} 'nope' is not a key of .*{layer}"):
        load_config(conf(devices=d))


def test_missing_layer_file_or_unset_field_is_allowed(conf):
    d = {"d1": {**DEVICES["d1"], "site": "anything"}}
    base = conf(devices=d, vars_={k: v for k, v in VARS.items() if k != "site.yml"})
    assert load_config(base).devices["d1"].site == "anything"
    d = {"d1": {"model": "R", "templates": ["core"]}}       # region/site/role unset, layer files exist
    assert load_config(conf(devices=d)).devices["d1"].region is None
