#!/usr/bin/env python3
"""SHOWCASE phase (as ADMIN_PROFILE) — reach the enrich_indicator MCP server via a UC HTTP Connection +
MCP Service, the governed path you'd use for a genuinely EXTERNAL MCP server.

    python3 -m tests.e2e.showcase_external_mcp <in_process|job|job_warehouse>

This is a deliberately-separate LAYER, not part of AIA's core bring-up: the enrich_indicator server is hosted
inside the app only for demo convenience, so a Databricks-native caller would hit /mcp/ directly under
CAN_USE on the app — no connection needed. We provision the connection + MCP service anyway to demonstrate
the external-MCP pattern. Runs post-deploy (the app-hosted server's URL only exists after deploy) and is
admin/owner work (CREATE CONNECTION is metastore-level; the MCP service is created in the AIA-owned schema;
the connection embeds a secret minted on the admin-owned external-MCP client SP). See tests/harness/mcp.py
and the README ("Showcase: an external MCP server, via a UC connection").
"""
import sys

from tests.harness import config, dbx, mcp, report


def main(mode: str) -> None:
    cfg = config.load()
    admin = cfg.require("ADMIN_PROFILE")
    app_name = cfg.require("APP_NAME")
    catalog, schema = cfg.require("CATALOG"), cfg.require("SCHEMA")
    job_sp = (cfg.get("JOB_SP") or "").strip()
    w = dbx.client(admin)

    app_url = w.apps.get(name=app_name).url
    custom_mcp_app_url = f"{app_url.rstrip('/')}/mcp/"   # trailing slash: avoids the app's Mount 307-redirect
    report.step(f"provision the external-MCP showcase (UC HTTP Connection + MCP Service, enrich_indicator, {mode})")
    result = mcp.provision(w, agent_mode=mode, app_name=app_name, job_sp_client_id=job_sp,
                           catalog=catalog, schema=schema, custom_mcp_app_url=custom_mcp_app_url)
    print(f"  {result}")
    print(f"\nExternal-MCP showcase provisioned (mode={mode}). The agent reaches enrich_indicator through the "
          f"MCP Service; the connection authenticates to the app as the external-MCP client SP.")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("in_process", "job", "job_warehouse"):
        sys.exit("usage: python -m tests.e2e.showcase_external_mcp <in_process|job|job_warehouse>")
    main(sys.argv[1])
