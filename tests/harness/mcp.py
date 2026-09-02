"""Showcase: reaching an EXTERNAL MCP server from Databricks via a UC HTTP Connection + MCP Service.

This is a TEACHING layer, not part of AIA's core architecture. The `enrich_indicator` MCP server is hosted
INSIDE the app (app/mcp_server.py, mounted at /mcp/) purely for demo convenience — so the demo needn't stand
up a separate server. Because it lives in the app, a Databricks-native caller would reach it by calling the
app's /mcp/ endpoint DIRECTLY, governed by CAN_USE on the app — no connection, no MCP service, no embedded
credential. We deliberately route through a UC HTTP Connection + MCP Service instead to demonstrate the
first-class, governed path you WOULD use for a genuinely external MCP server. See the README
("Showcase: an external MCP server, via a UC connection").

Ownership: this is ADMIN / catalog-schema-owner work. Creating a connection is metastore-level
(CREATE CONNECTION); the MCP Service is created in the AIA-owned schema; and the connection embeds an OAuth
M2M credential minted on a dedicated **external-MCP client SP** (below). All of it runs POST-deploy — the
server is app-hosted, so the connection's target URL only exists after deploy.

Two identities, deliberately separated (this is why a real external server is cleaner to reason about than
one embedded in the app):
  * the **external-MCP client SP** (`aia-external-mcp-client`) — a dedicated SP the ADMIN creates, so the
    admin can mint its OAuth M2M secret. Its credential is embedded in the connection and it holds CAN_USE on
    the app: it's the client identity the connection authenticates AS to reach the (stand-in external) server.
    It is NOT AIA's investigation-time identity — the server still runs the UC function as the app's OWN
    ambient identity once a call is let in.
  * the **agent caller** (app SP for in_process, job SP for job modes) — the identity that actually invokes
    the MCP Service at investigation time, so it gets EXECUTE on the service. A plain grant the admin makes on
    the service it owns; no secret minting on the app SP (which the admin can't manage anyway — the Apps
    service owns it).

VERIFIED LIVE against a real serverless workspace: the connection `options` shape, the MCP-service REST body
(`ensure_mcp_service`), the `mcp-service` permission path segment (`grant_sp_execute_on_mcp_service`), and the
CAN_USE app grant all succeed.
"""
from __future__ import annotations

from urllib.parse import urlparse

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import AlreadyExists
from databricks.sdk.service import apps
from databricks.sdk.service.catalog import ConnectionType

from tests.harness import identities

CLIENT_SP_DISPLAY = "aia-external-mcp-client"   # the dedicated client SP the connection authenticates as


def _do(w: WorkspaceClient, method: str, path: str, body: dict | None = None, query: dict | None = None):
    return w.api_client.do(method, path, body=body, query=query)


def resolve_agent_caller_sp(w: WorkspaceClient, *, agent_mode: str, app_name: str, job_sp_client_id: str) -> str:
    """The client_id of AIA's investigation-time caller — the identity that invokes the MCP Service and so
    needs EXECUTE on it: the app SP for in_process, the job SP for job/job_warehouse. This is a plain read +
    config lookup (no minting), so the admin can resolve and grant it regardless of who owns the app SP."""
    if agent_mode == "in_process":
        client_id = w.apps.get(name=app_name).service_principal_client_id
        if not client_id:
            raise RuntimeError(f"app {app_name!r} has no service principal yet — did deploy run?")
        return client_id
    if not job_sp_client_id or job_sp_client_id == "none":
        raise RuntimeError("job_sp is required for agent_mode=job/job_warehouse (admin pre-allocates it).")
    return job_sp_client_id


def ensure_client_sp(w: WorkspaceClient) -> tuple[str, str]:
    """Create-or-find the dedicated external-MCP client SP and mint it a FRESH OAuth M2M secret. The ADMIN
    owns it (it created it), so minting works — unlike the app SP, whose secrets only the app owner can mint.
    Returns (client_id, secret). The secret is shown only here; the caller embeds it in the connection now."""
    sp = identities.find_or_create_sp(w, CLIENT_SP_DISPLAY)
    identities.add_entitlements(w, sp["id"], ["workspace-access"])   # best-effort; lets it use the app
    secret = identities.mint_oauth_secret(w, sp["id"])               # deletes stale first (per-SP cap of 5)
    return sp["applicationId"], secret


