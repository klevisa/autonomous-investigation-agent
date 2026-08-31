#!/usr/bin/env python3
"""SCENARIO S01 — happy-path investigation end-to-end (both modes). Kicks a real case via the app API,
waits for completion, and asserts the verdict + case rollup landed, and that a TOOL query ran AS THE ROLE
(the whole point of the RBAC model).

    python3 -m tests.e2e.scenarios.s01_happy_investigation <in_process|job|job_warehouse>
"""
import os
import sys

from tests.harness import applifecycle, config, report, waiters
from tests.harness.appapi import AppClient
from tests.harness.pgstate import PgState


def main(mode: str, restart) -> None:
    cfg = config.load()
    case = os.environ.get("CASE_ID", "CASE-0001")
    app = AppClient(cfg.require("DEPLOYER_PROFILE"), cfg.require("APP_NAME"))
    pg = PgState.from_config(cfg)
    r = report.Results()

    report.step(f"S01 ({mode}): trigger an investigation for {case} via the app API")
    resp = app.post("/api/investigations", f'{{"case_id":"{case}"}}')
    print(f"  response: {resp}")
    inv = resp.get("investigation_id", "")
    r.assert_eq("trigger returned investigation_started", "investigation_started", resp.get("status"))
    r.assert_eq("mode", mode, resp.get("mode"))
    if not inv:
        print("no investigation_id — aborting")
        r.finish()

    report.step("wait for the investigation to complete (job mode also spins up a Spark job)")
    timeout = int(os.environ.get("S01_TIMEOUT", "600"))
    waiters.wait_equals(f"investigation {inv} complete", timeout, lambda: pg.inv_status(inv), "complete")
    r.assert_eq("investigation status", "complete", pg.inv_status(inv))

    report.step("assert the verdict + case rollup persisted")
    sev = pg.inv_field(inv, "assessed_severity")
    print(f"  assessed_severity: {sev}")
    r.check("assessed_severity present", sev in ("low", "medium", "high"))
    cs = pg.case_status(case)
    r.check("case rolled up (investigated|escalated)", cs in ("investigated", "escalated"))
    r.assert_eq("case.latest points at this investigation", inv,
                pg._scalar(f"SELECT latest_investigation_id FROM cases WHERE case_id='{case}'",
                           "latest_investigation_id"))

    report.step("assert tools ran (evidence trail non-empty ⇒ the member SP's warehouse query worked)")
    n = pg._scalar(
        f"SELECT jsonb_array_length(COALESCE(tools_called,'[]'::jsonb)) n "
        f"FROM investigations WHERE investigation_id='{inv}'", "n")
    r.check("at least one tool was called (member-SP warehouse query succeeded)", (n or 0) >= 1)
    print("  (model_endpoint tags the runner: aia-app-in-process | aia-investigate-job)")

    # Audit trail: the RBAC model's whole point is that a MEMBER SP ran this. investigated_by is stamped with
    # the runner's real identity (the app SP for in_process, the job SP for job) — only the bare "app"/"job"
    # fallback strings mean identity resolution FAILED. Assert a real identity was recorded.
    who = pg.inv_field(inv, "investigated_by")
    print(f"  investigated_by: {who}")
    r.check("investigation stamped with a resolved runner identity (not the fallback)",
            bool(who) and who not in ("app", "job"))
    r.finish()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("in_process", "job", "job_warehouse"):
        sys.exit("usage: python -m tests.e2e.scenarios.s01_happy_investigation <in_process|job|job_warehouse>")
    main(sys.argv[1], applifecycle.make_restart(config.load()))
