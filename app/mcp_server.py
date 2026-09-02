"""app/mcp_server.py — the custom MCP server for enrich_indicator.

Exposes ONE tool, enrich_indicator, executing the SAME UC function every other tool call runs
(see demo/src/seed_demo_data.py) — just reached over MCP instead of direct SQL, and run on
Databricks-managed serverless compute (unitycatalog-ai's DatabricksFunctionClient, Spark Connect
serverless) instead of a warehouse — no warehouse dependency in any AIA_AGENT_MODE.

Mounted into the FastAPI app (app/main.py) at /mcp/ and fronted, in production, by a Unity Catalog HTTP
Connection + MCP Service (tests/harness/mcp.py) — that's the governance boundary; this
module has no idea whether it's being called through the connection or hit directly. That's the
"door-key" model (see README): whoever reaches this code runs the UC function as the APP'S OWN ambient
identity (DatabricksFunctionClient authenticates the same way every other line of this app does), not
as whichever SP the connection's credential names — the connection controls WHO can reach this code,
not WHOSE identity runs it once they're in.
"""
import csv
import io
import json

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.config import Config

# A separate, cheap Config.from_env() (env-var parse only, no WorkspaceClient/Lakebase setup) rather
# than importing CATALOG/SCHEMA from app.investigations — this module only needs the catalog/schema
# names, and staying decoupled from investigations' heavier module-level setup keeps it independently
# importable/testable (see tests/unit/test_mcp_server.py).
_cfg = Config.from_env()
CATALOG = _cfg.catalog
SCHEMA = _cfg.schema

mcp_server = FastMCP(
    "aia-enrich-indicator",
    # streamable_http_path="/": FastMCP's internal route defaults to "/mcp", which would double up with
    # the "/mcp" prefix app/main.py mounts this app under (-> "/mcp/mcp"). Setting it to "/" here makes
    # the mount path the ONLY path component, so the client-facing endpoint is exactly /mcp/ (Starlette's
    # Mount 307-redirects the trailing-slash-free /mcp to /mcp/ — scripts/setup.py's custom_mcp_app_url
    # already includes the trailing slash when it registers this URL with the UC connection, so the
    # connection's proxied calls never hit that redirect; verified empirically — see PR notes).
    streamable_http_path="/",
    # The mcp SDK's Streamable HTTP transport defaults to DNS-rebinding protection that rejects every
    # Host header unless explicitly allow-listed (verified empirically: default allowed_hosts=[] rejects
    # ALL hosts, not just unknown ones). That protection targets browser clients being tricked into
    # calling an attacker-controlled hostname that resolves to localhost/an internal service — not a
    # relevant threat here, since every caller reaches this app through Databricks' own OAuth-enforcing
    # edge (Apps auth, or the UC connection's OAuth M2M via the AI Gateway proxy) — a strictly stronger
    # boundary already required before any request reaches this code. Disabled rather than hardcoding an
    # allowed_hosts entry, since the app's real hostname isn't known at code-authoring time.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _rows(result):
    """A unitycatalog-ai FunctionExecutionResult -> list[dict]. enrich_indicator RETURNS TABLE, which
    execute_function serializes as result.value = a CSV string (result.format == "CSV") — confirmed
    against unitycatalog-ai's own process_retriever_output, which reads table results the same way
    (pandas.read_csv(StringIO(result.value)) for format == "CSV")."""
    if result.error:
        raise RuntimeError(result.error)
    if result.format != "CSV":
        return [{"value": result.value}]
    return list(csv.DictReader(io.StringIO(result.value)))


def _execute_enrich_indicator(indicator: str) -> str:
    """Pulled out of the @mcp_server.tool()-decorated function below so it's directly unit-testable
    (tests/unit/test_mcp_server.py) regardless of how FastMCP's decorator wraps its target."""
    from unitycatalog.ai.core.databricks import DatabricksFunctionClient
    client = DatabricksFunctionClient(execution_mode="serverless")
    result = client.execute_function(f"{CATALOG}.{SCHEMA}.enrich_indicator", {"ind": indicator})
    return json.dumps(_rows(result))


@mcp_server.tool()
def enrich_indicator(indicator: str) -> str:
    """URLhaus verdict for a URL/IP/domain/hash (query_status ok=known-bad, threat, url_status, tags, family)."""
    return _execute_enrich_indicator(indicator)
