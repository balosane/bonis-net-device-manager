"""Read-only NetBox REST client.

Only GET exists: the class has no write method and the request builder refuses every other HTTP method. The token is read
from an environment variable chosen by the caller (never from argv) and never appears in an error message or a repr."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

PAGE_LIMIT = 200


class NetBoxError(RuntimeError):
    """A NetBox request failed. The message names the URL path (never the query string or the token)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):   # a redirect would re-send the Authorization header elsewhere
        return None


def default_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect).open


class NetBoxClient:
    def __init__(self, base_url: str, token: str, *, token_kind: str = "bearer", host_header: str | None = None,
                 timeout: float = 15.0, retries: int = 2, opener=None, sleep=time.sleep):
        if token_kind not in ("bearer", "token"):
            raise NetBoxError(f"token kind {token_kind!r} must be bearer or token")
        if not token:
            raise NetBoxError("empty NetBox token")
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise NetBoxError(f"base URL {base_url!r} must look like http(s)://host[:port]")
        self._base = f"{parts.scheme}://{parts.netloc}"
        self._token = token
        self._kind = token_kind
        self._host_header = host_header
        self._timeout = timeout
        self._retries = retries
        self._opener = opener or default_opener()
        self._sleep = sleep
        self._device_types: dict[int, dict] = {}
        self._sites: dict[int, dict] = {}

    @classmethod
    def from_env(cls, base_url: str, token_env: str, **kw) -> "NetBoxClient":
        token = os.environ.get(token_env)
        if not token:
            raise NetBoxError(f"environment variable {token_env} is not set or empty (it must hold the NetBox API token)")
        return cls(base_url, token, **kw)

    def __repr__(self):
        return f"NetBoxClient({self._base!r}, token_kind={self._kind!r})"

    # -- request -----------------------------------------------------------------------------------------------------

    def _build_request(self, method: str, path_and_query: str) -> urllib.request.Request:
        if method != "GET":
            raise NetBoxError(f"refusing {method} {path_and_query.split('?')[0]}: this client is read-only (GET only)")
        scheme = "Bearer" if self._kind == "bearer" else "Token"
        req = urllib.request.Request(self._base + path_and_query, method="GET")
        req.add_header("Authorization", f"{scheme} {self._token}")
        req.add_header("Accept", "application/json")
        if self._host_header:
            req.add_header("Host", self._host_header)
        return req

    def _get_json(self, path_and_query: str):
        shown = path_and_query.split("?")[0]
        last = ""
        for attempt in range(self._retries + 1):
            req = self._build_request("GET", path_and_query)
            try:
                with self._opener(req, timeout=self._timeout) as resp:
                    body = resp.read()
                try:
                    return json.loads(body)
                except ValueError:
                    raise NetBoxError(f"GET {shown}: response is not JSON") from None
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    raise NetBoxError(f"GET {shown}: HTTP {e.code}") from None
                last = f"HTTP {e.code}"
            except NetBoxError:
                raise
            except (urllib.error.URLError, OSError) as e:
                last = f"connection error ({type(e).__name__})"
            if attempt < self._retries:
                self._sleep(0.5 * (attempt + 1))
        raise NetBoxError(f"GET {shown}: {last} after {self._retries + 1} attempts")

    def get(self, path: str, params: dict | None = None):
        q = {"limit": PAGE_LIMIT, **(params or {})}
        return self._get_json(f"{path}?{urllib.parse.urlencode(q, doseq=True)}")

    def get_all(self, path: str, params: dict | None = None) -> list[dict]:
        """All results of a list endpoint, following `next`. Only the path and query of `next` are used, against our own
        base URL: NetBox builds that link from the Host header, which is wrong behind an SSH tunnel."""
        doc = self.get(path, params)
        out = list(doc.get("results", []))
        seen = 0
        while doc.get("next"):
            seen += 1
            if seen > 10000:
                raise NetBoxError(f"GET {path}: pagination does not end")
            nxt = urllib.parse.urlsplit(doc["next"])
            doc = self._get_json(nxt.path + (f"?{nxt.query}" if nxt.query else ""))
            out.extend(doc.get("results", []))
        return out

    # -- typed reads -------------------------------------------------------------------------------------------------

    def list_devices(self) -> list[dict]:
        return self.get_all("/api/dcim/devices/")

    def get_device_type(self, type_id: int) -> dict:
        if type_id not in self._device_types:
            res = self.get_all("/api/dcim/device-types/", {"id": type_id})
            if not res:
                raise NetBoxError(f"GET /api/dcim/device-types/: device type {type_id} not found")
            self._device_types[type_id] = res[0]
        return self._device_types[type_id]

    def get_site(self, site_id: int) -> dict:
        if site_id not in self._sites:
            res = self.get_all("/api/dcim/sites/", {"id": site_id})
            if not res:
                raise NetBoxError(f"GET /api/dcim/sites/: site {site_id} not found")
            self._sites[site_id] = res[0]
        return self._sites[site_id]
