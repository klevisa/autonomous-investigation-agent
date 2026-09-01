"""Tier 1 — app/mcp_server.py: the enrich_indicator MCP tool's result-shape normalization (_rows) and
its execution call (_execute_enrich_indicator). unitycatalog-ai is faked via sys.modules injection so
this test doesn't need the real package installed — mirrors how this repo fakes other external SDKs
(e.g. boto3) at the Tier-1 layer rather than requiring them present.
"""
import json
import sys
from dataclasses import dataclass

import pytest

# app/mcp_server.py's Config.from_env() needs the app's full required env set (see app/config.py) even
# though this module only reads catalog/schema — set it once here rather than duplicating per test.
REQUIRED_ENV = {
    "AIA_CATALOG": "cat", "AIA_SCHEMA": "sch", "AIA_LAKEBASE_PROJECT": "proj",
    "AIA_LAKEBASE_BRANCH": "production", "AIA_LAKEBASE_ENDPOINT": "primary",
    "AIA_PG_DATABASE": "db", "AIA_INVESTIGATE_JOB_NAME": "investigate",
}


@pytest.fixture
def mcp_server_module(monkeypatch):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("app.mcp_server", None)
    sys.modules.pop("app.config", None)
    import app.mcp_server as m
    return m


@dataclass
class FakeResult:
    value: object
    format: str = "CSV"
    error: str = None


def test_rows_parses_csv_table_result(mcp_server_module):
    result = FakeResult(value="indicator,query_status\nx,ok\n", format="CSV")
    assert mcp_server_module._rows(result) == [{"indicator": "x", "query_status": "ok"}]


def test_rows_wraps_a_non_csv_value(mcp_server_module):
    result = FakeResult(value="42", format="SCALAR")
    assert mcp_server_module._rows(result) == [{"value": "42"}]


def test_rows_raises_on_error(mcp_server_module):
    result = FakeResult(value=None, format="CSV", error="boom")
    with pytest.raises(RuntimeError, match="boom"):
        mcp_server_module._rows(result)


def test_execute_enrich_indicator_calls_the_real_uc_function_serverless(mcp_server_module, monkeypatch):
    calls = []

    class FakeDatabricksFunctionClient:
        def __init__(self, execution_mode):
            calls.append(("init", execution_mode))

        def execute_function(self, full_name, params):
            calls.append(("execute", full_name, params))
            return FakeResult(value="indicator,query_status\nx,ok\n", format="CSV")

    fake_mod = type(sys)("unitycatalog.ai.core.databricks")
    fake_mod.DatabricksFunctionClient = FakeDatabricksFunctionClient
    for name in ("unitycatalog", "unitycatalog.ai", "unitycatalog.ai.core", "unitycatalog.ai.core.databricks"):
        monkeypatch.setitem(sys.modules, name, sys.modules.get(name) or type(sys)(name))
    monkeypatch.setitem(sys.modules, "unitycatalog.ai.core.databricks", fake_mod)

    out = mcp_server_module._execute_enrich_indicator("http://bad.example/x")

    assert calls[0] == ("init", "serverless")
    assert calls[1] == ("execute", "cat.sch.enrich_indicator", {"ind": "http://bad.example/x"})
    assert json.loads(out) == [{"indicator": "x", "query_status": "ok"}]
