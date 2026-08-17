#!/usr/bin/env python3
"""ADMIN prereqs (as ADMIN_PROFILE). What a platform admin sets up ONCE before the app
team deploys. Idempotent-ish; writes discovered ids back into config.env.

    python3 -m tests.e2e.admin_prereqs <in_process|job|job_warehouse>

main() is split into two halves so the flow is explicit:

  TEST SCAFFOLDING — a AIA admin would NOT normally do these; they exist only to make the test
  self-contained (in prod AIA already has an evidence schema, tools, and data):
    1. create the evidence schema (admin-owned).
    2. create the AIA role (account group) + read/execute on the schema. NO warehouse grant — the
       warehouse is granted per-SP below (to whichever SP actually runs the tools), never to the broad role.
    3. create the SEEDER SP with grants applied DIRECTLY (no group): USE_CATALOG + USE_SCHEMA +
       CREATE_TABLE/FUNCTION/VOLUME, plus its own OAuth secret + CLI profile so it can deploy & run the demo
       bundle. These CREATE_* grants belong to the seeder (seeding is the seeder's job). (Its Lakebase grants
       come later, from the deployer/owner, after build_structure.)

  WHAT THE AIA TEAM ACTUALLY DOES:
    4. (job / job_warehouse) create the JOB SP, add it as a MEMBER of the role (inherits tool grants by
       membership, no secret), grant it ACCESS on the LLM service credential (the pre-deploy tool-runner),
       and — job_warehouse only — grant it warehouse CAN_USE DIRECTLY (it hits the warehouse as itself).
    5. create the deployer / CI SP (non-admin) + OAuth secret + CLI profile, and (job modes) grant it
       servicePrincipal.user on the job SP so it can set run_as. Skipped if DEPLOYER_PROFILE already names a
       real regular user.

  (in_process's warehouse CAN_USE + group membership land post-deploy in 02_admin_postdeploy, on the APP SP,
   which is only born at deploy. LLM token rotation is in 00b_admin_aws_secret.)

Reuses databricks_ops.groups.wait_for_sp_in_account_dir for the SCIM-propagation gate; the account-admin
primitives (create group/SP, mint secret, rule-set writes, warehouse/UC grants) live in tests/harness/identities.
"""
import json
import sys
from pathlib import Path

from tests.harness import config, dbx, identities, report
from databricks_ops import groups

REPO_ROOT = Path(__file__).resolve().parents[2]

SP_USER_ROLE = "roles/servicePrincipal.user"


def _envtok(cfg) -> str:
    """Suffix for synthesized-identity names; append the target so multi-env runs don't collide."""
    suffix = cfg.get("TEST_SUFFIX", "t1")
    target = cfg.bundle_target
    return f"{suffix}-{target}" if target else suffix


# ═══════════════════ TEST SCAFFOLDING (a AIA admin would not normally do this) ═══════════════════

def create_schema(cfg, admin: str) -> None:
    """1) create the evidence schema (admin owns it). Idempotent — tolerate "already exists"."""
    catalog, schema = cfg.require("CATALOG"), cfg.require("SCHEMA")
    report.step(f"1) create the evidence schema '{catalog}.{schema}' (admin-owned)")
    r = dbx.cli(admin, "schemas", "create", schema, catalog, check=False)
    print(f"  {'created' if r.returncode == 0 else 'reusing'} schema {catalog}.{schema}")


def create_aia_role(cfg, admin: str, account_id: str) -> str:
    """2) create the AIA role (account group) + assign to workspace + read/execute on the schema. Returns
    the account-group id. NO warehouse grant here — the warehouse is granted to whichever SP actually runs
    the tools (the job SP for job_warehouse in step 4; the app SP post-deploy for in_process), never to the
    broad role."""
    role, catalog, schema = cfg.require("ROLE_GROUP"), cfg.require("CATALOG"), cfg.require("SCHEMA")
    w = dbx.client(admin)
    report.step(f"2) create the AIA role (account group '{role}') + read/execute on {catalog}.{schema}")
    group = identities.find_or_create_group(w, role)
    gid = group["id"]
    cfg.set("ASSUME_GROUP", gid)
    identities.assign_to_workspace(w, account_id, dbx.profile_field(admin, "workspace_id"), gid)
    identities.add_group_entitlements(w, gid, ["workspace-access", "databricks-sql-access"])
    identities.uc_grant_catalog(w, catalog, role, ["USE_CATALOG"])
    identities.uc_grant_schema(w, catalog, schema, role, ["USE_SCHEMA", "SELECT", "EXECUTE"])
    print(f"  role {role} assigned to workspace + granted USE_CATALOG + USE_SCHEMA/SELECT/EXECUTE (no warehouse)")
    return gid


