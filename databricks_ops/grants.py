"""Workspace grants the REGULAR USER wires post-deploy — the typed-SDK verbs behind scripts/setup.py.

The role model (see README "Identity & permissions"):
  in_process:  the APP SP is a MEMBER of the role group (inherits the evidence/tool grants) — made a member
               post-deploy by the admin (admin_postdeploy), with warehouse CAN_USE granted directly to it.
               Nothing to wire here.
  job:         the JOB SP is a MEMBER of the role group too (inherits the grants — admin pre-allocates that,
               no secret). Post-deploy we wire only what the job actually needs:
                 app SP → CAN_MANAGE_RUN on the investigate job  (grant_app_can_manage_run_on_job)
                 job SP → CAN_READ on the bundle dir             (grant_dir_read, to read its own notebook)
               The job needs NO app permission at all: it reports by appending to the Lakebase journal
               (lib/journal.py), whose INSERT grant is issued by src/build_structure.py.

The role's own data grants (evidence SELECT / tool EXECUTE) and both SPs' group membership are set by the
ADMIN out of band. Most grants here are typed SDK methods; grant_sp_execute_on_mcp_service is raw REST
(see its own docstring — MCP Services are new enough there's no confirmed typed SDK coverage yet).

MCP tool-routing (databricks_ops/mcp_connection.py) adds two more grants, for whichever SP is the
deployed stack's investigation-time caller (app SP for in_process, job SP otherwise):
  grant_sp_can_use_app             — lets that SP's OAuth M2M credential (embedded in the UC connection)
                                      past the app's own OAuth edge (the "door key" — see README).
  grant_sp_execute_on_mcp_service  — lets that SP actually invoke the enrich_indicator MCP Service.
                                      NEVER grant USE CONNECTION instead — that would let the grantee
                                      bypass tool selection and call the backend directly.
"""
from __future__ import annotations

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import apps, jobs


def grant_app_can_manage_run_on_job(w: WorkspaceClient, job_id: int, app_sp_app_id: str) -> None:
    """job mode: let the app SP trigger the investigate job."""
    w.jobs.update_permissions(
        job_id=str(job_id),
        access_control_list=[jobs.JobAccessControlRequest(
            service_principal_name=app_sp_app_id, permission_level=jobs.JobPermissionLevel.CAN_MANAGE_RUN)])


# REMOVED: grant_job_sp_can_use_app. The job SP needed CAN_USE on the app only for the HTTP verdict
# callback; with the append-only journal (lib/journal.py) the job never contacts the app.


def grant_dir_read(w: WorkspaceClient, dir_path: str, job_sp_app_id: str) -> None:
    """job mode: let the job SP READ the deployed notebook it runs. The investigate job's run_as is the job
    SP, a DIFFERENT identity than the deployer who owns the deployed bundle files — a run_as SP that can't
    read the notebook dies INTERNAL_ERROR "Unable to access the notebook ... lacks the required permissions"
    (verified 2026-08-04). We grant CAN_READ on the bundle ROOT directory (workspace ACLs are inherited by
    the whole subtree, so this covers files/src/investigate). Additive (update_permissions merges, not
    replaces) — unlike a bundle `permissions` block, which is authoritative and would reset the job's ACL on
    every deploy + wipe the app SP's CAN_MANAGE_RUN. That's why this lives here as a post-deploy grant."""
    from databricks.sdk.service import workspace as ws
    obj_id = w.workspace.get_status(dir_path).object_id
    w.workspace.update_permissions(
        workspace_object_type="directories", workspace_object_id=str(obj_id),
        access_control_list=[ws.WorkspaceObjectAccessControlRequest(
            service_principal_name=job_sp_app_id,
            permission_level=ws.WorkspaceObjectPermissionLevel.CAN_READ)])

# Note: the mode-appropriate wiring lives in the readable recipe (scripts/setup.py) — in job mode it calls
# grant_app_can_manage_run_on_job + grant_dir_read. in_process needs none of these (the app SP's role
# membership + direct warehouse grant are done by the admin post-deploy, admin_postdeploy). Keeping that
# sequencing in the recipe (not a composite function here) is deliberate: the step order should be legible.


def grant_sp_can_use_app(w: WorkspaceClient, app_name: str, sp_app_id: str) -> None:
    """MCP tool routing: let the SP embedded in the UC connection (databricks_ops/mcp_connection.py)
    past the app's own OAuth-M2M edge. update_permissions (additive), matching grant_app_can_manage_run_on_job's
    style — this must not reset the app's existing ACL."""
    w.apps.update_permissions(
        app_name=app_name,
        access_control_list=[apps.AppAccessControlRequest(
            service_principal_name=sp_app_id, permission_level=apps.AppPermissionLevel.CAN_USE)])


def grant_sp_execute_on_mcp_service(w: WorkspaceClient, catalog: str, schema: str, service_name: str,
                                    sp_app_id: str) -> None:
    """MCP tool routing: let the SP invoke the enrich_indicator MCP Service. Raw REST — MCP Services are
    new enough that (as of this writing) there's no confirmed typed SDK grant method, mirroring
    databricks_ops/lakebase.py's precedent for SDK-uncovered UC surfaces. VERIFY LIVE: the exact
    securable_type path segment ("mcp-service", by analogy with the /mcp-services collection and other
    UC permission paths like .../permissions/function/...) against
    docs.databricks.com/aws/en/ai-gateway/register-mcp-service, which confirms the body shape
    ({"changes": [{"principal": ..., "add": ["EXECUTE"]}]}) but not the exact path segment.

    NEVER grant USE CONNECTION here instead — the docs explicitly warn it lets the grantee bypass tool
    selection and call the backend directly."""
    full_name = f"{catalog}.{schema}.{service_name}"
    w.api_client.do("PATCH", f"/api/2.1/unity-catalog/permissions/mcp-service/{full_name}",
                    body={"changes": [{"principal": sp_app_id, "add": ["EXECUTE"]}]})
