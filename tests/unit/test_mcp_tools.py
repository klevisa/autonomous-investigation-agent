"""Tier 1 — the MCP tool-routing adapter (lib/mcp_tools).

Pins the two things most likely to silently break: (1) MCP calls use each UC function's REAL SQL
parameter name (UC_FUNCTION_PARAM's "ind"), NOT lib/investigator.TOOL_ARG's LLM-facing "indicator" —
see lib/mcp_tools.py's module docstring for why; and (2) routing sends enrich_indicator/pivot_indicator
to the MCP path and the other 3 tools to the SQL path, unchanged.
"""
import json

from lib.mcp_tools import make_mcp_tool_fn, make_routed_tool_fn


class FakeMCPClient:
    """Records call_tool(name, arguments) and returns a canned MCP CallToolResult-shaped dict — a plain
    dict with a "content" list of {"text": ...} blocks is exactly what _extract_text expects, so this
    doesn't need the real `mcp` package installed to exercise the parsing logic."""

    def __init__(self, text):
        self.calls = []
        self._text = text

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": [{"text": self._text}]}


def test_mcp_call_uses_real_uc_param_name_not_llm_facing_name():
    # enrich_indicator/pivot_indicator's real UC function param is "ind" (demo/src/seed_demo_data.py),
    # not TOOL_ARG's "indicator" — a call built with the wrong key would fail against the real function.
    client = FakeMCPClient(json.dumps([{"ok": True}]))
    fn = make_mcp_tool_fn({"enrich_indicator": client})
    fn("enrich_indicator", "http://bad.example/x")
    assert client.calls == [("enrich_indicator", {"ind": "http://bad.example/x"})]


def test_enrich_indicator_result_is_json_decoded():
    client = FakeMCPClient(json.dumps([{"indicator": "x", "query_status": "ok"}]))
    fn = make_mcp_tool_fn({"enrich_indicator": client})
    assert fn("enrich_indicator", "x") == [{"indicator": "x", "query_status": "ok"}]


def test_pivot_indicator_result_falls_back_to_csv_when_not_json():
    # Databricks-managed MCP controls pivot_indicator's serialization; make_mcp_tool_fn must tolerate a
    # CSV-formatted result the same way the JSON-then-CSV fallback in lib/mcp_tools._parse_managed_rows
    # documents (mirrors unitycatalog-ai's own table-valued CSV format — confirmed in app/mcp_server.py).
    csv_text = "indicator_value,campaign_id\nx,camp-1\n"
    client = FakeMCPClient(csv_text)
    fn = make_mcp_tool_fn({"pivot_indicator": client})
    assert fn("pivot_indicator", "x") == [{"indicator_value": "x", "campaign_id": "camp-1"}]


def test_pivot_indicator_result_json_also_works():
    client = FakeMCPClient(json.dumps([{"indicator_value": "x", "campaign_id": "camp-1"}]))
    fn = make_mcp_tool_fn({"pivot_indicator": client})
    assert fn("pivot_indicator", "x") == [{"indicator_value": "x", "campaign_id": "camp-1"}]


def test_unconfigured_tool_returns_error_without_raising():
    fn = make_mcp_tool_fn({})
    assert fn("enrich_indicator", "x") == [{"error": "no MCP client configured for tool enrich_indicator"}]


def test_routed_tool_fn_sends_mcp_tools_to_mcp_and_the_rest_to_sql():
    sql_calls, mcp_calls = [], []

    def sql_tool_fn(name, value):
        sql_calls.append((name, value))
        return [{"from": "sql"}]

    def mcp_tool_fn(name, value):
        mcp_calls.append((name, value))
        return [{"from": "mcp"}]

    tool_fn = make_routed_tool_fn(sql_tool_fn, mcp_tool_fn)

    assert tool_fn("enrich_indicator", "x") == [{"from": "mcp"}]
    assert tool_fn("pivot_indicator", "x") == [{"from": "mcp"}]
    assert tool_fn("blast_radius", "x") == [{"from": "sql"}]
    assert tool_fn("get_account_risk", "a") == [{"from": "sql"}]
    assert tool_fn("get_account_actions", "a") == [{"from": "sql"}]

    assert mcp_calls == [("enrich_indicator", "x"), ("pivot_indicator", "x")]
    assert sql_calls == [("blast_radius", "x"), ("get_account_risk", "a"), ("get_account_actions", "a")]
