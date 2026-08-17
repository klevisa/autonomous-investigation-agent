#!/usr/bin/env bash
# Assemble a prod config.yml from the PROD_* values. GitHub Actions repo VARIABLES are the SINGLE source
# of truth for prod, and this script materializes them into config.yml (which is gitignored, so neither
# CI's checkout nor a fresh clone has one). Used two ways, so the prod values never live in two places:
#   * CI:    GitHub injects PROD_* into the env; this runs as-is (see deploy.yml).
#   * Local: the prod operator passes the repo (owner/name); we fetch PROD_* via `gh variable get` first,
#            then write the same file — so `setup.py` against prod reads the exact values CI deploys with.
# app_name is optional (omitted when unset → the databricks.yml default applies).
# llm_secret_region and llm_secret_json_key have defaults (us-west-2 / "token"); the rest are required.
#
# Usage:
#   CI:     bash .github/workflows/write-config.sh
#   Local:  bash .github/workflows/write-config.sh <owner/repo>     # needs `gh` authenticated
set -euo pipefail

REPO="${1:-}"
if [ -n "$REPO" ]; then
  # Local mode: pull the prod values from the repo's Actions variables into the env.
  command -v gh >/dev/null || { echo "gh not found — install the GitHub CLI (see README prereqs)"; exit 1; }
  for v in PROD_CATALOG PROD_SCHEMA PROD_WAREHOUSE_ID PROD_LAKEBASE_PROJECT PROD_PG_DATABASE \
           PROD_JOB_SP PROD_APP_NAME PROD_AGENT_MODE PROD_DEPLOY_SUFFIX \
           PROD_LLM_ENDPOINT_URL PROD_LLM_SERVICE_CREDENTIAL PROD_LLM_SECRET_ARN PROD_LLM_SECRET_REGION PROD_LLM_SECRET_JSON_KEY; do
    # tolerate missing optional vars (PROD_APP_NAME, PROD_AGENT_MODE, PROD_JOB_SP, PROD_LLM_SECRET_REGION, PROD_LLM_SECRET_JSON_KEY); required ones checked below
    val=$(gh variable get "$v" --repo "$REPO" 2>/dev/null || true)
    export "$v"="$val"
  done
fi

: "${PROD_CATALOG:?set repo variable PROD_CATALOG}"
: "${PROD_SCHEMA:?set repo variable PROD_SCHEMA}"
: "${PROD_WAREHOUSE_ID:?set repo variable PROD_WAREHOUSE_ID}"
: "${PROD_LAKEBASE_PROJECT:?set repo variable PROD_LAKEBASE_PROJECT}"
: "${PROD_PG_DATABASE:?set repo variable PROD_PG_DATABASE}"
: "${PROD_LLM_ENDPOINT_URL:?set repo variable PROD_LLM_ENDPOINT_URL}"
: "${PROD_LLM_SERVICE_CREDENTIAL:?set repo variable PROD_LLM_SERVICE_CREDENTIAL}"
: "${PROD_LLM_SECRET_ARN:?set repo variable PROD_LLM_SECRET_ARN}"
# agent_mode: default in_process. job_sp is REQUIRED in both job variants (the investigate job runs as it).
PROD_AGENT_MODE="${PROD_AGENT_MODE:-in_process}"
if [ "$PROD_AGENT_MODE" = "job" ] || [ "$PROD_AGENT_MODE" = "job_warehouse" ]; then
  : "${PROD_JOB_SP:?job/job_warehouse mode: set repo variable PROD_JOB_SP (the job SP the investigate job runs as)}"
fi
# llm_secret_region and llm_secret_json_key have defaults
PROD_LLM_SECRET_REGION="${PROD_LLM_SECRET_REGION:-us-west-2}"
PROD_LLM_SECRET_JSON_KEY="${PROD_LLM_SECRET_JSON_KEY:-token}"

{
  echo "targets:"
  echo "  prod:"
  echo "    variables:"
  echo "      catalog: \"${PROD_CATALOG}\""
  echo "      schema: \"${PROD_SCHEMA}\""
  echo "      warehouse_id: \"${PROD_WAREHOUSE_ID}\""
  echo "      lakebase_project: \"${PROD_LAKEBASE_PROJECT}\""
  echo "      pg_database: \"${PROD_PG_DATABASE}\""
  echo "      llm_endpoint_url: \"${PROD_LLM_ENDPOINT_URL}\""
  echo "      llm_service_credential: \"${PROD_LLM_SERVICE_CREDENTIAL}\""
  echo "      llm_secret_arn: \"${PROD_LLM_SECRET_ARN}\""
  echo "      llm_secret_region: \"${PROD_LLM_SECRET_REGION}\""
  echo "      llm_secret_json_key: \"${PROD_LLM_SECRET_JSON_KEY}\""
  echo "      agent_mode: \"${PROD_AGENT_MODE}\""
  [ -n "${PROD_JOB_SP:-}" ] && echo "      job_sp: \"${PROD_JOB_SP}\""
  [ -n "${PROD_APP_NAME:-}" ] && echo "      app_name: \"${PROD_APP_NAME}\""
  # Optional: isolates the prod bundle root for parallel TEST deployments (empty in real prod → default root).
  [ -n "${PROD_DEPLOY_SUFFIX:-}" ] && echo "      deploy_suffix: \"${PROD_DEPLOY_SUFFIX}\""
  # JOB mode: overlay the investigate job's run_as onto the base resource, so the job runs as the JOB SP —
  # not the deploying identity (the CI SP). Without this the job's run_as defaults to its creator (the CI SP),
  # and every job-SP grant (role membership, journal INSERT, LLM-credential ACCESS) targets the wrong
  # identity → the job fails at runtime. Target-scoped override (targets.prod.resources.*) so it MERGES onto
  # the base job (a second top-level resources block keyed `investigate` would collide). Mirrors
  # tests/harness/render_config.py. The job SP also needs CAN_READ on the deployed notebook — granted
  # post-deploy in scripts/setup.py (job mode), not here (a bundle permissions block resets ACLs each deploy).
  if [ "${PROD_AGENT_MODE}" = "job" ] || [ "${PROD_AGENT_MODE}" = "job_warehouse" ]; then
    echo "    resources:"
    echo "      jobs:"
    echo "        investigate:"
    echo "          run_as:"
    echo "            service_principal_name: \"${PROD_JOB_SP}\""
  fi
} > config.yml

echo "Wrote config.yml for prod:"
cat config.yml