def create_seeder_sp(cfg, admin: str, account_id: str) -> None:
    """3) create the SEEDER SP with grants applied DIRECTLY (no group): USE_CATALOG + USE_SCHEMA +
    CREATE_TABLE/FUNCTION/VOLUME, plus its own OAuth secret + CLI profile so it can deploy & run the demo
    bundle. These CREATE_* grants belong to the seeder (seeding is its job). Idempotent;
    reuses SEEDER_PROFILE if already set (so it doesn't invalidate an in-use secret)."""
    catalog, schema = cfg.require("CATALOG"), cfg.require("SCHEMA")
    w = dbx.client(admin)
    report.step("3) create the SEEDER SP + direct CREATE_* grants + its own CLI profile")
    name = cfg.get("SEEDER_SP_NAME") or f"aia-test-seeder-{_envtok(cfg)}"
    sp = identities.find_or_create_sp(w, name)
    sp_num, sp_app = sp["id"], sp["applicationId"]
    cfg.set("SEEDER_SP", sp_app)
    identities.add_entitlements(w, sp_num, ["workspace-access", "databricks-sql-access"])
    identities.assign_to_workspace(w, account_id, dbx.profile_field(admin, "workspace_id"), sp_num)
    # UC grants applied DIRECTLY to the seeder SP (no group): it creates the demo Delta tables/functions + a
    # CSV volume. No warehouse — the seed runs on Spark, not the warehouse.
    identities.uc_grant_catalog(w, catalog, sp_app, ["USE_CATALOG"])
    identities.uc_grant_schema(w, catalog, schema, sp_app,
                               ["USE_SCHEMA", "CREATE_TABLE", "CREATE_FUNCTION", "CREATE_VOLUME"])
    print(f"  seeder SP {sp_app} granted USE_CATALOG + USE_SCHEMA/CREATE_TABLE/FUNCTION/VOLUME (no warehouse)")
    if cfg.get("SEEDER_PROFILE"):
        print(f"  (reusing existing SEEDER_PROFILE={cfg.get('SEEDER_PROFILE')})")
        return
    secret = identities.mint_oauth_secret(w, sp_num)   # delete-all-then-mint-one (dodges the 5-cap)
    host = dbx.profile_field(admin, "host")
    seeder_profile = f"{admin}-seeder"
    identities.write_cli_profile(seeder_profile, host=host, client_id=sp_app,
                                 client_secret=secret, account_id=account_id)
    cfg.set("SEEDER_PROFILE", seeder_profile)
    print(f"  seeder SP → CLI profile {seeder_profile} (deploys & runs the demo bundle)")


# ═══════════════════ WHAT THE AIA TEAM ACTUALLY DOES ═══════════════════

def create_job_sp(cfg, admin: str, account_id: str, gid: str, mode: str) -> None:
    """4) (job / job_warehouse) create the JOB SP, add it as a MEMBER of the role (inherits tool grants),
    grant it LLM-credential ACCESS (it's the pre-deploy tool-runner), and — job_warehouse only — warehouse
    CAN_USE DIRECTLY (it hits the warehouse as itself). The deployer's servicePrincipal.user on it is granted
    in step 5 (once the deployer exists)."""
    role = cfg.require("ROLE_GROUP")
    w = dbx.client(admin)
    report.step("4) (job) create the job SP + ADD IT TO THE ROLE (membership → inherits tool grants)")
    name = cfg.get("JOB_SP_NAME") or f"aia-test-jobsp-{_envtok(cfg)}"
    sp = identities.find_or_create_sp(w, name)
    sp_num, sp_app = sp["id"], sp["applicationId"]
    print(f"  job SP num={sp_num} app={sp_app}")
    cfg.set("JOB_SP", sp_app)
    # wait for account-SCIM propagation before the membership PATCH
    groups.wait_for_sp_in_account_dir(w, sp_app)
    identities.add_entitlements(w, sp_num, ["workspace-access"])
    identities.assign_to_workspace(w, account_id, dbx.profile_field(admin, "workspace_id"), sp_num)
    identities.add_group_member(w, gid, sp_num)
    print(f"  added job SP as a MEMBER of role group {role}")

    # LLM credential ACCESS: the job SP is known pre-deploy (created just above), so grant it here rather than
    # in 02 post-deploy — nothing about it depends on the app existing. Matches L2, which grants this in its 00.
    cred = cfg.get("AIA_LLM_SERVICE_CREDENTIAL")
    if cred:
        report.step(f"4b) grant the job SP ACCESS on the LLM service credential '{cred}'")
        dbx.cli(admin, "grants", "update", "credential", cred,
                "--json", json.dumps({"changes": [{"principal": sp_app, "add": ["ACCESS"]}]}), check=False)
        print(f"  granted ACCESS on {cred} to job SP {sp_app}")

    # job_warehouse ONLY: the job runs tools on the warehouse AS ITSELF (run_as=job SP), so grant CAN_USE
    # directly on the SP. Plain `job` runs tools on its own Spark and needs no warehouse.
    if mode == "job_warehouse":
        warehouse = cfg.require("WAREHOUSE_ID")
        report.step(f"4c) (job_warehouse) grant the job SP CAN_USE on warehouse {warehouse}")
        identities.grant_warehouse_can_use_sp(w, warehouse, sp_app)
        print(f"  granted warehouse CAN_USE directly on job SP {sp_app}")


