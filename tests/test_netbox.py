import urllib.error

import pytest

from bonis_netdev.netbox import NetBoxClient, NetBoxError
from fakenetbox import FakeNetBox

TOKEN = "s3cret-token"


def client(srv, **kw):
    kw.setdefault("sleep", lambda s: None)
    return NetBoxClient(srv.url, kw.pop("token", TOKEN), **kw)


def test_pagination_follows_next_against_own_base():
    with FakeNetBox() as srv:
        devs = client(srv).list_devices()
        assert len(devs) == 10
        pages = [r for r in srv.requests if r[1] == "/api/dcim/devices/"]
        assert len(pages) == 3                      # 4 + 4 + 2; the wrong host in `next` was never used
        assert pages[0][2]["limit"] == ["200"]


def test_bearer_and_token_headers_and_host_header():
    with FakeNetBox() as srv:
        client(srv, host_header="ipam2.skamanet.cz").get("/api/dcim/sites/", {"id": 1})
        client(srv, token_kind="token").get("/api/dcim/sites/", {"id": 1})
        h1, h2 = srv.requests[0][3], srv.requests[1][3]
        assert h1["Authorization"] == f"Bearer {TOKEN}" and h1["Host"] == "ipam2.skamanet.cz"
        assert h2["Authorization"] == f"Token {TOKEN}" and h2["Host"].startswith("127.0.0.1")


def test_bad_token_kind_and_empty_token():
    with pytest.raises(NetBoxError):
        NetBoxClient("http://x", "t", token_kind="basic")
    with pytest.raises(NetBoxError):
        NetBoxClient("http://x", "")
    with pytest.raises(NetBoxError):
        NetBoxClient("ftp://x", "t")


def test_token_never_in_errors_or_repr():
    with FakeNetBox() as srv:
        with pytest.raises(NetBoxError) as e:
            client(srv, token="wrong-secret-xyz").list_devices()
        assert "wrong-secret-xyz" not in str(e.value) and "HTTP 403" in str(e.value)
        assert "/api/dcim/devices/" in str(e.value) and "limit" not in str(e.value)
        assert TOKEN not in repr(client(srv))
    # connection refused
    c = NetBoxClient("http://127.0.0.1:1", "tok-abc", sleep=lambda s: None, timeout=1)
    with pytest.raises(NetBoxError) as e:
        c.list_devices()
    assert "tok-abc" not in str(e.value) and "connection error" in str(e.value)


def test_read_only_class_and_builder():
    for m in ("post", "put", "patch", "delete", "request", "write"):
        assert not hasattr(NetBoxClient, m)
    with FakeNetBox() as srv:
        c = client(srv)
        for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD"):
            with pytest.raises(NetBoxError, match="read-only"):
                c._build_request(method, "/api/dcim/devices/")
        assert srv.requests == []
    assert [n for n in dir(NetBoxClient) if not n.startswith("_") and n not in
            ("from_env", "get", "get_all", "list_devices", "get_device_type", "get_site")] == []


def test_retries_on_5xx_then_success_and_exhaustion():
    with FakeNetBox(fail_first=2) as srv:
        assert len(client(srv).list_devices()) == 10
        assert len([r for r in srv.requests if r[1] == "/api/dcim/devices/"]) == 5     # 2 failures + 3 pages
    with FakeNetBox(fail_first=3) as srv:
        with pytest.raises(NetBoxError, match="HTTP 503 after 3 attempts"):
            client(srv).list_devices()


def test_no_retry_on_4xx():
    with FakeNetBox() as srv:
        with pytest.raises(NetBoxError):
            client(srv).get("/api/nothing/")
        assert len(srv.requests) == 1


def test_opener_injection_and_redirect_not_followed():
    calls = []

    def opener(req, timeout=None):
        calls.append(req)
        raise urllib.error.URLError("boom")
    c = NetBoxClient("http://nb", "t", opener=opener, sleep=lambda s: None)
    with pytest.raises(NetBoxError):
        c.list_devices()
    assert len(calls) == 3 and calls[0].get_method() == "GET"


def test_typed_reads_are_cached():
    with FakeNetBox() as srv:
        c = client(srv)
        assert c.get_device_type(1)["manufacturer"]["name"] == "MikroTik"
        assert c.get_device_type(1) and c.get_site(1)["region"]["slug"] == "cz-east" and c.get_site(1)
        assert [r[1] for r in srv.requests] == ["/api/dcim/device-types/", "/api/dcim/sites/"]
        with pytest.raises(NetBoxError):
            c.get_site(99)


def test_from_env(monkeypatch):
    monkeypatch.delenv("NB_T", raising=False)
    with pytest.raises(NetBoxError, match="NB_T"):
        NetBoxClient.from_env("http://x", "NB_T")
    monkeypatch.setenv("NB_T", "abc")
    assert NetBoxClient.from_env("http://x", "NB_T", token_kind="token")
