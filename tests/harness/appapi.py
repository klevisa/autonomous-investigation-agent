"""Drive the deployed AIA app over its REST API, the way Tines would.

The caller is the DEPLOYER identity (it must hold CAN_USE on the app; a plain SP M2M token is accepted by
the Apps OAuth proxy — verified). Bearer resolution handles three profile shapes: a PAT
(token=dapi…), an M2M SP (client_id/secret → mint via the OIDC client_credentials endpoint), or an
interactive OAuth profile (`databricks auth token`).

Every call gates on `wait_healthy` first: a Databricks App briefly serves an HTML "App Not Available" page
(not JSON) while its container is (re)starting, which would otherwise make json parsing blow up.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import dbx


class AppClient:
    def __init__(self, profile: str, app_name: str):
        self.profile = profile
        self.app_name = app_name
        self._url = None

    @property
    def url(self) -> str:
        # Don't cache an EMPTY resolve: `apps get` can transiently return no `url` (control-plane blip, or the
        # app mid-(re)deploy), and an empty base would build a relative request URL. Re-resolve until non-empty.
        if not self._url:
            self._url = dbx.app_url(self.profile, self.app_name)
        return self._url

    def bearer(self) -> str:
        """A bearer token the app's OAuth proxy accepts for the deployer (PAT | M2M mint | oauth)."""
        tok = dbx.profile_field(self.profile, "token")
        if tok:
            return tok
        cid = dbx.profile_field(self.profile, "client_id")
        sec = dbx.profile_field(self.profile, "client_secret")
        if cid and sec:
            host = dbx.profile_field(self.profile, "host").rstrip("/")
            body = urllib.parse.urlencode(
                {"grant_type": "client_credentials", "scope": "all-apis"}).encode()
            req = urllib.request.Request(f"{host}/oidc/v1/token", data=body, method="POST")
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode())
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp).get("access_token", "")
        # interactive oauth profile
        cp = dbx.cli(self.profile, "auth", "token", check=False)
        try:
            return json.loads(cp.stdout).get("access_token", "")
        except (ValueError, AttributeError):
            return ""

    def _open(self, path: str, method: str = "GET", body: str | None = None):
        req = urllib.request.Request(f"{self.url}{path}", method=method,
                                     data=(body.encode() if body else None))
        req.add_header("Authorization", f"Bearer {self.bearer()}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        return urllib.request.urlopen(req, timeout=60)

    def wait_healthy(self, timeout: int = 180) -> bool:
        """Block until GET /healthz returns 200 (the app's restart window). Returns False on timeout."""
        t = 0
        while True:
            try:
                self._url = None   # re-resolve each poll: a transient empty `apps get` self-heals within the wait
                with self._open("/healthz") as resp:
                    if resp.status == 200:
                        return True
            # ValueError: an empty base URL → urlopen("/healthz") raises "unknown url type" — treat as not-ready.
            except (urllib.error.HTTPError, urllib.error.URLError, ConnectionError, ValueError):
                pass
            t += 5
            if t >= timeout:
                print(f"  (timeout {timeout}s: app /healthz never returned 200)")
                return False
            time.sleep(5)

    @staticmethod
    def _err(e: urllib.error.HTTPError) -> dict:
        """A diagnosable error dict for a non-2xx response — always carries the HTTP status (e.g. a 503
        from app/job saturation), plus the JSON error body if the app returned one."""
        out = {"error": f"HTTP {e.code}"}
        try:
            body = json.loads(e.read().decode())
            if isinstance(body, dict):
                out.update(body)
        except (ValueError, OSError):
            pass
        return out

    def post(self, path: str, body: str) -> dict:
        self.wait_healthy()
        try:
            with self._open(path, "POST", body) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return self._err(e)

    def get(self, path: str) -> dict:
        self.wait_healthy()
        try:
            with self._open(path) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return self._err(e)