def synth_deployer(cfg, admin: str, account_id: str, mode: str) -> None:
    """5) create the deployer / CI SP (non-admin) + OAuth secret + CLI profile — and (job modes) grant it servicePrincipal.user on the
    job SP so it can set run_as. Skipped if DEPLOYER_PROFILE is already set (any existing profile — a real
    user or a pre-provisioned SP). The deployer gets NO catalog/schema grants: it only deploys the bundle,
    provisions Lakebase, and owns those tables."""
    w = dbx.client(admin)
    if cfg.get("DEPLOYER_PROFILE"):
        print(f"  (using existing DEPLOYER_PROFILE={cfg.get('DEPLOYER_PROFILE')} — a user or pre-provisioned SP)")
    else:
        report.step("5) synthesize a REGULAR-USER deployer/CI SP + OAuth secret + CLI profile")
        name = f"aia-test-deployer-{_envtok(cfg)}"
        sp = identities.find_or_create_sp(w, name)
        sp_num, sp_app = sp["id"], sp["applicationId"]
        identities.add_entitlements(w, sp_num, ["workspace-access", "databricks-sql-access"])
        secret = identities.mint_oauth_secret(w, sp_num)   # delete-all-then-mint-one (dodges the 5-cap)
        host = dbx.profile_field(admin, "host")
        deployer_profile = f"{admin}-deployer"
        identities.write_cli_profile(deployer_profile, host=host, client_id=sp_app,
                                     client_secret=secret, account_id=account_id)
        cfg.set("DEPLOYER_PROFILE", deployer_profile)
        cfg.set("DEPLOYER_SP", sp_app)
        print(f"  deployer SP app={sp_app} (non-admin) → profile {deployer_profile}")

    if mode in ("job", "job_warehouse"):
        job_sp = cfg.require("JOB_SP")
        dep = dbx.cli_json(cfg.require("DEPLOYER_PROFILE"), "current-user", "me")["userName"]
        report.step(f"5b) grant the deployer ({dep}) servicePrincipal.user on the job SP (to set it as run_as)")
        identities.grant_role_on_sp(w, account_id, job_sp, SP_USER_ROLE, f"servicePrincipals/{dep}")
        print(f"  granted deployer servicePrincipal.user on job SP {job_sp}")


def main(mode: str) -> None:
    cfg = config.load()
    admin = cfg.require("ADMIN_PROFILE")
    account_id = cfg.get("ACCOUNT_ID") or dbx.account_id(admin)
    if not account_id:
        sys.exit("could not resolve account id from ADMIN_PROFILE")
    if mode in ("in_process", "job_warehouse") and not cfg.get("WAREHOUSE_ID"):
        sys.exit(f"set WAREHOUSE_ID in config.env ({mode} runs tools through the warehouse)")

    # ── TEST SCAFFOLDING (a AIA admin would not normally do this — it makes the test self-contained) ──
    create_schema(cfg, admin)
    gid = create_aia_role(cfg, admin, account_id)
    create_seeder_sp(cfg, admin, account_id)

    # ── WHAT THE AIA TEAM ACTUALLY DOES ──
    if mode in ("job", "job_warehouse"):
        create_job_sp(cfg, admin, account_id, gid, mode)
    synth_deployer(cfg, admin, account_id, mode)

    print(f"\nADMIN prereqs done for mode={mode}. Next: user runs deploy.py")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("in_process", "job", "job_warehouse"):
        sys.exit("usage: python -m tests.e2e.admin_prereqs <in_process|job|job_warehouse>")
    main(sys.argv[1])
