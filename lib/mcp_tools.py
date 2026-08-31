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
import csv
import io
import json

# The UC functions' REAL SQL parameter name (see demo/src/seed_demo_data.py — both functions are
# declared `(ind STRING)`), NOT lib/investigator.TOOL_ARG's LLM-facing arg name ("indicator"). SQL
# calls today are positional (`fn('value')`) so TOOL_ARG's name never mattered on that path; MCP calls
# are NAMED, so this is the one that must be used to build `arguments`. Do not "simplify" this back to
# TOOL_ARG — that would silently break both MCP tool calls.
UC_FUNCTION_PARAM = {"enrich_indicator": "ind", "pivot_indicator": "ind"}


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
    the serialization of its RETURNS TABLE result — try JSON first, fall back to CSV."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return list(csv.DictReader(io.StringIO(text)))


_RESULT_PARSERS = {"enrich_indicator": _parse_json_rows, "pivot_indicator": _parse_managed_rows}


def make_mcp_tool_fn(clients):
    """clients: {tool_name: DatabricksMCPClient}. Returns tool_fn(name, value) -> list[dict] that
    calls the matching MCP tool with its UC function's real parameter name and parses the result."""
    def tool_fn(name, value):
        if name not in clients:
            return [{"error": f"no MCP client configured for tool {name}"}]
        result = clients[name].call_tool(name, {UC_FUNCTION_PARAM[name]: value})
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
# databricks_ops/mcp_connection.py. Both this module and that one hardcode the same name rather than
# passing it around, since a single AIA deployment only ever provisions one.
CUSTOM_MCP_SERVICE_NAME = "aia_enrich_indicator"


def managed_mcp_url(host, catalog, schema, tool_name):
    """A Databricks-managed MCP server URL for one UC function — no hosting, no connection, governed
    natively by the caller's own EXECUTE grant on the function."""
    return f"{host.rstrip('/')}/api/2.0/mcp/functions/{catalog}/{schema}/{tool_name}"


def custom_mcp_url(host, catalog, schema, service_name=CUSTOM_MCP_SERVICE_NAME):
    """The AI-Gateway URL for a registered (connection-backed) MCP Service — fronts our own
    app/mcp_server.py through the UC HTTP Connection (databricks_ops/mcp_connection.py). Confirmed
    template (docs.databricks.com/aws/en/ai-gateway/register-mcp-service):
    https://<host>/ai-gateway/mcp-services/{catalog}.{schema}.{service_name} — note this is the AI
    Gateway path, distinct from managed_mcp_url's /api/2.0/mcp/functions/... path above."""
    return f"{host.rstrip('/')}/ai-gateway/mcp-services/{catalog}.{schema}.{service_name}"


def make_mcp_clients(workspace_client, catalog, schema):
    """Build the DatabricksMCPClient for each of the 2 MCP-routed tools, both authenticating as
    whatever identity `workspace_client` already carries (the app's own SP in in_process, the job SP
    in job/job_warehouse) — the door-key model: this is just an authenticated call through the ambient
    WorkspaceClient, same as every other tool call."""
    from databricks_mcp import DatabricksMCPClient
    host = workspace_client.config.host
    return {
        "pivot_indicator": DatabricksMCPClient(
            server_url=managed_mcp_url(host, catalog, schema, "pivot_indicator"),
            workspace_client=workspace_client),
        "enrich_indicator": DatabricksMCPClient(
            server_url=custom_mcp_url(host, catalog, schema),
            workspace_client=workspace_client),
    }
