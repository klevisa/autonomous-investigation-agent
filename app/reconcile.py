"""The startup-reconcile DECISION — pure, so the densest logic in the app is table-testable.

Reconcile answers one question per orphaned 'running' row: what do we do with it? The ANSWER depends on
several signals (attempts vs the cap, whether the case still exists, and — in job mode — whether the job
is still executing and whether it ever actually started).

This module holds ONLY the decision: gather the signals in the caller (app/investigations.py), call
`decide(...)`, then act on the returned Action. No I/O here, so the whole matrix is unit-tested.

The actions:
  ABANDON            — give up permanently (over the attempts cap, or the case disappeared). Flags the case
                       'needs_review'; the row leaves 'running' so reconcile never sees it again.
  REQUEUE_COUNTED    — re-run AND count it against the attempts cap (real work was attempted, then lost —
                       the crash-loop the cap exists to bound).
  REQUEUE_UNCOUNTED  — re-run but DON'T count it (the job never got compute — queue backlog / cancelled
                       before start — so nothing was consumed and the cap shouldn't be burned).
  LEAVE              — do nothing this sweep (the job is still executing, or the Jobs API errored
                       transiently and the run may well be alive; a later sweep re-checks).
"""

# Actions
ABANDON = "abandon"
REQUEUE_COUNTED = "requeue_counted"
REQUEUE_UNCOUNTED = "requeue_uncounted"
LEAVE = "leave"

# Job execution state (job mode only), as classified from the Jobs API by the caller.
JOB_RUNNING = "running"        # PENDING/RUNNING/BLOCKED/QUEUED/TERMINATING — still executing
JOB_GONE = "gone"              # the run genuinely no longer exists (NotFound / auto-removed after 60d)
JOB_TRANSIENT = "transient"    # the get_run call errored transiently — the run may still be alive
JOB_NO_RUN_ID = "no_run_id"    # no run id was ever recorded (nothing to check; safe to re-fire)


def decide(mode, attempts, max_attempts, case_exists, job_state=None, last_event=None):
    """Return the Action for one orphaned 'running' row. Pure.

      mode:         "in_process" | "job"  (job_warehouse is a "job" from reconcile's point of view)
      attempts:     the row's current attempt count
      max_attempts: the cap
      case_exists:  is the case still loadable? (can't re-run a vanished case)
      job_state:    JOB_* — only consulted in job mode
      last_event:   the most recent journal event type (None | 'dispatched' | 'started' | ...), job mode
    """
    # 1. Over the cap → abandon, regardless of mode. (A crash-looper must not re-run forever.)
    if attempts >= max_attempts:
        return ABANDON

    # 2. in_process: the worker thread died with the old process, so the row is provably orphaned. Re-run
    #    it and count the attempt — unless the case itself is gone, in which case there's nothing to run.
    if mode == "in_process":
        return REQUEUE_COUNTED if case_exists else ABANDON

    # 3. job mode. If the run is still executing (or we couldn't tell), leave it — its terminal journal
    #    event will land and be applied by the poll; re-firing now would double-run a live investigation.
    if job_state in (JOB_RUNNING, JOB_TRANSIENT):
        return LEAVE
    # The run is gone (or was never recorded) → we will re-fire, if the case still exists.
    if not case_exists:
        return ABANDON
    # 'started' proves real work was attempted → count it. No 'started' (never dispatched past the queue)
    # means no compute was consumed → re-fire without burning an attempt.
    never_started = last_event in (None, "dispatched")
    return REQUEUE_UNCOUNTED if never_started else REQUEUE_COUNTED
