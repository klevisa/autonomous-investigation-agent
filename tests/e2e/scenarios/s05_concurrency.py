#!/usr/bin/env python3
"""SCENARIO S05 — concurrency: fire N investigations back-to-back and assert they ALL complete. In
in_process this exercises the app's background-thread pool; in job mode it's N concurrent job runs. Catches
deadlocks / shared-state clobbering / connection exhaustion.

    python3 -m tests.e2e.scenarios.s05_concurrency <in_process|job> [N]
"""
import os
import sys

from tests.harness import applifecycle, config, report, waiters
from tests.harness.appapi import AppClient
from tests.harness.pgstate import PgState

# distinct seeded cases so rollups don't collide
CASES = ["CASE-0005", "CASE-0006", "CASE-0007", "CASE-0008",
         "CASE-0009", "CASE-0010", "CASE-0011", "CASE-0012"]


def main(mode: str, restart, n: int = 5) -> None:
    cfg = config.load()
    app = AppClient(cfg.require("DEPLOYER_PROFILE"), cfg.require("APP_NAME"))
    pg = PgState.from_config(cfg)
    r = report.Results()

    report.step(f"S05 ({mode}): fire {n} investigations back-to-back")
    invs = []
    for i in range(n):
        case = CASES[i]
        resp = app.post("/api/investigations", f'{{"case_id":"{case}"}}')
        iv = resp.get("investigation_id", "")
        # surface the full response when there's no id (e.g. HTTP 503 saturation / error body) so a failure
        # is diagnosable rather than showing an empty arrow.
        print(f"  {case} → {iv if iv else resp}")
        invs.append(iv)

    report.step(f"wait for all {n} to complete")
    timeout = int(os.environ.get("S05_TIMEOUT", "900"))
    ok = 0
    for iv in invs:
        if iv and waiters.wait_equals(f"{iv} complete", timeout, lambda iv=iv: pg.inv_status(iv), "complete"):
            ok += 1
        else:
            print(f"  {iv} did not complete")
    r.assert_eq("all investigations completed", n, ok)
    r.finish()


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("in_process", "job", "job_warehouse"):
        sys.exit("usage: python -m tests.e2e.scenarios.s05_concurrency <in_process|job> [N]")
    main(sys.argv[1], applifecycle.make_restart(config.load()),
         int(sys.argv[2]) if len(sys.argv) > 2 else 5)
