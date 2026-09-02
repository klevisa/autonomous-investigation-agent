"""MCP-routed tools — enrich_indicator and pivot_indicator reach their UC functions over MCP instead
of direct SQL: enrich_indicator via a custom MCP server (this repo's app/mcp_server.py) fronted by a
Unity Catalog HTTP Connection + MCP Service; pivot_indicator via Databricks managed MCP
(/api/2.0/mcp/functions/{catalog}/{schema}/pivot_indicator). blast_radius, get_account_risk, and
get_account_actions keep going through lib/tools.make_tool_fn unchanged.

Same tool_fn(name, value) -> list[dict] contract as lib/tools.make_tool_fn (see lib/investigator.py):
this module supplies that contract for the 2 MCP tools, plus a router that combines both halves into
the one tool_fn the Investigator is built with. Exceptions are left to propagate — like
lib/tools.make_tool_fn, this module does not catch them; ToolWieldingAgent._run_tool already turns a
raised exception into [{"error": str(e)}], so catching here would just duplicate that.
"""
import json

# The MCP TOOL's input-schema parameter name for each tool call — NOT lib/investigator.TOOL_ARG's
# LLM-facing arg name ("indicator"), and NOT necessarily the UC function's real SQL parameter name
# either. SQL calls today are positional (`fn('value')`) so TOOL_ARG's name never mattered there; MCP
# calls are NAMED, and the two MCP tools disagree on what that name is:
#   * pivot_indicator (Databricks MANAGED MCP) exposes the UC function directly, so its MCP schema
#     mirrors the function's own SQL parameter name verbatim: "ind" (demo/src/seed_demo_data.py).
#   * enrich_indicator (our CUSTOM MCP server, app/mcp_server.py) is OUR OWN tool function —
#     `def enrich_indicator(indicator: str)` — so its MCP schema uses whatever WE named that Python
#     parameter: "indicator", not the UC function's "ind" (app/mcp_server.py translates indicator->ind
#     internally when it calls the real UC function). Verified live: calling with {"ind": ...} against
#     the deployed custom server fails pydantic validation ("Field required: indicator").
# Do not collapse these to one shared value — they are genuinely different per transport.
UC_FUNCTION_PARAM = {"enrich_indicator": "indicator", "pivot_indicator": "ind"}


def _extract_text(result):
    """Pull the text payload out of an MCP CallToolResult (a list of content blocks; both tools here
    only ever return a single text block)."""
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content", [])
    for block in content or []:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text is not None:
            return text
    return "[]"


def _parse_json_rows(text):
    """enrich_indicator: our own custom server (app/mcp_server.py) returns json.dumps(rows) as its
    tool output — we control that format end-to-end, so this is a plain decode."""
    return json.loads(text)


def _parse_managed_rows(text):
    """pivot_indicator: Databricks-managed MCP executes the UC function itself, so we don't control
    the serialization of its RETURNS TABLE result — and it is NOT the same shape enrich_indicator's
    OWN server uses (app/mcp_server.py's unitycatalog-ai client returns CSV text; that's a different,
    unrelated code path). Verified live: managed MCP's tools/call result is a JSON OBJECT
    {"columns": [...], "rows": [[...], ...], "is_truncated": bool} — a column-oriented table, not a
    list of row-dicts. Zip columns onto each row; tolerate a plain JSON list too, in case the shape
    ever varies for a different function's return type. Raise loudly on anything else — a silent
    fallback here would hand the LLM wrong-shaped evidence instead of a visible error."""
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "columns" in parsed and "rows" in parsed:
        return [dict(zip(parsed["columns"], row)) for row in parsed["rows"]]
    if isinstance(parsed, list):
        return parsed
    raise ValueError(f"unrecognized managed-MCP result shape: {parsed!r}")


_RESULT_PARSERS = {"enrich_indicator": _parse_json_rows, "pivot_indicator": _parse_managed_rows}


