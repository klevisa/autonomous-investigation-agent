"""Provision the UC HTTP Connection + MCP Service that front the custom enrich_indicator MCP server
(app/mcp_server.py) — one of the 3 tool-delivery variations this branch showcases, alongside managed
MCP (pivot_indicator, needs NO provisioning — Databricks executes it natively) and direct UC SQL (the
other 3 tools, unchanged).

ONE connection, re-provisioned per deploy (see README "Door-key vs native per-caller"): its embedded
OAuth M2M credential is whichever SP is THIS deployed stack's investigation-time caller — the APP SP
for agent_mode=in_process, the JOB SP otherwise (job / job_warehouse) — resolved from the SAME
agent_mode bundle variable that already picks where investigations run (app/investigations.py,
src/investigate.py). That SP is a "door key": it needs CAN_USE on the app (to get past the app's
OAuth-M2M edge — the AI Gateway proxy authenticates to the app AS this SP, since Databricks Apps has
no mechanism to forward an M2M caller's own identity into app code) and EXECUTE on the MCP Service (to
invoke the tool) — never USE CONNECTION, which would let it bypass tool selection and call the raw
backend directly (see databricks_ops/grants.py).

NOTE on the SDK: MCP Services are new enough that there's no confirmed typed SDK method as of this
writing — like databricks_ops/lakebase.py's Lakebase `postgres` endpoints, we go through the SDK's own
HTTP client (`w.api_client.do`), the same REST surface the `databricks ai-gateway create-mcp-service`
CLI verb wraps. Connections DO have a typed method (`w.connections`).

VERIFY LIVE before relying on this in a real deploy (flagged, not guessed — see the plan this module
was built from): the exact `options` keys `ensure_connection` sends for an HTTP/OAuth-M2M connection,
and the exact `config` body shape `ensure_mcp_service` sends — both confirmed at the shape level
against docs.databricks.com/aws/en/ai-gateway/register-mcp-service, but not exercised end-to-end yet.
Run `provision()` once against fevm-aws-serverless-stable-3 and fix any field-name mismatch the API
rejects; that's expected to be a small, mechanical fix, not a design change.
"""
from __future__ import annotations

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.catalog import ConnectionType

MAX_SP_SECRETS = 5   # per-SP cap; provisioning always mints a fresh secret, so old ones must be cleaned
                      # up or repeated `setup.py` runs eventually fail to mint a new one.


def _do(w: WorkspaceClient, method: str, path: str, body: dict | None = None, query: dict | None = None):
    return w.api_client.do(method, path, body=body, query=query)


def resolve_caller_sp(w: WorkspaceClient, *, agent_mode: str, app_name: str, job_sp_client_id: str) -> str:
    """The client_id of whichever SP is THIS deployed stack's investigation-time caller: the app SP for
    in_process, the job SP for job/job_warehouse — the same split AGENT_MODE already drives everywhere
    else. Raises loudly if the mode needs a job SP that wasn't configured (mirrors setup.py's own
    job_sp check for the existing job/job_warehouse wiring)."""
    if agent_mode == "in_process":
        client_id = w.apps.get(name=app_name).service_principal_client_id
        if not client_id:
            raise RuntimeError(f"app {app_name!r} has no service principal yet — did deploy.py run?")
        return client_id
    if not job_sp_client_id or job_sp_client_id == "none":
        raise RuntimeError("job_sp is required for agent_mode=job/job_warehouse (admin pre-allocates it).")
    return job_sp_client_id


def _sp_numeric_id(w: WorkspaceClient, client_id: str) -> str:
    """The WORKSPACE-scoped numeric id service_principal_secrets_proxy needs, from a client_id. This is
    a plain typed `w.service_principals` lookup — workspace-local, unlike the account-SCIM lookups in
    databricks_ops/groups.py, which exist there specifically because group membership is account-scoped."""
    matches = list(w.service_principals.list(filter=f'applicationId eq "{client_id}"'))
    if not matches:
        raise RuntimeError(f"service principal {client_id!r} not found in this workspace")
    return matches[0].id


