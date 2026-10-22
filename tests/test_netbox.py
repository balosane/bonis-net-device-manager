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


# ---- review fixes -------------------------------------------------------------------------------------------------------

import io
import json
import traceback


class Resp:
    def __init__(self, doc, raw=None):
        self.b = raw if raw is not None else json.dumps(doc).encode()

    def read(self, n=-1):
        return self.b if n < 0 else self.b[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def stub(*docs):
    """opener returning the given documents in order; records requests."""
    calls = []
    it = iter(docs)

    def opener(req, timeout=None):
        calls.append(req)
        d = next(it)
        return d if isinstance(d, Resp) else Resp(d)
    opener.calls = calls
    return opener


def page(results, nxt=None, count=None):
    return {"count": len(results) if count is None else count, "next": nxt, "results": results}


@pytest.mark.parametrize("nxt", ["@evil.example/api/?a=1", ":9999/x", "evil.com/x", "//evil.example/x", "///evil/x", "\\\\evil\\x", "/ok\\@evil/x",
                                 "api/dcim/devices/", "/x\r\nHost: evil", "http://nb.local@evil.example/x"])
def test_bad_next_is_refused_and_not_requested(nxt):
    op = stub(page([{"a": 1}], nxt, count=2))
    c = NetBoxClient("http://netbox.local", "tok", opener=op, sleep=lambda s: None)
    # http://user@host form: only path/query are taken, which is safe, so it must still stay on base
    if nxt.startswith("http://"):
        op = stub(page([{"a": 1}], nxt, count=2), page([{"a": 2}]))
        c = NetBoxClient("http://netbox.local", "tok", opener=op, sleep=lambda s: None)
        c.get_all("/api/x/")
        assert all(r.full_url.startswith("http://netbox.local/") for r in op.calls)
        return
    with pytest.raises(NetBoxError):
        c.get_all("/api/x/")
    assert len(op.calls) == 1


def test_next_absolute_and_relative_path_accepted_encoded_chars_stay_in_path():
    op = stub(page([{"a": 1}], "https://other.example/api/x/?offset=1&q=%2F%2Fevil", count=3),
              page([{"a": 2}], "/api/x/?offset=2", count=3), page([{"a": 3}]))
    c = NetBoxClient("http://netbox.local", "tok", opener=op, sleep=lambda s: None)
    assert len(c.get_all("/api/x/")) == 3
    assert [r.full_url for r in op.calls][1] == "http://netbox.local/api/x/?offset=1&q=%2F%2Fevil"


def test_request_target_check_direct():
    c = NetBoxClient("http://netbox.local", "tok", opener=stub())
    for bad in ("x", "//h/x", "/a b", "/a\\b", "/a\nb", "@h/x"):
        with pytest.raises(NetBoxError):
            c._build_request("GET", bad)


@pytest.mark.parametrize("tok", ["abc\n", "a b", "a\r\nX: y", "tab\t", "\x00", "é"])
def test_bad_token_rejected_without_echo(tok):
    with pytest.raises(NetBoxError) as e:
        NetBoxClient("http://x", tok)
    assert "whitespace/control" in str(e.value)
    assert "abc" not in str(e.value) and "X: y" not in str(e.value)


def test_env_token_trailing_newline(monkeypatch):
    monkeypatch.setenv("NB_T2", "supersecret\n")
    with pytest.raises(NetBoxError) as e:
        NetBoxClient.from_env("http://x", "NB_T2")
    assert "supersecret" not in str(e.value) and "whitespace" in str(e.value)


def test_bad_host_header_rejected():
    for h in ("a\r\nb", "a b", ""):
        with pytest.raises(NetBoxError):
            NetBoxClient("http://x", "tok", host_header=h)


def test_value_error_from_opener_never_leaks_token():
    secret = "TOPSECRETVALUE"

    def opener(req, timeout=None):
        raise ValueError(f"Invalid header value b'Bearer {secret}\\n'")
    c = NetBoxClient("http://x", secret, opener=opener, sleep=lambda s: None)
    with pytest.raises(NetBoxError) as e:
        c.list_devices()
    text = "".join(traceback.format_exception(e.value)) + str(e.value) + repr(e.value) + repr(c) + str(c)
    assert secret not in text


def test_real_http_client_rejects_bad_header_without_leak():
    # a Host header the validator would reject is also impossible to reach http.client with
    secret = "REALSECRET"
    with pytest.raises(NetBoxError) as e:
        NetBoxClient("http://127.0.0.1:1", secret + "\n").list_devices()
    assert secret not in "".join(traceback.format_exception(e.value))


@pytest.mark.parametrize("doc", [[1, 2], "x", {"results": "no", "count": 1}, {"count": 1}, {"results": [], "count": "1"},
                                 {"results": [1], "count": 1}])
def test_malformed_list_response(doc):
    c = NetBoxClient("http://nb", "t", opener=stub(doc), sleep=lambda s: None)
    with pytest.raises(NetBoxError, match="not a NetBox list"):
        c.get_all("/api/x/")


def test_truncated_read_detected():
    c = NetBoxClient("http://nb", "t", opener=stub(page([{"a": 1}], None, count=5)), sleep=lambda s: None)
    with pytest.raises(NetBoxError, match="truncated"):
        c.get_all("/api/x/")


def test_repeated_next_and_page_cap():
    op = stub(page([{"a": 1}], "/api/x/?o=1", count=10), page([{"a": 2}], "/api/x/?o=1", count=10))
    with pytest.raises(NetBoxError, match="does not end"):
        NetBoxClient("http://nb", "t", opener=op, sleep=lambda s: None).get_all("/api/x/")
    # distinct next URLs forever, but more pages than count/page-size + 2
    docs = [page([{"a": 0}], "/api/x/?o=0", count=2)] + [page([{"a": i}], f"/api/x/?o={i}", count=2) for i in range(1, 10)]
    with pytest.raises(NetBoxError, match="does not end"):
        NetBoxClient("http://nb", "t", opener=stub(*docs), sleep=lambda s: None).get_all("/api/x/")


def test_body_size_cap():
    big = Resp(None, raw=b"x" * (16 * 1024 * 1024 + 5))
    with pytest.raises(NetBoxError, match="larger than"):
        NetBoxClient("http://nb", "t", opener=stub(big), sleep=lambda s: None).get_all("/api/x/")
