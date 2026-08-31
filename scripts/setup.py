#!/usr/bin/env python3
"""POST-DEPLOY setup — run by the regular user (or CI) after scripts/deploy.py. This IS the recipe, top to
bottom, each step a thin databricks_ops call or a `databricks bundle run`, so the sequence is visible here.
It never deploys code, so it's safe against a CI-locked prod.

    PROFILE=<cli-profile> TARGET=stage python3 scripts/setup.py
    python3 scripts/setup.py --profile <cli-profile> --target stage
    python3 scripts/setup.py --target prod          # CI: no profile → SDK/CLI use env-var (M2M) auth

Steps:
  1. provision Lakebase (autoscaling Postgres project + the cases/investigations database — no UC catalog).
  2. build_structure — creates the cases/investigations/journal tables + grants the app SP its Postgres
     access (as the table owner, in the same pg8000 session). The app SP id is resolved from the deployed app;
     in job mode the job SP id is also passed (build_structure grants it INSERT-only on the journal).
  3. (job / job_warehouse only) wire the two post-deploy ACLs the app needs to drive the investigate job:
     the app SP → CAN_MANAGE_RUN on the job, and the job SP → CAN_READ on the bundle dir (its run_as identity
     differs from the deployer who owns the files). Both additive, so they survive redeploys.

NOTE: the app SP's MEMBERSHIP in the AIA role is NOT done here — a group-membership PATCH needs account-host
access (account SCIM) the workspace proxy rejects for a regular deployer, so the ADMIN does it out of band
post-deploy (see README "Admin setup"). Runtime resolution (lib/resolve.py) means NO redeploy and NO app
restart: the app resolves its SP id / Lakebase host / job id lazily on next use.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # repo root, so databricks_ops imports
from databricks_ops import config, dbx, lakebase, grants, mcp_connection   # noqa: E402
from bundle import run_bundle   # sibling module in scripts/ (this file is run as `python3 scripts/setup.py`)

JOB_MODES = ("job", "job_warehouse")


def main() -> None:
    ap = argparse.ArgumentParser(description="Post-deploy setup: Lakebase + build_structure + job wiring.")
    ap.add_argument("--profile", default=os.environ.get("PROFILE"))
    ap.add_argument("--target", default=os.environ.get("TARGET") or "stage")
    ap.add_argument("--config", default="config.yml")
    a = ap.parse_args()

    cfg = config.load_config(a.config, a.target)
    project = cfg.get("LAKEBASE_PROJECT") or sys.exit("config.yml: lakebase_project is empty")
    pg_db = cfg.get("PG_DATABASE") or sys.exit("config.yml: pg_database is empty")
    app_name = cfg.get("APP_NAME") or sys.exit("config.yml: app_name is empty")
    mode = (cfg.get("AGENT_MODE") or "in_process").strip()
    job_sp = (cfg.get("JOB_SP") or "").strip()
    w = dbx.workspace(a.profile)   # profile None → SDK env-var (M2M) auth (CI)
    print(f"post-deploy setup: target={a.target}  mode={mode}")

    print(f"== 1. provision Lakebase (project={project} db={pg_db}) ==")
    lakebase.provision(w, project, pg_db)

    print("== 2. build_structure (Lakebase tables + Postgres grants) ==")
    app_sp = w.apps.get(name=app_name).service_principal_client_id
    if not app_sp:
        sys.exit(f"app {app_name!r} has no service principal yet — did deploy.py run?")
    print(f"   app SP = {app_sp}")
    nb_params = f"app_sp_id={app_sp}"
    if mode in JOB_MODES:
        if not job_sp or job_sp == "none":
            sys.exit("config.yml: job_sp is empty (job/job_warehouse — admin pre-allocates the job SP)")
        nb_params += f",job_sp_id={job_sp}"
        print(f"   job SP = {job_sp} (will get INSERT-only on the journal)")
    run_bundle(["run", "build_structure", "--notebook-params", nb_params], a.profile, a.target)

    if mode in JOB_MODES:
        print(f"== 3. ({mode}) wire app↔job permissions ==")
        summary = json.loads(run_bundle(["summary", "-o", "json"], a.profile, a.target, capture=True).stdout)
        job_id = int(summary["resources"]["jobs"]["investigate"]["id"])
        bundle_root = summary["workspace"]["root_path"]
        grants.grant_app_can_manage_run_on_job(w, job_id, app_sp)   # app SP can trigger the job
        grants.grant_dir_read(w, bundle_root, job_sp)               # job SP can read the deployed notebook
        print(f"   app SP CAN_MANAGE_RUN on job {job_id}; job SP CAN_READ on {bundle_root} "
              f"(tools by membership, journal INSERT from step 2)")
    else:
        print("== 3. (in_process) no user-side grants — the ADMIN adds the app SP to the AIA role "
              "post-deploy (see README) ==")

    print("== 4. provision the UC HTTP Connection + MCP Service for enrich_indicator ==")
    app_url = w.apps.get(name=app_name).url
    catalog = cfg.get("CATALOG") or sys.exit("config.yml: catalog is empty")
    schema = cfg.get("SCHEMA") or sys.exit("config.yml: schema is empty")
    result = mcp_connection.provision(
        w, agent_mode=mode, app_name=app_name, job_sp_client_id=job_sp, catalog=catalog, schema=schema,
        custom_mcp_app_url=f"{app_url.rstrip('/')}/mcp/")   # trailing slash: avoids the app's own
                                                             # Mount 307-redirect (verified empirically)
    print(f"   {result}")

    print(f"\n== setup complete ==\n{a.target} is live — the app picks up Lakebase + resolves the role id on "
          f"its next request (no restart).")


if __name__ == "__main__":
    main()
