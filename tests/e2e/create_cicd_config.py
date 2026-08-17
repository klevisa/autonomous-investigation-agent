#!/usr/bin/env python3
"""CI/CD ONLY — push the deployer's credentials + the PROD_* bundle values to GitHub Actions so a
dispatch/merge deploys prod as the CI service principal. Runs AFTER admin_prereqs (which created the
deployer — the CI SP in prod — and wrote DEPLOYER_PROFILE/DEPLOYER_SP back to the config).

    python3 -m tests.e2e.create_cicd_config <in_process|job|job_warehouse>
"""
import subprocess
import sys

from tests.harness import config, dbx, identities, report
from scripts import setup_cicd


def _sp_num(admin_client, sp_app: str) -> str:
    """The numeric SCIM id for an SP applicationId (the secrets proxy takes the numeric id, not the appId)."""
    for sp in admin_client.service_principals.list():
        if sp.application_id == sp_app:
            return str(sp.id)
    sys.exit(f"could not resolve numeric id for deployer SP {sp_app}")


def main(mode: str) -> None:
    cfg = config.load()
    admin = cfg.require("ADMIN_PROFILE")
    repo = cfg.require("GH_REPO")
    if subprocess.run(["gh", "--version"], capture_output=True).returncode != 0:
        sys.exit("gh not found — install + authenticate the GitHub CLI")

    deployer_profile = cfg.require("DEPLOYER_PROFILE")
    deployer_sp = cfg.require("DEPLOYER_SP")
    # Mint a FRESH ADDITIONAL OAuth secret for the deployer SP (mint_additional_*, NOT mint_oauth_secret which
    # deletes existing) so the deployer's CLI-profile secret — which PgState uses to read prod Lakebase —
    # stays valid. It's passed to setup_cicd.push in memory (which hands it to `gh` over stdin); never stored.
    w = dbx.client(admin)
    client_secret = identities.mint_additional_oauth_secret(w, _sp_num(w, deployer_sp))
    host = dbx.profile_field(deployer_profile, "host") or dbx.profile_field(admin, "host")

    report.step(f"push CI/CD config to GitHub (deployer/CI SP {deployer_sp}) via setup_cicd.push")
    setup_cicd.push(
        repo,
        dbx_host=host, dbx_client_id=deployer_sp, dbx_client_secret=client_secret,
        catalog=cfg.require("CATALOG"), schema=cfg.require("SCHEMA"),
        warehouse_id=cfg.require("WAREHOUSE_ID"), project=cfg.require("LAKEBASE_PROJECT"),
        pg_database=cfg.require("PG_DATABASE"),
        llm_endpoint_url=cfg.require("AIA_LLM_ENDPOINT_URL"),
        llm_service_credential=cfg.require("AIA_LLM_SERVICE_CREDENTIAL"),
        llm_secret_arn=cfg.require("AIA_LLM_SECRET_ARN"),
        agent_mode=mode, job_sp=cfg.get("JOB_SP"), app_name=cfg.require("APP_NAME"),
        llm_secret_region=cfg.get("AIA_LLM_SECRET_REGION"),
        llm_secret_json_key=cfg.get("AIA_LLM_SECRET_JSON_KEY"),
        # Isolate THIS test suffix's prod bundle root so back-to-back cicd rows don't contend on the shared
        # /Workspace/Shared/.bundle/aia-poc/prod (empty suffix → default root, i.e. real prod behaviour).
        deploy_suffix=(f"-{cfg.get('TEST_SUFFIX')}" if cfg.get("TEST_SUFFIX") else ""),
    )
    print(f"\nCI/CD config pushed (mode={mode}). Next: the orchestrator triggers + watches the CI deploy.")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("in_process", "job", "job_warehouse"):
        sys.exit("usage: python -m tests.e2e.create_cicd_config <in_process|job|job_warehouse>")
    main(sys.argv[1])
