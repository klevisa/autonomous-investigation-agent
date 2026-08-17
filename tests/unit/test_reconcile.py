"""Tier 1 — the reconcile decision matrix (app/reconcile.decide). Pure, exhaustive.

This is the densest logic in the app; the table below IS the spec for what startup reconcile does with an
orphaned 'running' row.
"""
import pytest

from app import reconcile as rc


# (mode, attempts, max_attempts, case_exists, job_state, last_event) -> expected action
CASES = [
    # --- the cap wins in both modes, whatever else is true -----------------------------------------
    ("in_process", 3, 3, True,  None,          None,          rc.ABANDON),
    ("job",        3, 3, True,  rc.JOB_GONE,   "started",     rc.ABANDON),
    ("job",        5, 3, True,  rc.JOB_RUNNING, "started",    rc.ABANDON),

    # --- in_process: orphaned thread -> re-run + count, unless the case vanished -------------------
    ("in_process", 1, 3, True,  None,          None,          rc.REQUEUE_COUNTED),
    ("in_process", 2, 3, False, None,          None,          rc.ABANDON),

    # --- job: still executing (or unknown) -> leave it for the poll --------------------------------
    ("job",        1, 3, True,  rc.JOB_RUNNING,   "started",  rc.LEAVE),
    ("job",        1, 3, True,  rc.JOB_TRANSIENT, "started",  rc.LEAVE),
    ("job",        1, 3, False, rc.JOB_RUNNING,   None,       rc.LEAVE),  # leave beats the missing-case check

    # --- job: run gone / never recorded -> re-fire; count only if it actually started --------------
    ("job",        1, 3, True,  rc.JOB_GONE,      "started",  rc.REQUEUE_COUNTED),
    ("job",        1, 3, True,  rc.JOB_GONE,      "dispatched", rc.REQUEUE_UNCOUNTED),
    ("job",        1, 3, True,  rc.JOB_GONE,      None,       rc.REQUEUE_UNCOUNTED),
    ("job",        1, 3, True,  rc.JOB_NO_RUN_ID, None,       rc.REQUEUE_UNCOUNTED),
    ("job",        1, 3, True,  rc.JOB_NO_RUN_ID, "started",  rc.REQUEUE_COUNTED),
    # gone but the case disappeared -> abandon
    ("job",        1, 3, False, rc.JOB_GONE,      "started",  rc.ABANDON),
]


@pytest.mark.parametrize("mode,attempts,cap,case_exists,job_state,last_event,expected", CASES)
def test_decide(mode, attempts, cap, case_exists, job_state, last_event, expected):
    assert rc.decide(mode, attempts, cap, case_exists, job_state, last_event) == expected


def test_attempts_at_cap_boundary_abandons():
    # cap is inclusive: attempts == max_attempts abandons (open counts as attempt 1)
    assert rc.decide("in_process", 3, 3, True) == rc.ABANDON
    assert rc.decide("in_process", 2, 3, True) == rc.REQUEUE_COUNTED
