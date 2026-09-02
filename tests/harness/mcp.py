"""Admin-side provisioning for the custom-MCP-server tool-delivery variation (enrich_indicator).

This lives in the TEST HARNESS, alongside tests/harness/identities.py, for the same reason: it is
ADMIN / catalog-schema-owner work, not the regular deployer's. Creating a UC HTTP Connection needs the
metastore-level CREATE CONNECTION; minting the caller SP's OAuth M2M secret to embed in it needs
servicePrincipal.manager on that SP (the deployer only holds servicePrincipal.user, for run_as); and the
MCP Service is created in the AIA-owned evidence schema. `databricks_ops` deliberately excludes all of
this (it stays deployer/owner-only). A real AIA admin does these steps out of band; this module is the
scripted reference, invoked from tests/e2e/admin_postdeploy.py.

WHY POST-DEPLOY, EVERY MODE: the custom MCP server is hosted inside the app (app/mcp_server.py, mounted
at /mcp/), so the connection's host is the app's URL — assigned at deploy, discovered via
`w.apps.get(...).url`, never known ahead of time. So the connection + service + grants land post-deploy
in ALL modes (there's nothing to pre-create pre-deploy, and the embedded secret is ephemeral — usable
only at mint time — so it can't be minted early and stashed). The only per-mode difference is WHICH SP is
embedded/granted: the app SP for agent_mode=in_process, the job SP otherwise. (For a REAL external MCP
server — which the app-hosted one only simulates — the host is known ahead of time, so job-mode
provisioning could move pre-deploy; the app-hosting is the sole reason it's post-deploy here.)

The embedded credential is a "door key" (see README "Door-key vs native per-caller"): the connection
controls WHO may reach the server, but once past it the server runs the UC function as its own ambient
identity. So the embedded SP needs CAN_USE on the app (past the app's OAuth edge) and EXECUTE on the MCP
Service — never USE CONNECTION, which would bypass tool selection and hit the raw backend directly.

VERIFY LIVE before relying on this in a real deploy (flagged, not guessed): the exact `options` keys
ensure_connection sends for an HTTP/OAuth-M2M connection, the `config` body shape ensure_mcp_service
sends, and the `mcp-service` path segment grant_sp_execute_on_mcp_service uses — all confirmed at the
shape level against docs.databricks.com/aws/en/ai-gateway/register-mcp-service, but not yet exercised
end-to-end. Run provision() once against a real serverless workspace and fix any field-name mismatch the
API rejects; expected to be a small, mechanical fix, not a design change.
"""
from __future__ import annotations

from urllib.parse import urlparse

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import AlreadyExists
from databricks.sdk.service import apps
from databricks.sdk.service.catalog import ConnectionType

from tests.harness import identities


def _do(w: WorkspaceClient, method: str, path: str, body: dict | None = None, query: dict | None = None):
    return w.api_client.do(method, path, body=body, query=query)


def resolve_caller_sp(w: WorkspaceClient, *, agent_mode: str, app_name: str, job_sp_client_id: str) -> str:
    """The client_id of whichever SP is THIS deployed stack's investigation-time caller: the app SP for
    in_process, the job SP for job/job_warehouse — the same split AGENT_MODE drives everywhere else."""
    if agent_mode == "in_process":
        client_id = w.apps.get(name=app_name).service_principal_client_id
        if not client_id:
            raise RuntimeError(f"app {app_name!r} has no service principal yet — did deploy run?")
        return client_id
    if not job_sp_client_id or job_sp_client_id == "none":
        raise RuntimeError("job_sp is required for agent_mode=job/job_warehouse (admin pre-allocates it).")
    return job_sp_client_id


def mint_caller_secret(w: WorkspaceClient, sp_client_id: str) -> str:
    """Mint a FRESH OAuth M2M secret for the caller SP to embed in the connection (there's no API to
    retrieve a previously-minted one, so re-provisioning always mints new). Reuses identities.mint_oauth_secret,
    which deletes existing secrets first to stay under the per-SP cap of 5. `sp_client_id` is the applicationId;
    the secrets proxy needs the numeric id, so resolve it via identities.find_sp."""
    sp = identities.find_sp(w, application_id=sp_client_id)
    if not sp:
        raise RuntimeError(f"service principal {sp_client_id!r} not found in this workspace")
    return identities.mint_oauth_secret(w, sp["id"])


