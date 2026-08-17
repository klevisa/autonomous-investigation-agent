#!/usr/bin/env python3
"""SCENARIO S04 (job mode only) — RECOVERY of orphaned job-mode investigations via the JOURNAL.

Job mode's verdict comes back not by HTTP callback but by the job APPENDING events to an append-only journal
(investigation_events); the app reconciles them into `investigations`. There are exactly FIVE situations an
app restart must handle. We inject each as a deterministic orphan (a 'running' row + the journal events that
situation would have), do ONE restart, and assert the reconcile outcome. The authenticity binding under test
is journal.pending_terminal_events' SQL join: an event applies ONLY IF its job_run_id matches the run id the
app recorded for that investigation.

  (1) MISSED EVENTS   completed event present, row still 'running' → APPLY the verdict.
  (2) NEVER STARTED   only 'dispatched' (no compute)              → RE-FIRE, do NOT bump attempts.
  (3) DIED MID-RUN    'started', no terminal                      → RE-FIRE and bump attempts.
  (4) CAP REACHED     row already at the attempts cap             → ABANDON (no re-fire).
  (5) FORGED / REPLAY forged completed event on a NORMAL orphan   → rejected + inert; row completes via its
                                                                     OWN authentic re-fire.

    python3 -m tests.e2e.scenarios.s04_recover_job
"""
import json
import os
import sys
import time

from tests.harness import applifecycle, config, report, waiters
from tests.harness.appapi import AppClient
from tests.harness.pgstate import PgState

# A well-formed verdict for the authentic completed event (record_verdict needs assessed_severity).
V_OK = json.dumps({
    "assessed_severity": "medium", "escalate_to_high": False, "recommended_play": "monitor",
    "confidence": 0.71, "summary": "S04 missed-event recovery",
    "rationale": "applied from journal on restart", "evidence": {"src": "s04"},
    "tools_called": ["get_account_risk"]})
# A verdict that would be LOUD if it ever wrongly applied (high + escalate). It must NEVER land (forged run id).
V_FORGED = json.dumps({
    "assessed_severity": "high", "escalate_to_high": True, "recommended_play": "escalate",
    "confidence": 0.99, "summary": "FORGED-SHOULD-NOT-APPLY",
    "rationale": "forged job_run_id — must be dropped by the authenticity join",
    "evidence": {}, "tools_called": []})


