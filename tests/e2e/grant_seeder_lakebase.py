#!/usr/bin/env python3
"""TEST SCAFFOLDING: the DEPLOYER (which OWNS the Lakebase tables — it ran
build_structure) grants the SEEDER SP the Lakebase access the demo seed needs. Runs AFTER (the
tables exist) and BEFORE (the seed writes them).

    python3 -m tests.e2e.grant_seeder_lakebase

This is deliberately NOT in the product's build_structure notebook: the seeder is a test-only identity, so
its grant lives in the harness, issued by the table OWNER (the deployer) — the same mechanism build_structure
uses for the app/job SPs (map the SP to a Postgres role via the control-plane, then GRANT by that role name).

What the seed does as the seeder (see demo/src/seed_demo_data.py): DELETE FROM investigations; DELETE FROM
cases WHERE source=...; INSERT INTO cases; SELECT count(*) FROM cases. So the seeder needs USAGE on the
schema + SELECT/INSERT/DELETE on `cases` + DELETE on `investigations` (it never touches the journal).
"""
import time

from tests.harness import config, report
from databricks.sdk import WorkspaceClient
from lib import resolve
from lib.pg import make_pg_connect, pg_exec


def _grant_with_role_retry(conn, statements, role_name, attempts=30, delay=2):
    """Run the GRANTs, retrying the whole (idempotent) set while Postgres still reports the freshly created
    role as absent — a control-plane-created role isn't usable in-DB immediately, so the first GRANT can fail
    SQLSTATE 42704 ('role "<id>" does not exist'). Any other error raises at once. (Mirrors the helper in
    src/build_structure.py; the seeder is test-only, so this copy lives in the harness.)"""
    for _i in range(attempts):
        try:
            for _stmt in statements:
                pg_exec(conn, _stmt)
            return
        except Exception as e:  # noqa: BLE001
            if "does not exist" in str(e) and role_name in str(e):
                time.sleep(delay)
                continue
            raise
    raise RuntimeError(f"role '{role_name}' never became usable for GRANT after {attempts * delay}s")


def main() -> None:
    cfg = config.load()
    deployer = cfg.require("DEPLOYER_PROFILE")   # OWNS the Lakebase tables (it ran build_structure)
    seeder = cfg.require("SEEDER_SP")
    schema = cfg.require("SCHEMA")
    project = cfg.require("LAKEBASE_PROJECT")
    pg_database = cfg.require("PG_DATABASE")
    branch = cfg.get("LAKEBASE_BRANCH", "production")
    endpoint = cfg.get("LAKEBASE_ENDPOINT", "primary")

    report.step(f"03b: DEPLOYER grants the SEEDER SP ({seeder}) Postgres access on {schema}.cases/investigations")
    w = WorkspaceClient(profile=deployer)
    me = w.current_user.me().user_name   # the deployer is the Postgres owner user
    host = resolve.pg_host(w, project, branch, endpoint)
    endpoint_path = resolve.endpoint_path(project, branch, endpoint)

    # 1. map the seeder SP to a Postgres role (its in-DB role name == the SP client id). Idempotent.
    branch_path = f"projects/{project}/branches/{branch}"
    try:
        w.api_client.do("POST", f"/api/2.0/postgres/{branch_path}/roles",
                        query={"role_id": "seedersp"},
                        body={"spec": {"identity_type": "SERVICE_PRINCIPAL",
                                       "auth_method": "LAKEBASE_OAUTH_V1", "postgres_role": seeder}})
        print("  mapped seeder SP → Postgres role")
    except Exception as e:  # noqa: BLE001
        print(f"  (create pg role: {str(e)[:120]} — continuing; role may already exist)")

    # 2. GRANT to that role by its in-DB name (== the SP client id) as the OWNER, in the deployer's session.
    #    seeder is a UUID from config (not user input), so f-string interpolation of the identifier is safe.
    conn = make_pg_connect(w, host=host, database=pg_database, user=me,
                           endpoint_path=endpoint_path, schema=schema)
    grants = (f'GRANT USAGE ON SCHEMA "{schema}" TO "{seeder}"',
              f'GRANT SELECT, INSERT, DELETE ON "{schema}".cases TO "{seeder}"',
              f'GRANT DELETE ON "{schema}".investigations TO "{seeder}"')
    _grant_with_role_retry(conn, grants, seeder)
    print(f"  granted USAGE + SELECT/INSERT/DELETE on {schema}.cases + DELETE on {schema}.investigations "
          f"to seeder {seeder}")
    print("\nseeder Lakebase access ready. Next: 04 runs the demo seed AS THE SEEDER.")


if __name__ == "__main__":
    main()
