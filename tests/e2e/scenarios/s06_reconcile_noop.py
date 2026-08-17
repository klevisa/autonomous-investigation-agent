#!/usr/bin/env python3
"""SCENARIO S06 — reconcile is a safe no-op when there's nothing to recover: with no 'running' rows, an app
restart must leave all terminal rows untouched and the app healthy. Guards against reconcile clobbering
completed/failed investigations or crashing startup.

    python3 -m tests.e2e.scenarios.s06_reconcile_noop
"""
import json

from tests.harness import applifecycle, config, report
from tests.harness.appapi import AppClient
from tests.harness.pgstate import PgState


def main(mode: str, restart) -> None:
    cfg = config.load()
    deployer = cfg.require("DEPLOYER_PROFILE")
    app_name = cfg.require("APP_NAME")
    pg = PgState.from_config(cfg)
    app = AppClient(deployer, app_name)
    r = report.Results()

    report.step("S06: snapshot terminal state, ensure NO 'running' rows exist")
    running = pg._scalar("SELECT count(*) c FROM investigations WHERE status='running'", "c")
    r.assert_eq("no running investigations before restart", 0, int(running or 0))
    before = int(pg._scalar("SELECT count(*) c FROM investigations WHERE status='complete'", "c") or 0)
    print(f"  complete rows before: {before}")

    report.step("restart the app (startup reconcile runs against an empty queue)")
    restart()            # strategy-aware (injected): dev = bundle deploy+run; cicd = a real CI redeploy
    app.wait_healthy()

    report.step("assert nothing changed + app is healthy")
    # Hit a REAL app route (/api/cases returns case JSON) — the platform intercepts /healthz with an empty
    # 200 body, so a body check there would always fail. api_get already gated on the platform 200 via
    # wait_healthy; this confirms the APP itself serves data (reconcile didn't break it).
    cases = app.get("/api/cases")
    r.check("app serves case data after restart", "case_id" in json.dumps(cases))
    after = int(pg._scalar("SELECT count(*) c FROM investigations WHERE status='complete'", "c") or 0)
    r.assert_eq("completed rows untouched by reconcile", before, after)
    run2 = pg._scalar("SELECT count(*) c FROM investigations WHERE status='running'", "c")
    r.assert_eq("still no running rows (reconcile didn't create work)", 0, int(run2 or 0))
    r.finish()


if __name__ == "__main__":
    main("in_process", applifecycle.make_restart(config.load()))