def main(mode: str, restart) -> None:
    cfg = config.load()
    deployer = cfg.require("DEPLOYER_PROFILE")
    app_name = cfg.require("APP_NAME")
    cap = int(os.environ.get("AIA_MAX_ATTEMPTS", "3"))
    ts = int(time.time())
    pg = PgState.from_config(cfg)
    app = AppClient(deployer, app_name)
    r = report.Results()

    def ev_unapplied(inv: str) -> bool:
        """True iff a completed event for inv is still unapplied (applied_at IS NULL)."""
        n = pg._scalar(
            f"SELECT count(*) c FROM investigation_events "
            f"WHERE investigation_id='{inv}' AND event_type='completed' AND applied_at IS NULL", "c")
        return int(n or 0) != 0

    # 5 real seeded cases no other job-mode scenario touches (s01 uses 0001; s05 uses 0005..0009).
    rows = pg.query(
        "SELECT case_id FROM cases WHERE case_id NOT IN "
        "('CASE-0001','CASE-0005','CASE-0006','CASE-0007','CASE-0008','CASE-0009') "
        "ORDER BY case_id LIMIT 5")
    cases = [row["case_id"] for row in rows]
    if len(cases) != 5:
        sys.exit(f"need 5 free seeded cases, got {len(cases)} — seed more cases")
    c1, c2, c3, c4, c5 = cases
    print(f"  cases: (1){c1} (2){c2} (3){c3} (4){c4} (5){c5}")

    # Synthetic run ids must be VALID int64 (jobs.get_run casts to int): the app's reconcile only re-fires
    # on a clean NotFound from the Jobs API. `<ts><n>` is ~11 digits — unique, obviously fake, unresolvable
    # → NotFound → re-fire. (An earlier `90000000000000<ts>` was 24 digits, overflowed int64, and the Jobs
    # API saw "Run 0", which broke the reconcile sweep — hence orphans never re-fired.)
    inv1, run1 = f"INV-missed-{ts}", f"{ts}0"     # (1) authentic completed event
    inv2, run2 = f"INV-nostart-{ts}", f"{ts}1"    # (2) dispatched only
    inv3, run3 = f"INV-died-{ts}", f"{ts}2"       # (3) started, no terminal
    inv4, run4 = f"INV-capped-{ts}", f"{ts}3"     # (4) capped row
    inv5, run5 = f"INV-forged-{ts}", f"{ts}4"     # (5) normal orphan's real (unresolvable) run id
    run5_forged = f"{ts}9"                         #     forged event's mismatched run id

    report.step("S04.setup: reset the 5 cases to 'new' and inject the five orphan situations")
    for c in cases:
        pg.execute(f"UPDATE cases SET status='new', assessed_severity=NULL, escalate_to_high=NULL, "
                   f"latest_investigation_id=NULL WHERE case_id='{c}'")

    def inject_inv(inv, case, run, attempts):
        pg.execute(
            "INSERT INTO investigations (investigation_id, case_id, status, model_endpoint, job_run_id, "
            f"attempts, started_at) VALUES ('{inv}','{case}','running','aia-investigate-job','{run}',"
            f"{attempts}, now()-interval '1 hour')")

    def inject_event(inv, case, etype, run, verdict=None):
        if verdict is None:
            pg.execute(
                "INSERT INTO investigation_events (investigation_id, case_id, event_type, job_run_id, "
                f"created_at) VALUES ('{inv}','{case}','{etype}','{run}', now())")
        else:
            pg.execute(
                "INSERT INTO investigation_events (investigation_id, case_id, event_type, job_run_id, "
                "verdict, created_at) VALUES (%s,%s,%s,%s, %s::jsonb, now())",
                (inv, case, etype, run, verdict))

    # (1) MISSED EVENTS — job finished (authentic completed event) while the app was down; row still running.
    inject_inv(inv1, c1, run1, 1)
    inject_event(inv1, c1, "completed", run1, V_OK)
    # (2) NEVER STARTED — dispatched but never got compute.
    inject_inv(inv2, c2, run2, 1)
    inject_event(inv2, c2, "dispatched", run2)
    # (3) DIED MID-RUN — dispatched + started, no terminal.
    inject_inv(inv3, c3, run3, 1)
    inject_event(inv3, c3, "dispatched", run3)
    inject_event(inv3, c3, "started", run3)
    # (4) CAP REACHED — already at the cap → reconcile must ABANDON, not re-fire.
    inject_inv(inv4, c4, run4, cap)
    inject_event(inv4, c4, "started", run4)
    # (5) FORGED / REPLAY on a NORMAL orphan — forged completed (mismatched run id) + a real died-mid-run shape.
    inject_inv(inv5, c5, run5, 1)
    inject_event(inv5, c5, "dispatched", run5)
    inject_event(inv5, c5, "started", run5)
    inject_event(inv5, c5, "completed", run5_forged, V_FORGED)

    # (1) has NO pre-restart assert — the live 10s journal poll may already be applying it. (2)-(5) are stable
    # on a live app (the poll never re-fires/abandons/accepts-forgery), so assert those are running.
    r.assert_eq("(2) injected orphan is running", "running", pg.inv_field(inv2, "status"))
    r.assert_eq("(3) injected orphan is running", "running", pg.inv_field(inv3, "status"))
    r.assert_eq("(4) injected orphan is running", "running", pg.inv_field(inv4, "status"))
    r.assert_eq("(5) injected orphan is running", "running", pg.inv_field(inv5, "status"))

    report.step("restart the app — startup reconcile sweeps the whole board")
    restart()            # strategy-aware (injected): dev = bundle deploy+run; cicd = a real CI redeploy
    app.wait_healthy()
    to = int(os.environ.get("S04_TIMEOUT", "600"))

    # ── (1) MISSED EVENTS: authentic completed event applied → row completes, verdict lands ──
    report.step("(1) MISSED EVENTS — authentic completed event applied on restart")
    waiters.wait_equals(f"(1) {inv1} complete", to, lambda: pg.inv_field(inv1, "status"), "complete")
    r.assert_eq("(1) row reconciled to complete", "complete", pg.inv_field(inv1, "status"))
    r.assert_eq("(1) verdict from the event landed", "medium", pg.inv_field(inv1, "assessed_severity"))
    r.check("(1) completed event marked applied", not ev_unapplied(inv1))
    r.check("(1) case rolled up", pg.case_status(c1) in ("investigated", "escalated"))

    # ── (2) NEVER STARTED: re-fired WITHOUT bumping attempts ──
    report.step("(2) NEVER STARTED — re-fired, attempts NOT bumped")
    waiters.wait_until("(2) re-fired (new job_run_id)", 180, lambda: pg.inv_field(inv2, "job_run_id") != run2)
    r.assert_eq("(2) attempts still 1 (dispatched-only re-fire is free)", 1, pg.inv_field(inv2, "attempts"))
    waiters.wait_equals(f"(2) {inv2} complete", to, lambda: pg.inv_field(inv2, "status"), "complete")
    r.assert_eq("(2) re-fired job completed", "complete", pg.inv_field(inv2, "status"))
    r.assert_eq("(2) attempts still 1 after completion", 1, pg.inv_field(inv2, "attempts"))

    # ── (3) DIED MID-RUN: re-fired AND attempts bumped ──
    report.step("(3) DIED MID-RUN — re-fired, attempts bumped to 2")
    waiters.wait_until("(3) re-fired (new job_run_id)", 180, lambda: pg.inv_field(inv3, "job_run_id") != run3)
    r.assert_eq("(3) attempts bumped to 2 (started-then-died counts)", 2, pg.inv_field(inv3, "attempts"))
    waiters.wait_equals(f"(3) {inv3} complete", to, lambda: pg.inv_field(inv3, "status"), "complete")
    r.assert_eq("(3) re-fired job completed", "complete", pg.inv_field(inv3, "status"))

    # ── (4) CAP REACHED: reconcile abandons the capped row ──
    report.step("(4) CAP REACHED — capped orphan abandoned, not re-fired")
    waiters.wait_until("(4) capped orphan leaves 'running'", 180, lambda: pg.inv_field(inv4, "status") != "running")
    r.assert_eq("(4) capped orphan abandoned (failed, not re-fired)", "failed", pg.inv_field(inv4, "status"))
    r.assert_eq("(4) attempts unchanged (abandon doesn't bump)", cap, pg.inv_field(inv4, "attempts"))
    r.assert_eq("(4) case parked needs_review (auto-retry stopped)", "needs_review", pg.case_status(c4))

    # ── (5) FORGED / REPLAY: forged event inert; row completes via its OWN re-fire ──
    report.step("(5) FORGED / REPLAY — forged event rejected; row recovers via its own authentic re-fire")
    waiters.wait_until("(5) re-fired (new job_run_id)", 180, lambda: pg.inv_field(inv5, "job_run_id") != run5)
    r.check("(5) forged completed event still UNAPPLIED after re-fire", ev_unapplied(inv5))
    waiters.wait_equals(f"(5) {inv5} complete", to, lambda: pg.inv_field(inv5, "status"), "complete")
    r.assert_eq("(5) row completed by its OWN authentic re-fire", "complete", pg.inv_field(inv5, "status"))
    r.check("(5) forged verdict never landed (summary is not the forgery)",
            pg.inv_field(inv5, "summary") != "FORGED-SHOULD-NOT-APPLY")
    r.check("(5) case rolled up from the real verdict", pg.case_status(c5) in ("investigated", "escalated"))
    r.finish()


if __name__ == "__main__":
    main("job", applifecycle.make_restart(config.load()))