def make_mcp_tool_fn(clients):
    """clients: {tool_name: (DatabricksMCPClient, mcp_call_name)} — see make_mcp_clients, which builds
    this shape. `mcp_call_name` is the name to pass to call_tool(), which is NOT always `tool_name`
    (see managed_mcp_tool_name). Returns tool_fn(name, value) -> list[dict]."""
    def tool_fn(name, value):
        if name not in clients:
            return [{"error": f"no MCP client configured for tool {name}"}]
        client, mcp_call_name = clients[name]
        # Plain blocking call — sequential tool use is expected. DatabricksMCPClient.call_tool() uses
        # asyncio.run() internally, which needs a thread with no already-running event loop. Both call
        # sites satisfy that WITHOUT any thread juggling here: the app runs each investigation on its own
        # background thread (app/investigations.py _launch_inprocess — no loop, and off the FastAPI event
        # loop), and the investigate job applies nest_asyncio at startup (src/investigate.py) so a blocking
        # call works under the notebook's ambient loop. Keeping this call dead-simple is deliberate.
        result = client.call_tool(mcp_call_name, {UC_FUNCTION_PARAM[name]: value})
        return _RESULT_PARSERS[name](_extract_text(result))
    return tool_fn


def make_routed_tool_fn(sql_tool_fn, mcp_tool_fn, mcp_tool_names=frozenset(UC_FUNCTION_PARAM)):
    """The tool_fn(name, value) the Investigator is built with: enrich_indicator/pivot_indicator go to
    mcp_tool_fn (see make_mcp_tool_fn); blast_radius/get_account_risk/get_account_actions go to
    sql_tool_fn (lib/tools.make_tool_fn), unchanged."""
    def tool_fn(name, value):
        if name in mcp_tool_names:
            return mcp_tool_fn(name, value)
        return sql_tool_fn(name, value)
    return tool_fn


# Fixed name for the UC MCP Service that fronts the custom enrich_indicator server — created by
# tests/harness/mcp.py. Both this module and that one hardcode the same name rather than
# passing it around, since a single AIA deployment only ever provisions one.
CUSTOM_MCP_SERVICE_NAME = "aia_enrich_indicator"


def managed_mcp_url(host, catalog, schema, tool_name):
    """A Databricks-managed MCP server URL for one UC function — no hosting, no connection, governed
    natively by the caller's own EXECUTE grant on the function."""
    return f"{host.rstrip('/')}/api/2.0/mcp/functions/{catalog}/{schema}/{tool_name}"


def managed_mcp_tool_name(catalog, schema, tool_name):
    """Databricks managed MCP always names a UC-function tool `catalog__schema__function` (double
    underscore) in tools/list and tools/call — verified live: even when the server URL is scoped to a
    single function (managed_mcp_url), list_tools() still returns this fully-qualified mangled name,
    not the bare function name. Calling call_tool() with the bare name 400s ("Function name [x] is
    malformed. Expected exactly 2 '__' separators.")."""
    return f"{catalog}__{schema}__{tool_name}"


def custom_mcp_url(host, catalog, schema, service_name=CUSTOM_MCP_SERVICE_NAME):
    """The AI-Gateway URL for a registered (connection-backed) MCP Service — fronts our own
    app/mcp_server.py through the UC HTTP Connection (tests/harness/mcp.py). Confirmed
    template (docs.databricks.com/aws/en/ai-gateway/register-mcp-service):
    https://<host>/ai-gateway/mcp-services/{catalog}.{schema}.{service_name} — note this is the AI
    Gateway path, distinct from managed_mcp_url's /api/2.0/mcp/functions/... path above. Unlike managed
    MCP, a connection-backed MCP Service calls its tool by the bare name our own server registered it
    under (verified live) — no catalog__schema__ mangling here.
    """
    return f"{host.rstrip('/')}/ai-gateway/mcp-services/{catalog}.{schema}.{service_name}"


def make_mcp_clients(workspace_client, catalog, schema):
    """Build {tool_name: (DatabricksMCPClient, mcp_call_name)} for each of the 2 MCP-routed tools, both
    authenticating as whatever identity `workspace_client` already carries (the app's own SP in
    in_process, the job SP in job/job_warehouse) — the door-key model: this is just an authenticated
    call through the ambient WorkspaceClient, same as every other tool call."""
    from databricks_mcp import DatabricksMCPClient
    host = workspace_client.config.host
    return {
        "pivot_indicator": (
            DatabricksMCPClient(server_url=managed_mcp_url(host, catalog, schema, "pivot_indicator"),
                                workspace_client=workspace_client),
            managed_mcp_tool_name(catalog, schema, "pivot_indicator")),
        "enrich_indicator": (
            DatabricksMCPClient(server_url=custom_mcp_url(host, catalog, schema),
                                workspace_client=workspace_client),
            "enrich_indicator"),
    }
