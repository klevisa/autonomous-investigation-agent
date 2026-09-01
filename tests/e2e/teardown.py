#!/usr/bin/env python3
"""e2e teardown — delete everything a run created, best-effort (each delete tolerates "already gone").

Strategy-aware (cfg.deploy_strategy):
  * dev  — the deployer owns the stage bundle + Lakebase; destroy them as the deployer.
  * cicd — the CI SP deployed prod, but each round mints a FRESH one, so `bundle destroy` (as admin, with
           admin's local state) often can't remove CI-SP-owned resources. We ALSO sweep prod jobs/app by name
           + clear the prod bundle's remote state root as the admin, and delete the GitHub secrets/vars.

The seeder's separate demo/ bundle is destroyed as the seeder (its state is always local, either strategy).
Account principals (role group, job SP, seeder SP, synthesized deployer SP) + the admin-owned schema go as the
admin. The LLM AWS secret + UC service credential are DURABLE shared infra — left in place.

    python3 -m tests.e2e.teardown
"""
import configparser
import os
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
from tests.harness import config, dbx, report

REPO_ROOT = HERE.parents[1]
DEMO_DIR = str(REPO_ROOT / "demo")

GH_SECRETS = ["DATABRICKS_HOST", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET"]
GH_VARS = ["PROD_CATALOG", "PROD_SCHEMA", "PROD_WAREHOUSE_ID", "PROD_LAKEBASE_PROJECT", "PROD_PG_DATABASE",
           "PROD_AGENT_MODE", "PROD_JOB_SP", "PROD_APP_NAME", "PROD_LLM_ENDPOINT_URL",
           "PROD_LLM_SERVICE_CREDENTIAL", "PROD_LLM_SECRET_ARN", "PROD_LLM_SECRET_REGION",
           "PROD_LLM_SECRET_JSON_KEY", "PROD_DEPLOY_SUFFIX"]


def _del_sp_by_app(admin: str, app_id: str) -> None:
    if not app_id:
        return
    sps = dbx.cli_json(admin, "service-principals", "list") or []
    sps = sps if isinstance(sps, list) else sps.get("Resources", [])
    num = next((s["id"] for s in sps if s.get("applicationId") == app_id), "")
    if num:
        dbx.cli(admin, "service-principals", "delete", num, check=False)
        print(f"  deleted SP {app_id}")


def _delete_jobs_by_name(admin: str, needle: str) -> None:
    """Delete every job whose name CONTAINS `needle`, as the admin (who can delete any job regardless of
    owner). Substring (not prefix): dev-mode bundle jobs are named '[dev <deployer>] [<target>] AIA …', so
    the meaningful marker '[<target>] AIA' sits mid-name. Catches jobs that `bundle destroy` leaves behind —
    CI-SP-created prod jobs, and dev jobs from a PRIOR run whose deployer/local-state this run no longer has."""
    jobs = dbx.cli_json(admin, "jobs", "list") or []
    jobs = jobs if isinstance(jobs, list) else jobs.get("jobs", [])
    hit = 0
    for j in jobs:
        if needle in (j.get("settings", {}) or {}).get("name", ""):
            dbx.cli(admin, "jobs", "delete", str(j["job_id"]), check=False)
            hit += 1
    if hit:
        print(f"  deleted {hit} lingering job(s) matching '{needle}'")


def _delete_app_and_wait(profile: str, app_name: str, timeout: int = 180) -> None:
    """Delete the app, then BLOCK until it's actually gone. App deletion is ASYNC — without the wait the app
    can still be ACTIVE/DELETING when teardown returns, and the next same-suffix deploy then collides with
    'an app with the same name already exists'. Best-effort: warn and return on timeout."""
    if not app_name:
        return
    dbx.cli(profile, "apps", "delete", app_name, check=False)
    deadline = time.time() + timeout
    while time.time() < deadline:
        cp = dbx.cli(profile, "apps", "get", app_name, check=False)
        out = (cp.stdout or "") + (cp.stderr or "")
        if any(m in out for m in ("does not exist", "RESOURCE_DOES_NOT_EXIST", "not found")):
            print(f"  app {app_name} deleted")
            return
        time.sleep(5)
    print(f"  (app {app_name} still present {timeout}s after delete — may need a manual `apps delete`)")


def _clear_synth_profile(cfg, key: str) -> None:
    """Remove a synthesized CLI profile (name ends -deployer / -seeder) from ~/.databrickscfg + blank the
    config pointer, so the next run re-synthesizes. A REAL user-named profile is left untouched."""
    prof = cfg.get(key)
    if not prof or not (prof.endswith("-deployer") or prof.endswith("-seeder")):
        return
    path = os.path.expanduser("~/.databrickscfg")
    p = configparser.ConfigParser()
    p.read(path)
    if p.has_section(prof):
        p.remove_section(prof)
        with open(path, "w") as f:
            p.write(f)
        print(f"  removed profile {prof}")
    cfg.set(key, "")
    print(f"  cleared {key} (synthesized) so the next run re-synthesizes")


def _purge_lakebase(profile: str, project: str) -> None:
    # purge=true HARD-deletes now (frees the name immediately). A plain delete only SOFT-deletes (name
    # reserved ~7 days); a rerun in that window would hang reusing the dead project.
    if project:
        dbx.cli(profile, "api", "delete", f"/api/2.0/postgres/projects/{project}?purge=true", check=False)


def main() -> None:
    cfg = config.load()
    admin = cfg.require("ADMIN_PROFILE")
    strategy = cfg.deploy_strategy
    target = cfg.bundle_target
    project = cfg.get("LAKEBASE_PROJECT")
    print(f"Tearing down e2e (strategy={strategy}, target={target}, suffix={cfg.get('TEST_SUFFIX')}). "
          f"Errors for missing items are fine.")

    # ── the seeder's demo/ bundle (state is always local — destroy as the seeder) ──
    seeder = cfg.get("SEEDER_PROFILE")
    if seeder:
        report.step("destroy the demo/ bundle (as the seeder)")
        dbx.bundle(seeder, "destroy", "--auto-approve", target=target, cwd=DEMO_DIR, check=False)

    # ── product bundle + Lakebase (strategy-specific) ──
    if strategy == "cicd":
        report.step("cicd: destroy the prod bundle + sweep prod jobs/app/state (as admin)")
        dbx.bundle(admin, "destroy", "--auto-approve", target="prod", check=False)
        _delete_jobs_by_name(admin, "[prod] AIA")
        _delete_app_and_wait(admin, cfg.get("APP_NAME"))
        # Clear the remote bundle STATE roots (admin's destroy can't reliably clear CI-SP-written state; a
        # stale tfstate makes the next fresh-CI-SP deploy fail reconciling now-gone jobs). Both bundles. The
        # roots are per-suffix (deploy_suffix isolates parallel test runs — see databricks.yml), so clear the
        # SUFFIXED path; empty suffix → the base prod root (real-prod behaviour).
        suffix = f"-{cfg.get('TEST_SUFFIX')}" if cfg.get("TEST_SUFFIX") else ""
        for root in (f"/Workspace/Shared/.bundle/aia-poc/prod{suffix}",
                     f"/Workspace/Shared/.bundle/aia-demo/prod{suffix}"):
            dbx.cli(admin, "workspace", "delete", root, "--recursive", check=False)
        _purge_lakebase(admin, project)
        repo = cfg.get("GH_REPO")
        if repo and subprocess.run(["gh", "--version"], capture_output=True).returncode == 0:
            report.step("delete GitHub secrets + PROD_* variables (best-effort)")
            for s in GH_SECRETS:
                subprocess.run(["gh", "secret", "delete", s, "--repo", repo], capture_output=True)
            for v in GH_VARS:
                subprocess.run(["gh", "variable", "delete", v, "--repo", repo], capture_output=True)
    else:
        report.step("dev: destroy the stage bundle (deployer), sweep leftover jobs/app + purge Lakebase (admin)")
        deployer = cfg.require("DEPLOYER_PROFILE")
        dbx.bundle(deployer, "destroy", "--auto-approve", target=target, check=False)
        # bundle destroy runs as the CURRENT deployer with freshly-cleared local state, so it can't see the
        # jobs/app a PRIOR run's (different, synthesized) deployer created → sweep them by name as admin.
        _delete_jobs_by_name(admin, f"[{target}] AIA")
        _delete_app_and_wait(admin, cfg.get("APP_NAME"))
        # Purge as ADMIN, not the deployer: a project leaked by a PRIOR deployer isn't manageable by the
        # current one (PermissionDenied), so a deployer-purge silently no-ops and the project lingers — which
        # then breaks the next run's provision() PATCH. Admin can always purge.
        _purge_lakebase(admin, project)

    # ── shared account principals + admin-owned schema (as admin) ──
    report.step("admin-owned: evidence schema (+ its tables/tools), role group, job/seeder/deployer SPs")
    # The schema is ADMIN-created+owned (admin_prereqs), so DROP as admin with --force (cascades to all
    # tables/functions regardless of which SP — deployer or seeder — owns them).
    dbx.cli(admin, "schemas", "delete", f"{cfg.get('CATALOG')}.{cfg.get('SCHEMA')}", "--force", check=False)
    gid = cfg.get("ASSUME_GROUP")
    if gid:
        dbx.cli(admin, "api", "delete", f"/api/2.0/account/scim/v2/Groups/{gid}", check=False)
    _del_sp_by_app(admin, cfg.get("JOB_SP"))
    _del_sp_by_app(admin, cfg.get("SEEDER_SP"))
    _del_sp_by_app(admin, cfg.get("DEPLOYER_SP"))
    _clear_synth_profile(cfg, "DEPLOYER_PROFILE")
    _clear_synth_profile(cfg, "SEEDER_PROFILE")

    print("\nTeardown done. NOTE: deleting an SP via the workspace SCIM proxy removes its WORKSPACE assignment")
    print("but leaves an inert ACCOUNT-directory entry; clean up out-of-band if it matters. The LLM AWS secret")
    print("+ UC service credential are durable shared infra — left in place.")


if __name__ == "__main__":
    main()
