#!/usr/bin/env python3
"""The regular USER runs post-deploy setup (as DEPLOYER_PROFILE) via the SHIPPED
scripts/setup.py (Lakebase provision + build_structure + job-mode app↔job wiring), then ASSERTS it
completed cleanly with no admin-only step left in it.

    python3 -m tests.e2e.setup <in_process|job>
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from tests.harness import config, dbx, report
from tests.harness.pgstate import PgState

REPO_ROOT = Path(__file__).resolve().parents[2]


def main(mode: str) -> None:
    cfg = config.load()
    deployer = cfg.require("DEPLOYER_PROFILE")
    project = cfg.require("LAKEBASE_PROJECT")
    pg_database = cfg.require("PG_DATABASE")

    report.step(f"run the shipped scripts/setup.py as the regular user ({deployer})")
    cp = subprocess.run(["python3", "scripts/setup.py"], cwd=str(REPO_ROOT),
                        env={**os.environ, "PROFILE": deployer, "TARGET": cfg.bundle_target})
    rc = cp.returncode
    print(f"  setup.py exit={rc}")

    report.step("verify setup.py succeeded end-to-end as the regular user")
    r = report.Results()
    r.assert_eq("setup.py exit code 0 (no admin-only step left in it)", 0, rc)

    def lakebase_project_exists() -> bool:
        return dbx.cli_json(deployer, "api", "get",
                            f"/api/2.0/postgres/projects/{project}") is not None

    def pg_database_exists() -> bool:
        resp = dbx.cli_json(deployer, "api", "get",
                            f"/api/2.0/postgres/projects/{project}/branches/production/databases")
        return pg_database in (json.dumps(resp) if resp is not None else "")

    pg = PgState.from_config(cfg)
    r.check("Lakebase project exists", lakebase_project_exists)
    r.check("Postgres database exists (no UC catalog needed)", pg_database_exists)
    r.check("cases table queryable", lambda: pg.query("SELECT count(*) c FROM cases") is not None)
    r.check("investigations table queryable",
            lambda: pg.query("SELECT count(*) c FROM investigations") is not None)
    r.finish()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("in_process", "job", "job_warehouse"):
        sys.exit("usage: python -m tests.e2e.setup <in_process|job>")
    main(sys.argv[1])
