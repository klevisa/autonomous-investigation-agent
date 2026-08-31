#!/usr/bin/env python3
"""The SEEDER SP loads demo data by deploying + running the SEPARATE demo/ bundle:
Delta substrate + 5 UC-function tools + grants the AIA role EXECUTE/SELECT + 25 demo cases into Lakebase.

    python3 -m tests.e2e.seed <in_process|job|job_warehouse>

The seeder OWNS the demo/ bundle, so its job runs AS THE SEEDER with no run_as overlay, and the bundle builds
its OWN wheel (lib/ + demo_substrate) — no dependency on the product bundle's wheel or artifacts path. The
seeder's UC CREATE grants (00_admin_prereqs) + Lakebase grants (03b, by the deployer/owner) are already in
place. Verification reads Lakebase as the deployer/owner (which can always read the tables it created).
"""
import shutil
import sys
import time
from pathlib import Path

from tests.harness import config, dbx, report
from tests.harness.pgstate import PgState

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = str(REPO_ROOT / "demo")


def _demo_vars(cfg) -> list[str]:
    """--var flags the demo bundle needs (same names as the product bundle; all non-secret, all from config)."""
    pairs = {
        "catalog": cfg.require("CATALOG"),
        "schema": cfg.require("SCHEMA"),
        "role_group": cfg.require("ROLE_GROUP"),
        "lakebase_project": cfg.require("LAKEBASE_PROJECT"),
        "lakebase_branch": cfg.get("LAKEBASE_BRANCH", "production"),
        "lakebase_endpoint": cfg.get("LAKEBASE_ENDPOINT", "primary"),
        "pg_database": cfg.require("PG_DATABASE"),
        # Isolate this suffix's prod demo-bundle root (empty → default root; only affects the prod target,
        # stage is dev-mode per-user). Mirrors the product bundle so back-to-back cicd rows don't contend.
        "deploy_suffix": (f"-{cfg.get('TEST_SUFFIX')}" if cfg.get("TEST_SUFFIX") else ""),
    }
    args: list[str] = []
    for k, v in pairs.items():
        args += ["--var", f"{k}={v}"]
    return args


def _demo_bundle(seeder: str, *args: str, target: str, what: str, clear_local=None) -> None:
    """Run a demo/-bundle command as the seeder, retrying transient control-plane failures. The sandbox plane
    intermittently 504s (`workspace-files/import-file` stream timeout) or times out `workspace/get-status`
    during a bundle deploy/run — the same flaky surface the product deploy retries (tests/e2e/deploy.py). Bundle
    deploy + run are idempotent, so a bounded re-try clears a one-off blip. If `clear_local` is given (deploy),
    drop that local state-cache dir before EACH attempt so every try is a clean re-sync. Surfaces the CLI
    output and exits on the final failure (a persistent error stays loud)."""
    attempts = 3
    for i in range(1, attempts + 1):
        # Clear the local state cache ONLY before the first attempt (drop a prior run's state); KEEP it between
        # retries so terraform UPDATEs what a failed attempt created instead of trying to re-CREATE it.
        if i == 1 and clear_local is not None and clear_local.exists():
            shutil.rmtree(clear_local)
        cp = dbx.bundle(seeder, *args, target=target, cwd=DEMO_DIR, check=False)
        if cp.returncode == 0:
            return
        if i < attempts:
            # Explicit, intentional backoff (see tests/e2e/deploy.py): an immediate re-try re-hits the same
            # transient control-plane blip; space attempts out so it clears.
            backoff = 10 * i
            report.step(f"  demo bundle {what} attempt {i}/{attempts} failed (rc={cp.returncode}); "
                        f"retrying in {backoff}s (transient?)")
            time.sleep(backoff)
        else:
            sys.stderr.write(cp.stdout or "")
            sys.stderr.write(cp.stderr or "")
            sys.exit(f"demo bundle {what} failed (exit {cp.returncode})")


def main(mode: str) -> None:
    cfg = config.load()
    seeder = cfg.require("SEEDER_PROFILE")
    role = cfg.require("ROLE_GROUP")
    target = cfg.bundle_target
    var_args = _demo_vars(cfg)

    # The demo bundle's local state-cache dir is dropped before EACH deploy attempt inside _demo_bundle (a
    # fresh seeder each round → per-seeder remote root, so a leftover local dir from a PRIOR seeder mismatches
    # lineage / hangs reconciling the old root). It's just a cache — dropping it forces a clean re-sync.
    local_state = Path(DEMO_DIR) / ".databricks" / "bundle" / target

    report.step(f"deploy the demo/ bundle AS THE SEEDER ({seeder}) — builds its own wheel (lib/ + demo_substrate)")
    _demo_bundle(seeder, "deploy", *var_args, target=target, what="deploy", clear_local=local_state)

    report.step(f"run seed_demo_data AS THE SEEDER (grants role {role} on the demo tools/tables; seeds 25 cases)")
    _demo_bundle(seeder, "run", "seed_demo_data", *var_args, target=target, what="run")

    report.step("verify the seed landed (read Lakebase as the deployer/owner)")
    pg = PgState.from_config(cfg)
    r = report.Results()
    r.assert_eq("25 demo cases seeded", 25, pg.case_count())
    r.check("a known demo case exists (CASE-0001)", lambda: pg.case_status("CASE-0001") is not None)
    r.finish()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("in_process", "job", "job_warehouse"):
        sys.exit("usage: python -m tests.e2e.seed <in_process|job|job_warehouse>")
    main(sys.argv[1])