def ensure_oauth_secret(w: WorkspaceClient, sp_client_id: str) -> str:
    """Mint a FRESH OAuth M2M client secret for the SP — there is no API to retrieve a previously-minted
    one, so re-provisioning always mints new — and delete old ones first so repeated `setup.py` runs
    never hit the per-SP secret cap. Returns the new secret value (shown ONLY here; the caller must use
    it immediately to (re)configure the connection)."""
    sp_id = _sp_numeric_id(w, sp_client_id)
    existing = list(w.service_principal_secrets_proxy.list(sp_id))
    while len(existing) >= MAX_SP_SECRETS:
        stale = existing.pop(0)   # oldest first, per API list order
        w.service_principal_secrets_proxy.delete(sp_id, stale.id)
    return w.service_principal_secrets_proxy.create(sp_id).secret


def ensure_connection(w: WorkspaceClient, name: str, *, mcp_app_url: str, sp_client_id: str,
                      sp_secret: str, token_endpoint: str) -> tuple[str, str]:
    """Create-or-update the ONE UC HTTP connection AIA uses to front the custom MCP server, embedding
    the given SP's freshly-minted OAuth M2M credential. Fixed name -> idempotent across deploys: get,
    then update if present, else create."""
    from urllib.parse import urlparse
    parsed = urlparse(mcp_app_url)
    options = {
        # "host" must be the FULL url including scheme (e.g. "https://host"), not a bare hostname —
        # verified live: a bare hostname fails with "Missing cloud file system scheme"; every working
        # HTTP/MCP connection in the account carries the scheme (confirmed by inspecting several).
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
    except Exception:
        w.connections.create(name=name, connection_type=ConnectionType.HTTP, options=options,
                             comment="AIA custom MCP server (enrich_indicator) — see databricks_ops/mcp_connection.py")
        return name, "created"


def ensure_mcp_service(w: WorkspaceClient, catalog: str, schema: str, service_name: str,
                       connection_name: str) -> tuple[str, str]:
    """Create-or-update the MCP Service exposing the custom server's one tool, referencing the
    connection. No confirmed typed SDK method for MCP Services yet — raw REST, mirroring
    databricks_ops/lakebase.py's precedent for SDK-uncovered UC surfaces."""
    parent = f"schemas/{catalog}.{schema}"
    full_name = f"{catalog}.{schema}.{service_name}"
    body = {"comment": "AIA custom MCP server (enrich_indicator)",
            "config": {"source_connection": {"name": f"connections/{connection_name}"},
                       "include_tool_selectors": []}}   # empty selector list = expose every tool the
                                                          # backend advertises (our server has only one)
    try:
        _do(w, "GET", f"/api/2.1/unity-catalog/mcp-services/{full_name}")
        _do(w, "PATCH", f"/api/2.1/unity-catalog/mcp-services/{full_name}", body=body)
        return full_name, "updated"
    except Exception:
        _do(w, "POST", "/api/2.1/unity-catalog/mcp-services", body=body,
            query={"parent": parent, "mcp_service_id": service_name})
        return full_name, "created"


def provision(w: WorkspaceClient, *, agent_mode: str, app_name: str, job_sp_client_id: str,
             catalog: str, schema: str, custom_mcp_app_url: str,
             connection_name: str = "aia_mcp_connection",
             service_name: str = "aia_enrich_indicator") -> dict:
    """The full recipe: resolve the caller SP, mint it a fresh OAuth secret, create/update the
    connection + MCP Service, and grant that SP CAN_USE on the app + EXECUTE on the MCP Service.
    Idempotent by fixed naming (connection/service); the secret mint is not (always fresh — see
    ensure_oauth_secret). Call from scripts/setup.py AFTER the app is deployed (needs its SP + URL)."""
    from databricks_ops import grants
    sp_client_id = resolve_caller_sp(w, agent_mode=agent_mode, app_name=app_name,
                                     job_sp_client_id=job_sp_client_id)
    secret = ensure_oauth_secret(w, sp_client_id)
    token_endpoint = f"{w.config.host.rstrip('/')}/oidc/v1/token"
    conn_name, conn_action = ensure_connection(
        w, connection_name, mcp_app_url=custom_mcp_app_url, sp_client_id=sp_client_id,
        sp_secret=secret, token_endpoint=token_endpoint)
    svc_name, svc_action = ensure_mcp_service(w, catalog, schema, service_name, conn_name)
    grants.grant_sp_can_use_app(w, app_name, sp_client_id)
    grants.grant_sp_execute_on_mcp_service(w, catalog, schema, service_name, sp_client_id)
    return {"connection": conn_name, "connection_action": conn_action,
            "mcp_service": svc_name, "mcp_service_action": svc_action, "caller_sp": sp_client_id}
