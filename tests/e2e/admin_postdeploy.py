#!/usr/bin/env python3
"""ADMIN post-deploy work (as ADMIN_PROFILE). These need the app SP (born at deploy) and/or account-host
+ metastore rights a regular deployer lacks, so the admin does them once, after deploy.

    python3 -m tests.e2e.admin_postdeploy <in_process|job|job_warehouse>

Two parts:

  1) Tool-access grants for the IN_PROCESS runner (in_process only): (a) make the app SP a MEMBER of the
     AIA role (it inherits the evidence SELECT / tool EXECUTE grants by membership — no role assumption,
     no minted token), (b) grant it warehouse CAN_USE DIRECTLY (it hits the warehouse as itself), and
     (c) grant it ACCESS on the LLM UC service credential (it mints the gateway token at runtime).
     job / job_warehouse need none of this — the job SP is the tool-runner, known pre-deploy, so
     admin_prereqs already granted it role MEMBERSHIP + LLM-credential ACCESS (+ warehouse CAN_USE for
     job_warehouse).

  2) Provision the custom-MCP-server variation (enrich_indicator) — ALL modes. Create the UC HTTP
     Connection + MCP Service and grant the caller SP CAN_USE (app) + EXECUTE (service). This is admin/
     owner work (CREATE CONNECTION is metastore-level; minting the SP's embedded OAuth M2M secret needs
     servicePrincipal.manager; the service is created in the AIA-owned schema), and it runs POST-deploy in
     every mode because the custom MCP server is hosted in the app, so its URL only exists after deploy.
     The embedded/granted SP is the app SP (in_process) or the job SP (job modes) — see tests/harness/mcp.py.
"""
import json
import sys

from tests.harness import config, dbx, identities, mcp, report
# reuse the PRODUCT's SCIM-propagation gate + group-id resolver — do not re-implement
from databricks_ops import groups


def _grant_inprocess_tool_access(cfg, w, admin: str, app_name: str, role: str) -> None:
    """in_process only: the app SP is the tool-runner, so it needs role membership + warehouse + LLM ACCESS."""
    app_sp = dbx.app_sp(admin, app_name)
    if not app_sp:
        sys.exit("could not resolve the app SP — run 01_deploy.py first")

    # 1) app SP → MEMBER of the AIA role. The app runs UC-function tools with its OWN ambient identity
    #    (WorkspaceClient()), so it needs the role's evidence SELECT / tool EXECUTE grants by MEMBERSHIP —
    #    the same way the job SP gets them (job mode).
    report.step(f"1) add the APP SP ({app_sp}) as a MEMBER of role '{role}' (inherits tool grants)")
    groups.wait_for_sp_in_account_dir(w, app_sp)   # the app SP lags account SCIM ~1-3 min after deploy
    gid = groups.resolve_account_group_id(w, role)
    sp = identities.find_sp(w, application_id=app_sp)
    if not sp:
        sys.exit(f"app SP {app_sp} not resolvable in the workspace directory yet")
    identities.add_group_member(w, gid, sp["id"])
    print(f"  app SP added as a member of {role}")

    # 2) app SP → warehouse CAN_USE directly (it runs the UC-function tools on the warehouse as itself).
    warehouse = cfg.require("WAREHOUSE_ID")
    report.step(f"2) grant the APP SP CAN_USE on warehouse {warehouse}")
    identities.grant_warehouse_can_use_sp(w, warehouse, app_sp)
    print(f"  granted warehouse CAN_USE directly on app SP {app_sp}")

    # 3) app SP → ACCESS on the LLM service credential (mints the gateway token at runtime — lib/llm.py).
    cred = cfg.get("AIA_LLM_SERVICE_CREDENTIAL")
    report.step(f"3) grant the APP SP ACCESS on the LLM service credential '{cred}'")
    if cred:
        dbx.cli(admin, "grants", "update", "credential", cred,
                "--json", json.dumps({"changes": [{"principal": app_sp, "add": ["ACCESS"]}]}), check=False)
        print(f"  granted ACCESS on {cred} to {app_sp}")
    else:
        print("  (AIA_LLM_SERVICE_CREDENTIAL unset — skipping; set it in config.env)")


def _provision_custom_mcp(cfg, w, mode: str, app_name: str) -> None:
    """ALL modes: the custom MCP server (enrich_indicator) is app-hosted, so provision its UC HTTP
    Connection + MCP Service now (post-deploy — the app URL only exists after deploy). mcp.provision resolves
    the caller SP from the mode (app SP for in_process, job SP otherwise), embeds a fresh OAuth M2M secret,
    and grants that SP CAN_USE on the app + EXECUTE on the service."""
    catalog, schema = cfg.require("CATALOG"), cfg.require("SCHEMA")
    job_sp = (cfg.get("JOB_SP") or "").strip()
    app_url = w.apps.get(name=app_name).url
    custom_mcp_app_url = f"{app_url.rstrip('/')}/mcp/"   # trailing slash: avoids the app's Mount 307-redirect
    report.step(f"provision the custom-MCP UC HTTP Connection + MCP Service (enrich_indicator, {mode})")
    result = mcp.provision(w, agent_mode=mode, app_name=app_name, job_sp_client_id=job_sp,
                           catalog=catalog, schema=schema, custom_mcp_app_url=custom_mcp_app_url)
    print(f"  {result}")


def main(mode: str) -> None:
    cfg = config.load()
    admin = cfg.require("ADMIN_PROFILE")
    app_name = cfg.require("APP_NAME")
    role = cfg.require("ROLE_GROUP")
    w = dbx.client(admin)

    if mode == "in_process":
        _grant_inprocess_tool_access(cfg, w, admin, app_name, role)
    else:
        print(f"{mode}: tool access via role MEMBERSHIP + credential ACCESS (+ warehouse for job_warehouse) "
              f"granted in admin_prereqs — nothing mode-specific here.")

    _provision_custom_mcp(cfg, w, mode, app_name)

    print(f"\nADMIN post-deploy done (mode={mode}). Next: USER runs 03_setup.py")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("in_process", "job", "job_warehouse"):
        sys.exit("usage: python -m tests.e2e.admin_postdeploy <in_process|job|job_warehouse>")
    main(sys.argv[1])
