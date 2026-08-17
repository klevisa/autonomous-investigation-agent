#!/usr/bin/env python3
"""Push the AIA PoC's CI/CD configuration to GitHub Actions (deploy SECRETS + prod VARIABLES).

This is the ONLY scripted step for wiring CI/CD. It does NOT create identities or grant anything — those are
admin prerequisites you do once, by hand (see README "Prod admin prerequisites"): a deploying service
principal (the "CI SP") with an OAuth M2M secret, granted catalog/schema + warehouse + (job mode)
servicePrincipal.user on the job SP + ACCESS on the LLM credential, plus the AIA role group + its grants.
Once those exist, this takes the resulting values and sets them on the repo so merge-to-master deploys work.

Prereq: `gh` is authenticated to the account that owns the repo. Secret values are passed to `gh` on STDIN
(not argv), and `gh secret set` handles the libsodium encryption the REST API requires — so the CLI is the
simplest correct path here (raw REST would mean encrypting the value ourselves).

Two ways to run:
  * imported by the harness: `setup_cicd.push(repo, dbx_host=..., dbx_client_id=..., ...)`
  * standalone (a prod operator): env vars + repo positional, mirroring the values below.

    REPO=owner/repo DBX_HOST=.. DBX_CLIENT_ID=.. DBX_CLIENT_SECRET=.. CATALOG=.. SCHEMA=.. WAREHOUSE_ID=.. \
      PROJECT=.. PG_DATABASE=.. LLM_ENDPOINT_URL=.. LLM_SERVICE_CREDENTIAL=.. LLM_SECRET_ARN=.. \
      python3 scripts/setup_cicd.py <owner/repo>
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


def _gh_set(kind: str, name: str, value: str, repo: str) -> None:
    """Set a GitHub Actions secret|variable, passing the value on STDIN so it never appears in the process
    list. `gh secret set` reads the value from stdin when --body is omitted (and encrypts it for us)."""
    subprocess.run(["gh", kind, "set", name, "--repo", repo], input=value, text=True, check=True)


def push(repo: str, *, dbx_host: str, dbx_client_id: str, dbx_client_secret: str,
         catalog: str, schema: str, warehouse_id: str, project: str, pg_database: str,
         llm_endpoint_url: str, llm_service_credential: str, llm_secret_arn: str,
         agent_mode: str = "in_process", job_sp: str = "", app_name: str = "",
         llm_secret_region: str = "", llm_secret_json_key: str = "", deploy_suffix: str = "") -> None:
    """Set the deploy secrets + the PROD_* variables CI (and the prod operator) build config.yml from."""
    print(f"Repo: {repo}   workspace: {dbx_host}")

    print("1) Setting GitHub Actions SECRETS (deploy credentials) ...")
    for name, val in {"DATABRICKS_HOST": dbx_host, "DATABRICKS_CLIENT_ID": dbx_client_id,
                      "DATABRICKS_CLIENT_SECRET": dbx_client_secret}.items():
        _gh_set("secret", name, val, repo)
    print("   3 secrets set.")

    print("2) Setting GitHub Actions VARIABLES (the prod bundle values — the single source of truth) ...")
    variables = {
        "PROD_CATALOG": catalog, "PROD_SCHEMA": schema, "PROD_WAREHOUSE_ID": warehouse_id,
        "PROD_LAKEBASE_PROJECT": project, "PROD_PG_DATABASE": pg_database,
        "PROD_LLM_ENDPOINT_URL": llm_endpoint_url, "PROD_LLM_SERVICE_CREDENTIAL": llm_service_credential,
        "PROD_LLM_SECRET_ARN": llm_secret_arn, "PROD_AGENT_MODE": agent_mode or "in_process",
    }
    # Optional — set only if provided (else CI falls back to its documented defaults).
    for name, val in (("PROD_JOB_SP", job_sp), ("PROD_APP_NAME", app_name),
                      ("PROD_LLM_SECRET_REGION", llm_secret_region),
                      ("PROD_LLM_SECRET_JSON_KEY", llm_secret_json_key),
                      ("PROD_DEPLOY_SUFFIX", deploy_suffix)):
        if val:
            variables[name] = val
    for name, val in variables.items():
        _gh_set("variable", name, val, repo)
    print(f"   {len(variables)} variables set.")
    print("Done. Merge to master to trigger a deploy.")


def _require(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        sys.exit(f"set {name}")
    return val


def main() -> None:
    ap = argparse.ArgumentParser(description="Push AIA CI/CD secrets + prod variables to GitHub.")
    ap.add_argument("repo", nargs="?", default=os.environ.get("REPO"), help="owner/repo")
    a = ap.parse_args()
    if not a.repo:
        sys.exit("usage: setup_cicd.py <owner/repo>  (or set REPO)")
    if subprocess.run(["gh", "--version"], capture_output=True).returncode != 0:
        sys.exit("gh not found — install + authenticate the GitHub CLI")
    push(
        a.repo,
        dbx_host=_require("DBX_HOST"), dbx_client_id=_require("DBX_CLIENT_ID"),
        dbx_client_secret=_require("DBX_CLIENT_SECRET"),
        catalog=_require("CATALOG"), schema=_require("SCHEMA"), warehouse_id=_require("WAREHOUSE_ID"),
        project=_require("PROJECT"), pg_database=_require("PG_DATABASE"),
        llm_endpoint_url=_require("LLM_ENDPOINT_URL"),
        llm_service_credential=_require("LLM_SERVICE_CREDENTIAL"), llm_secret_arn=_require("LLM_SECRET_ARN"),
        agent_mode=os.environ.get("AGENT_MODE", "in_process"), job_sp=os.environ.get("JOB_SP", ""),
        app_name=os.environ.get("APP_NAME", ""), llm_secret_region=os.environ.get("LLM_SECRET_REGION", ""),
        llm_secret_json_key=os.environ.get("LLM_SECRET_JSON_KEY", ""),
    )


if __name__ == "__main__":
    main()
