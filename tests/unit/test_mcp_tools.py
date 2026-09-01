"""Tier 1 — the MCP tool-routing adapter (lib/mcp_tools).

Pins three things caught by live testing against a real workspace (see the module's own comments for
the exact failure each guards against): (1) each MCP tool call uses its OWN input-schema parameter
name (UC_FUNCTION_PARAM) — "ind" for pivot_indicator (managed MCP mirrors the UC function's real SQL
param) but "indicator" for enrich_indicator (our own custom server's tool signature); (2) managed MCP
calls use the mangled catalog__schema__function name, not the bare tool name; and (3) routing sends
enrich_indicator/pivot_indicator to the MCP path and the other 3 tools to the SQL path, unchanged.
"""
import json

from lib.mcp_tools import make_mcp_tool_fn, make_routed_tool_fn, managed_mcp_tool_name


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


def test_managed_mcp_tool_name_mangles_catalog_schema_function():
    # Verified live: Databricks managed MCP names a UC-function tool catalog__schema__function even
    # when the server URL is scoped to a single function — the bare function name 400s on call_tool.
    assert managed_mcp_tool_name("cat", "sch", "pivot_indicator") == "cat__sch__pivot_indicator"


def test_pivot_indicator_call_uses_the_mangled_name_and_the_uc_functions_real_sql_param_name():
    # pivot_indicator is Databricks MANAGED MCP, which mirrors the UC function's own signature, so its
    # MCP schema param is "ind" (demo/src/seed_demo_data.py) — not TOOL_ARG's "indicator" — and it must
    # be called by its mangled name, not the bare "pivot_indicator".
    client = FakeMCPClient(json.dumps([{"ok": True}]))
    fn = make_mcp_tool_fn({"pivot_indicator": (client, "cat__sch__pivot_indicator")})
    fn("pivot_indicator", "http://bad.example/x")
    assert client.calls == [("cat__sch__pivot_indicator", {"ind": "http://bad.example/x"})]


def test_enrich_indicator_call_uses_its_bare_name_and_the_custom_servers_own_param_name():
    # enrich_indicator is OUR OWN custom MCP server (app/mcp_server.py's `def enrich_indicator(indicator)`),
    # reached via a connection-backed MCP Service — verified live: it's called by its bare name (no
    # mangling), and its MCP schema param is "indicator", not "ind" ({"ind": ...} fails pydantic
    # validation against the deployed server: "Field required: indicator"). Do not "fix" either back.
    client = FakeMCPClient(json.dumps([{"ok": True}]))
    fn = make_mcp_tool_fn({"enrich_indicator": (client, "enrich_indicator")})
    fn("enrich_indicator", "http://bad.example/x")
    assert client.calls == [("enrich_indicator", {"indicator": "http://bad.example/x"})]


def test_enrich_indicator_result_is_json_decoded():
    client = FakeMCPClient(json.dumps([{"indicator": "x", "query_status": "ok"}]))
    fn = make_mcp_tool_fn({"enrich_indicator": (client, "enrich_indicator")})
    assert fn("enrich_indicator", "x") == [{"indicator": "x", "query_status": "ok"}]


def test_pivot_indicator_result_parses_the_real_columns_rows_shape():
    # Verified live: Databricks-managed MCP's tools/call result for a RETURNS TABLE function is a JSON
    # OBJECT {"columns": [...], "rows": [[...], ...]} — column-oriented, not a list of row-dicts, and
    # NOT the CSV shape enrich_indicator's own server uses (a separate, unrelated code path).
    text = json.dumps({"columns": ["indicator_value", "campaign_id"],
                       "rows": [["x", "camp-1"]], "is_truncated": False})
    client = FakeMCPClient(text)
    fn = make_mcp_tool_fn({"pivot_indicator": (client, "cat__sch__pivot_indicator")})
    assert fn("pivot_indicator", "x") == [{"indicator_value": "x", "campaign_id": "camp-1"}]


def test_pivot_indicator_result_plain_json_list_also_works():
    client = FakeMCPClient(json.dumps([{"indicator_value": "x", "campaign_id": "camp-1"}]))
    fn = make_mcp_tool_fn({"pivot_indicator": (client, "cat__sch__pivot_indicator")})
    assert fn("pivot_indicator", "x") == [{"indicator_value": "x", "campaign_id": "camp-1"}]


def test_pivot_indicator_result_raises_loudly_on_an_unrecognized_shape():
    client = FakeMCPClient(json.dumps({"unexpected": "shape"}))
    fn = make_mcp_tool_fn({"pivot_indicator": (client, "cat__sch__pivot_indicator")})
    try:
        fn("pivot_indicator", "x")
        assert False, "expected a ValueError"
    except ValueError:
        pass


def test_unconfigured_tool_returns_error_without_raising():
    fn = make_mcp_tool_fn({})
    assert fn("enrich_indicator", "x") == [{"error": "no MCP client configured for tool enrich_indicator"}]


def test_mcp_tool_fn_calls_the_client_directly_no_thread_juggling():
    # make_mcp_tool_fn does a plain, blocking client.call_tool() — no threads. The event-loop concern
    # (DatabricksMCPClient.call_tool uses asyncio.run internally) is handled at the CALL SITES, not here:
    # the app runs investigations on a background thread (no loop), and the investigate job applies
    # nest_asyncio (src/investigate.py) so a blocking call works under the notebook's ambient loop.
    # Verified live in job_warehouse mode after that fix: both MCP tools return real data, agent converges.
    calls = []

    class RecordingClient:
        def call_tool(self, name, arguments):
            calls.append(name)
            return {"content": [{"text": json.dumps([{"ok": True}])}]}

    fn = make_mcp_tool_fn({"enrich_indicator": (RecordingClient(), "enrich_indicator")})
    assert fn("enrich_indicator", "x") == [{"ok": True}]
    assert calls == ["enrich_indicator"]


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
