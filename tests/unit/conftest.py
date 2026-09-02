"""Unit-tier fixtures for the APP layer.

app/investigations.py and app/main.py parse AIA_* env and construct a WorkspaceClient at IMPORT time, so
app-layer unit tests need the env set and the SDK client faked BEFORE import. `load_app_module` does exactly
that: set env (mode-overridable), fake databricks.sdk.WorkspaceClient, drop the cached app modules, then
import fresh — so the module's globals (agent_mode/IS_JOB/cfg) reflect the test's chosen mode. Everything the
tests touch (the store, jobs API, journal) is then monkeypatched per test — no network, no Lakebase, no LLM.
"""
import importlib
import sys
import types
from contextlib import asynccontextmanager

import pytest

# ── Fake the `mcp` FastMCP SDK (Tier-1: external SDKs are faked, not installed — see tests/requirements.txt,
#    which carries only pytest/fastapi/httpx/pyyaml). app/mcp_server.py imports FastMCP +
#    TransportSecuritySettings at MODULE scope, and app/main.py mounts the server (streamable_http_app) and
#    enters its session manager in the app lifespan; this stub supports exactly that surface and nothing more.
#    (databricks-mcp and unitycatalog-ai are imported lazily inside functions, so they don't need faking to
#    import the modules — the tests that exercise them stub at the call site.)
if "mcp.server.fastmcp" not in sys.modules:
    _mcp = types.ModuleType("mcp")
    _mcp_server_mod = types.ModuleType("mcp.server")
    _fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    _transport_mod = types.ModuleType("mcp.server.transport_security")

    class _FakeSessionManager:
        def run(self):
            @asynccontextmanager
            async def _cm():
                yield
            return _cm()

    class _FakeFastMCP:
        def __init__(self, *a, **k):
            self.session_manager = _FakeSessionManager()

        def tool(self, *a, **k):
            def _decorator(fn):
                return fn
            return _decorator

        def streamable_http_app(self):
            async def _asgi(scope, receive, send):  # minimal ASGI app; unit tests never route to /mcp/
                raise AssertionError("the faked MCP sub-app should not be invoked in unit tests")
            return _asgi

    class _FakeTransportSecuritySettings:
        def __init__(self, *a, **k):
            pass

    _fastmcp_mod.FastMCP = _FakeFastMCP
    _transport_mod.TransportSecuritySettings = _FakeTransportSecuritySettings
    _mcp.server = _mcp_server_mod
    _mcp_server_mod.fastmcp = _fastmcp_mod
    _mcp_server_mod.transport_security = _transport_mod
    sys.modules.update({
        "mcp": _mcp,
        "mcp.server": _mcp_server_mod,
        "mcp.server.fastmcp": _fastmcp_mod,
        "mcp.server.transport_security": _transport_mod,
    })

# Minimal env that satisfies Config.from_env's required keys (+ dummy Databricks auth so a real
# WorkspaceClient construction, if ever unpatched, wouldn't reach for ambient credentials).
BASE_ENV = {
    "AIA_CATALOG": "cat",
    "AIA_SCHEMA": "sch",
    "AIA_LAKEBASE_PROJECT": "proj",
    "AIA_LAKEBASE_BRANCH": "production",
    "AIA_LAKEBASE_ENDPOINT": "primary",
    "AIA_PG_DATABASE": "db",
    "AIA_INVESTIGATE_JOB_NAME": "aia-investigate",
    "AIA_APP_NAME": "aia-app",
    "AIA_WAREHOUSE_ID": "wh-1",
    "DATABRICKS_HOST": "https://example.invalid",
    "DATABRICKS_TOKEN": "dummy",
    "DATABRICKS_CLIENT_ID": "app-sp-client-id",
}

_APP_MODULES = ("app.investigations", "app.main", "app.config")


class FakeWorkspaceClient:
    """Stands in for databricks.sdk.WorkspaceClient at import; tests replace `_w` with what they need."""
    def __init__(self, *a, **k):
        pass


@pytest.fixture
def load_app_module(monkeypatch):
    def _load(module="app.investigations", *, env=None, mode=None):
        full = dict(BASE_ENV)
        if mode is not None:
            full["AIA_AGENT_MODE"] = mode
        full.update(env or {})
        for k, v in full.items():
            monkeypatch.setenv(k, v)
        import databricks.sdk as sdk
        monkeypatch.setattr(sdk, "WorkspaceClient", FakeWorkspaceClient)
        for m in _APP_MODULES:
            sys.modules.pop(m, None)
        return importlib.import_module(module)
    return _load


class FakeStore:
    """A recording PostgresStateStore stand-in. `case` is what load_case returns (None → not found)."""
    def __init__(self, case=None):
        self._case = case
        self._connect = "CONN"          # opaque token passed through to journal.* (which is mocked)
        self.calls = []

    def load_case(self, case_id):
        self.calls.append(("load_case", case_id))
        return dict(self._case) if self._case else None

    def open_investigation(self, case_id, model_endpoint, run_ref, investigated_by):
        self.calls.append(("open_investigation", case_id, model_endpoint, investigated_by))
        return "INV-1"

    def set_job_run_id(self, inv_id, run_id):
        self.calls.append(("set_job_run_id", inv_id, run_id))

    def record_verdict(self, inv_id, case_id, verdict):
        self.calls.append(("record_verdict", inv_id, case_id, verdict))

    def fail_investigation(self, inv_id, case_id, error):
        self.calls.append(("fail_investigation", inv_id, case_id, str(error)))

    def abandon_investigation(self, inv_id, case_id, reason):
        self.calls.append(("abandon_investigation", inv_id, case_id, reason))

    def bump_attempts(self, inv_id):
        self.calls.append(("bump_attempts", inv_id))
        return 2

    def running_investigations(self):
        self.calls.append(("running_investigations",))
        return []

    def _query(self, *a, **k):
        return []

    def names(self):
        return [c[0] for c in self.calls]

    def find(self, name):
        return [c for c in self.calls if c[0] == name]
