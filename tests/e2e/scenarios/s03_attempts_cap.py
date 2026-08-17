#!/usr/bin/env python3
"""SCENARIO S03 (in_process) — the attempts CAP: a crash-looping investigation must NOT be re-run forever.
When an orphan's attempts have already reached MAX_ATTEMPTS (default 3), startup reconcile must ABANDON it —
mark the investigation 'failed' and park the CASE as 'needs_review' (not re-run, not 'new').

We inject an orphan already AT the cap, restart, and assert reconcile abandons it rather than launching.

    python3 -m tests.e2e.scenarios.s03_attempts_cap
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
    case = os.environ.get("CASE_ID", "CASE-0003")
    inv = f"INV-capped-{int(time.time())}"
    cap = int(os.environ.get("AIA_MAX_ATTEMPTS", "3"))
    pg = PgState.from_config(cfg)
    app = AppClient(deployer, app_name)
    r = report.Results()

    report.step(f"S03: inject an orphan already AT the attempts cap ({cap}) for {case}")
    pg.execute(
        "INSERT INTO investigations (investigation_id, case_id, status, model_endpoint, attempts, started_at) "
        f"VALUES ('{inv}','{case}','running','aia-app-in-process', {cap}, now()-interval '1 hour')")
    pg.execute(f"UPDATE cases SET status='investigating' WHERE case_id='{case}'")
    r.assert_eq("orphan running at cap before restart", "running", pg.inv_status(inv))
    r.assert_eq("attempts at cap", cap, pg.inv_attempts(inv))

    report.step("restart the app → startup reconcile runs")
    restart()            # strategy-aware (injected): dev = bundle deploy+run; cicd = a real CI redeploy
    app.wait_healthy()

    report.step("assert reconcile ABANDONED the capped orphan (not re-run)")
    waiters.wait_until("orphan leaves 'running'", 180, lambda: pg.inv_status(inv) != "running")
    r.assert_eq("capped investigation marked failed (not complete/running)", "failed", pg.inv_status(inv))
    r.assert_eq("case parked as needs_review (auto-retry stopped)", "needs_review", pg.case_status(case))
    r.assert_eq("attempts unchanged (no re-run)", cap, pg.inv_attempts(inv))
    r.finish()


if __name__ == "__main__":
    main("in_process", applifecycle.make_restart(config.load()))