def ensure_connection(w: WorkspaceClient, name: str, *, mcp_app_url: str, sp_client_id: str,
                      sp_secret: str, token_endpoint: str) -> tuple[str, str]:
    """Create-or-update the ONE UC HTTP connection AIA uses to front the custom MCP server, embedding the
    given SP's freshly-minted OAuth M2M credential. Fixed name -> idempotent across deploys: get, then
    update if present, else create."""
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
                             comment="AIA custom MCP server (enrich_indicator) — see tests/harness/mcp.py")
        return name, "created"


def ensure_mcp_service(w: WorkspaceClient, catalog: str, schema: str, service_name: str,
                       connection_name: str) -> tuple[str, str]:
    """Create-or-update the MCP Service exposing the custom server's one tool, referencing the connection.
    No confirmed typed SDK method for MCP Services yet — raw REST, mirroring databricks_ops/lakebase.py's
    precedent for SDK-uncovered UC surfaces. Idempotent via create-then-fallback (the mcp-services
    GET-by-name form isn't reliable across versions — verified live): POST and, on AlreadyExists, PATCH to
    keep the config current. The service references the connection by NAME, so rotating the connection's
    embedded credential needs no service change."""
    parent = f"schemas/{catalog}.{schema}"
    full_name = f"{catalog}.{schema}.{service_name}"
    body = {"comment": "AIA custom MCP server (enrich_indicator)",
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
    """Let the SP embedded in the UC connection past the app's own OAuth-M2M edge ("door key").
    update_permissions is additive — it must not reset the app's existing ACL."""
    w.apps.update_permissions(
        app_name=app_name,
        access_control_list=[apps.AppAccessControlRequest(
            service_principal_name=sp_app_id, permission_level=apps.AppPermissionLevel.CAN_USE)])


def grant_sp_execute_on_mcp_service(w: WorkspaceClient, catalog: str, schema: str, service_name: str,
                                    sp_app_id: str) -> None:
    """Let the SP invoke the enrich_indicator MCP Service. Raw REST — no confirmed typed SDK grant method
    for MCP Services yet. NEVER grant USE CONNECTION instead — the docs warn it lets the grantee bypass
    tool selection and call the backend directly. VERIFY LIVE: the "mcp-service" path segment (body shape
    {"changes": [{"principal": ..., "add": ["EXECUTE"]}]} is confirmed by the docs)."""
    full_name = f"{catalog}.{schema}.{service_name}"
    _do(w, "PATCH", f"/api/2.1/unity-catalog/permissions/mcp-service/{full_name}",
        body={"changes": [{"principal": sp_app_id, "add": ["EXECUTE"]}]})


def provision(w: WorkspaceClient, *, agent_mode: str, app_name: str, job_sp_client_id: str,
              catalog: str, schema: str, custom_mcp_app_url: str,
              connection_name: str = "aia_mcp_connection",
              service_name: str = "aia_enrich_indicator") -> dict:
    """The full ADMIN recipe: resolve the caller SP, mint it a fresh OAuth secret, create/update the
    connection + MCP Service, and grant that SP CAN_USE on the app + EXECUTE on the service. Idempotent by
    fixed naming; the secret mint is always fresh. Call from admin_postdeploy AFTER deploy (needs the app
    SP + URL)."""
    sp_client_id = resolve_caller_sp(w, agent_mode=agent_mode, app_name=app_name,
                                     job_sp_client_id=job_sp_client_id)
    secret = mint_caller_secret(w, sp_client_id)
    token_endpoint = f"{w.config.host.rstrip('/')}/oidc/v1/token"
    conn_name, conn_action = ensure_connection(
        w, connection_name, mcp_app_url=custom_mcp_app_url, sp_client_id=sp_client_id,
        sp_secret=secret, token_endpoint=token_endpoint)
    svc_name, svc_action = ensure_mcp_service(w, catalog, schema, service_name, conn_name)
    grant_sp_can_use_app(w, app_name, sp_client_id)
    grant_sp_execute_on_mcp_service(w, catalog, schema, service_name, sp_client_id)
    return {"connection": conn_name, "connection_action": conn_action,
            "mcp_service": svc_name, "mcp_service_action": svc_action, "caller_sp": sp_client_id}
