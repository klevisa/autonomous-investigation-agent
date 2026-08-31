"""Tier 1 — app/investigations.py's tool-routing wiring (_make_investigation_deps): enrich_indicator/
pivot_indicator go through MCP, the other 3 stay on direct SQL — the same routing lib/mcp_tools.py
itself pins (tests/unit/test_mcp_tools.py), exercised here through the actual in_process call site.

WorkspaceClient is faked BEFORE importing app.investigations — a real one hangs trying to resolve
credentials with no live workspace behind it. That's fine: this module only ever treats `_w` as an
ambient identity handle to pass along (the door-key model — see README), never inspects it itself.
"""
import sys

import pytest

REQUIRED_ENV = {
    "AIA_CATALOG": "cat", "AIA_SCHEMA": "sch", "AIA_LAKEBASE_PROJECT": "proj",
    "AIA_LAKEBASE_BRANCH": "production", "AIA_LAKEBASE_ENDPOINT": "primary",
    "AIA_PG_DATABASE": "db", "AIA_INVESTIGATE_JOB_NAME": "investigate",
    "AIA_WAREHOUSE_ID": "wh-123", "AIA_AGENT_MODE": "in_process",
}


class FakeWorkspaceClient:
    def __init__(self, *a, **k):
        pass


class FakeWarehouseSqlRunner:
    """Records what it was built with and echoes back the SQL statement it was asked to run — enough
    to confirm each direct-SQL tool call is qualified with the right catalog.schema.tool(value)."""

    def __init__(self, workspace, warehouse_id):
        self.workspace, self.warehouse_id = workspace, warehouse_id

    def query(self, statement):
        return [{"sql_statement": statement}]


class FakeMCPClient:
    def __init__(self, name):
        self._name = name

    def call_tool(self, name, arguments):
        return {"content": [{"text": '[{"from": "mcp:%s"}]' % self._name}]}


class FakeGatewayLLM:
    def chat(self, messages, tools=None, max_tokens=None):
        return {}


@pytest.fixture
def investigations_module(monkeypatch):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    import databricks.sdk
    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", FakeWorkspaceClient)
    for name in ("app.investigations", "app.config"):
        sys.modules.pop(name, None)
    import app.investigations as m
    return m


def test_make_investigation_deps_routes_the_5_tools_correctly(investigations_module, monkeypatch):
    import lib.tools
    import lib.mcp_tools
    import lib.llm

    monkeypatch.setattr(lib.tools, "WarehouseSqlRunner", FakeWarehouseSqlRunner)
    monkeypatch.setattr(
        lib.mcp_tools, "make_mcp_clients",
        lambda workspace, catalog, schema: {
            "enrich_indicator": (FakeMCPClient("enrich_indicator"), "enrich_indicator"),
            "pivot_indicator": (FakeMCPClient("pivot_indicator"), f"{catalog}__{schema}__pivot_indicator"),
        })
    monkeypatch.setattr(lib.llm, "GatewayLLM", FakeGatewayLLM)

    tool_fn, llm_fn = investigations_module._make_investigation_deps()

    assert tool_fn("enrich_indicator", "x") == [{"from": "mcp:enrich_indicator"}]
    assert tool_fn("pivot_indicator", "x") == [{"from": "mcp:pivot_indicator"}]
    for name, value in (("blast_radius", "x"), ("get_account_risk", "a"), ("get_account_actions", "a")):
        rows = tool_fn(name, value)
        assert rows == [{"sql_statement": f"SELECT * FROM cat.sch.{name}('{value}')"}]