def ensure_connection(w: WorkspaceClient, name: str, *, mcp_app_url: str, sp_client_id: str,
                      sp_secret: str, token_endpoint: str) -> tuple[str, str]:
    """Create-or-update the ONE UC HTTP connection fronting the (stand-in external) MCP server, embedding the
    external-MCP client SP's freshly-minted OAuth M2M credential. Fixed name -> idempotent: get, then update
    if present, else create."""
    parsed = urlparse(mcp_app_url)
    options = {
        # "host" must be the FULL url including scheme (e.g. "https://host"), not a bare hostname —
        # verified live: a bare hostname fails with "Missing cloud file system scheme".
        "host": f"{parsed.scheme}://{parsed.hostname}",
        "port": str(parsed.port or 443),
        "base_path": parsed.path or "/",
        "client_id": sp_client_id,
        "client_secret": sp_secret,
        "oauth_scope": "all-apis",
        "token_endpoint": token_endpoint,
    }
    try:
        w.connections.get(name)
        _do(w, "PATCH", f"/api/2.1/unity-catalog/connections/{name}",
            body={"options": options}, query={"update_mask": "options"})
        return name, "updated"
    except Exception:  # noqa: BLE001 — get raises when absent; fall through to create
        w.connections.create(name=name, connection_type=ConnectionType.HTTP, options=options,
                             comment="AIA external-MCP showcase (enrich_indicator) — see tests/harness/mcp.py")
        return name, "created"


def ensure_mcp_service(w: WorkspaceClient, catalog: str, schema: str, service_name: str,
                       connection_name: str) -> tuple[str, str]:
    """Create-or-update the MCP Service exposing the server's one tool, referencing the connection. No
    confirmed typed SDK method for MCP Services yet — raw REST, mirroring databricks_ops/lakebase.py's
    precedent for SDK-uncovered UC surfaces. Idempotent via create-then-fallback (the mcp-services
    GET-by-name form isn't reliable across versions — verified live): POST and, on AlreadyExists, PATCH."""
    parent = f"schemas/{catalog}.{schema}"
    full_name = f"{catalog}.{schema}.{service_name}"
    body = {"comment": "AIA external-MCP showcase (enrich_indicator)",
            "config": {"source_connection": {"name": f"connections/{connection_name}"},
                       "include_tool_selectors": []}}   # empty selector list = expose every tool the
                                                          # backend advertises (our server has only one)
    try:
        _do(w, "POST", "/api/2.1/unity-catalog/mcp-services", body=body,
            query={"parent": parent, "mcp_service_id": service_name})
        return full_name, "created"
    except AlreadyExists:
        try:
            _do(w, "PATCH", f"/api/2.1/unity-catalog/mcp-services/{full_name}", body=body)
            return full_name, "updated"
        except Exception:  # noqa: BLE001 — already present + referencing the fixed-name connection
            return full_name, "exists"


def grant_sp_can_use_app(w: WorkspaceClient, app_name: str, sp_app_id: str) -> None:
    """Let the external-MCP client SP (embedded in the connection) past the app's own OAuth-M2M edge.
    update_permissions is additive — it must not reset the app's existing ACL."""
    w.apps.update_permissions(
        app_name=app_name,
        access_control_list=[apps.AppAccessControlRequest(
            service_principal_name=sp_app_id, permission_level=apps.AppPermissionLevel.CAN_USE)])


def grant_sp_execute_on_mcp_service(w: WorkspaceClient, catalog: str, schema: str, service_name: str,
                                    sp_app_id: str) -> None:
    """Let the agent caller (app/job SP) invoke the MCP Service. Raw REST — no confirmed typed SDK grant
    method for MCP Services yet. NEVER grant USE CONNECTION instead — the docs warn it lets the grantee
    bypass tool selection and call the backend directly."""
    full_name = f"{catalog}.{schema}.{service_name}"
    _do(w, "PATCH", f"/api/2.1/unity-catalog/permissions/mcp-service/{full_name}",
        body={"changes": [{"principal": sp_app_id, "add": ["EXECUTE"]}]})


def provision(w: WorkspaceClient, *, agent_mode: str, app_name: str, job_sp_client_id: str,
              catalog: str, schema: str, custom_mcp_app_url: str,
              connection_name: str = "aia_mcp_connection",
              service_name: str = "aia_enrich_indicator") -> dict:
    """The full ADMIN recipe, uniform across modes: create the external-MCP client SP + mint its secret,
    embed it in the connection, create the MCP Service, grant the client SP CAN_USE on the app, and grant the
    agent caller (app/job SP) EXECUTE on the service. `w` is the ADMIN client throughout — no app-SP secret
    minting, so no deployer handoff. Call AFTER deploy (needs the app's URL + SP)."""
    client_sp, secret = ensure_client_sp(w)
    token_endpoint = f"{w.config.host.rstrip('/')}/oidc/v1/token"
    conn_name, conn_action = ensure_connection(
        w, connection_name, mcp_app_url=custom_mcp_app_url, sp_client_id=client_sp,
        sp_secret=secret, token_endpoint=token_endpoint)
    svc_name, svc_action = ensure_mcp_service(w, catalog, schema, service_name, conn_name)
    grant_sp_can_use_app(w, app_name, client_sp)                        # connection -> app (door)
    caller_sp = resolve_agent_caller_sp(w, agent_mode=agent_mode, app_name=app_name,
                                        job_sp_client_id=job_sp_client_id)
    grant_sp_execute_on_mcp_service(w, catalog, schema, service_name, caller_sp)   # agent -> service
    return {"connection": conn_name, "connection_action": conn_action,
            "mcp_service": svc_name, "mcp_service_action": svc_action,
            "client_sp": client_sp, "agent_caller_sp": caller_sp}
