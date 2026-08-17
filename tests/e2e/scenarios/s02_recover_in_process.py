#!/usr/bin/env python3
"""SCENARIO S02 (in_process only) — RECOVERY: the app stops while an investigation is running; on restart
the startup reconcile must RE-RUN the orphaned row, drive it to complete, and bump attempts.

We inject a GUARANTEED orphan (a 'running' row for a real seeded case + case 'investigating', attempts=1 —
exactly what open_investigation leaves if the worker thread died), restart the app, and assert reconcile
picks it up.

    python3 -m tests.e2e.scenarios.s02_recover_in_process
"""
import os
import time

from tests.harness import applifecycle, config, report, waiters
from tests.harness.appapi import AppClient
from tests.harness.pgstate import PgState


def main(mode: str, restart) -> None:
    cfg = config.load()
    deployer = cfg.require("DEPLOYER_PROFILE")
    app_name = cfg.require("APP_NAME")
    case = os.environ.get("CASE_ID", "CASE-0002")
    inv = f"INV-orphan-{int(time.time())}"
    pg = PgState.from_config(cfg)
    app = AppClient(deployer, app_name)
    r = report.Results()

    report.step(f"S02: inject a guaranteed orphaned 'running' investigation ({inv} for {case})")
    pg.execute(
        "INSERT INTO investigations (investigation_id, case_id, status, model_endpoint, attempts, started_at) "
        f"VALUES ('{inv}','{case}','running','aia-app-in-process',1, now()-interval '1 hour')")
    pg.execute(f"UPDATE cases SET status='investigating' WHERE case_id='{case}'")
    r.assert_eq("orphan is running before restart", "running", pg.inv_status(inv))
    r.assert_eq("attempts starts at 1", 1, pg.inv_attempts(inv))

    report.step("restart the app (simulates the process that owned the thread dying + coming back)")
    restart()            # strategy-aware (injected): dev = bundle deploy+run; cicd = a real CI redeploy
    app.wait_healthy()   # poll-until-serving (HTTP 200), so the lifespan startup (reconcile) has run

    report.step("assert startup reconcile RE-RAN the orphan → it completes, attempts incremented")
    timeout = int(os.environ.get("S02_TIMEOUT", "600"))
    waiters.wait_equals(f"orphan {inv} complete", timeout, lambda: pg.inv_status(inv), "complete")
    r.assert_eq("orphan reconciled to complete", "complete", pg.inv_status(inv))
    r.check("attempts incremented past 1 (re-run counted)", (pg.inv_attempts(inv) or 0) >= 2)
    r.check("case rolled up after recovery", pg.case_status(case) in ("investigated", "escalated"))
    r.finish()


if __name__ == "__main__":
    main("in_process", applifecycle.make_restart(config.load()))
