"""In-process fake NetBox (http.server in a thread). Serves canned JSON, records every request, pages at PAGE results."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

PAGE = 4
MIK = {"id": 1, "name": "MikroTik", "slug": "mikrotik"}
UBNT = {"id": 2, "name": "Ubiquiti", "slug": "ubiquiti"}
GEN = {"id": 3, "name": "Generic", "slug": "generic"}

DEVICE_TYPES = {
    1: {"id": 1, "model": "hEX S", "manufacturer": MIK},
    2: {"id": 2, "model": "LHG 5", "manufacturer": UBNT},
    3: {"id": 3, "model": "NanoStation 5AC Loco", "manufacturer": UBNT},
    4: {"id": 4, "model": "RB2011UiAS", "manufacturer": MIK},
    5: {"id": 5, "model": "Generic switch", "manufacturer": GEN},
}
SITES = {
    1: {"id": 1, "name": "site-a", "slug": "site-a", "region": {"slug": "cz-east"}},
    2: {"id": 2, "name": "site-b", "slug": "site-b", "region": None},
}


def dev(did, name, type_id, tags, ip="10.0.0.%d/24", site=1, role="router", brief=False, **kw):
    dt = {"id": type_id, "model": DEVICE_TYPES[type_id]["model"]}
    if not brief:
        dt["manufacturer"] = DEVICE_TYPES[type_id]["manufacturer"]
    return {"id": did, "name": name, "device_type": dt, "role": {"name": role.title(), "slug": role},
            "site": {"id": site, "name": SITES[site]["name"], "slug": SITES[site]["slug"]},
            "platform": None, "primary_ip4": {"address": ip % did} if ip else None,
            "tags": [{"name": t, "slug": t} for t in tags], "status": {"value": "active"}, **kw}


DEVICES = [
    dev(1, "core-1", 1, ["tpl-core-router", "src-network-yml"], brief=True),          # manufacturer resolved via device-types
    dev(2, "radio-1", 2, ["tpl-core-router"], site=2, role="ptp"),                    # alias 'LHG 5', no region
    dev(3, "ap-1", 3, ["tpl-ap-5g"], role="ap"),                                      # wifi n/a (no driver)... see tests
    dev(4, "feat-1", 1, ["feat-dns-standard"]),                                       # single feature
    dev(5, "unknown-model", 4, ["tpl-core-router"]),                                  # problem
    dev(6, "no-ip", 1, ["tpl-core-router"], ip=None),                                 # problem
    dev(7, "bad-tags", 1, ["tpl-nope", "feat-nope"]),                                 # problem (2 reasons)
    dev(8, "generic-sw", 5, ["tpl-core-router", "placeholder"]),                      # problem
    dev(9, "untagged", 1, ["needs-review"]),                                          # unmanaged
    dev(10, "untagged-2", 1, []),                                                     # unmanaged
]


class FakeNetBox:
    def __init__(self, devices=None, fail_first=0, token="s3cret-token", page=PAGE):
        self.devices = list(DEVICES if devices is None else devices)
        self.requests = []          # (method, path, query, headers)
        self.fail_first = fail_first
        self.token = token
        self.page = page
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                u = urlsplit(self.path)
                q = parse_qs(u.query)
                outer.requests.append((self.command, u.path, q, dict(self.headers)))
                if self.command != "GET":
                    return self._send(405, {"detail": "method not allowed"})
                if outer.fail_first > 0:
                    outer.fail_first -= 1
                    return self._send(503, {"detail": "busy"})
                auth = self.headers.get("Authorization", "")
                if auth not in (f"Bearer {outer.token}", f"Token {outer.token}"):
                    return self._send(403, {"detail": "bad token"})
                if u.path == "/api/dcim/devices/":
                    return self._page(outer.devices, q, u.path)
                if u.path == "/api/dcim/device-types/":
                    return self._page([DEVICE_TYPES[int(i)] for i in q.get("id", []) if int(i) in DEVICE_TYPES], q, u.path)
                if u.path == "/api/dcim/sites/":
                    return self._page([SITES[int(i)] for i in q.get("id", []) if int(i) in SITES], q, u.path)
                self._send(404, {"detail": "not found"})

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _any

            def _page(self, items, q, path):
                limit = min(int(q.get("limit", ["50"])[0]), outer.page)
                off = int(q.get("offset", ["0"])[0])
                nxt = None
                if off + limit < len(items):
                    # absolute link with the WRONG host, as NetBox builds it from the Host header behind a tunnel
                    nxt = f"https://ipam2.skamanet.cz{path}?limit={limit}&offset={off + limit}"
                    for k, vs in q.items():
                        if k not in ("limit", "offset"):
                            nxt += "".join(f"&{k}={v}" for v in vs)
                self._send(200, {"count": len(items), "next": nxt, "previous": None, "results": items[off:off + limit]})

            def _send(self, code, doc):
                body = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()
