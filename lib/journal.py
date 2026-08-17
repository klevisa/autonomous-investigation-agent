"""The investigation JOURNAL — an append-only handoff between the investigate JOB and the APP (job mode).

THE SHAPE  — two tables, one direction:
    app  open_investigation   -> investigations INSERT (running)  + journal 'dispatched'
    job  at start             -> journal 'started'
    job  at end               -> journal 'completed' (full verdict) | 'failed'
    app  reconcile            -> reads pending events, applies via record_verdict, stamps applied_at

TRUST BOUNDARY — the job holds INSERT on `investigation_events` and NOTHING else: no SELECT on the journal,
no access at all to `cases`/`investigations`. It cannot read case data, mutate state, or even see its own
events. The app remains the sole owner of state; the journal is an *outbox*, not state.

AUTHENTICITY — `job_run_id` is the binding. The job stamps every event with `{{job.run_id}}`, which the
Databricks platform injects and a job cannot forge, and the app already recorded that run id at
`jobs.run_now` (state_store.set_job_run_id). The app applies an event ONLY IF its job_run_id matches the one
it dispatched for that investigation_id — so a job SP cannot append a verdict for someone else's
investigation. Nothing is minted and nothing is stored: no client secret, no bearer token, no shared HMAC.
(`investigation_id` is already uuid4-derived, so it's unguessable; the run-id match adds *authenticity* on
top of that unguessability.)

Both sides import this one module, so the event vocabulary lives in exactly one place.
"""
import json

from lib.pg import pg_exec, pg_query

# The event vocabulary. 'dispatched' is written by the APP (it knows it fired the job); the rest by the JOB.
DISPATCHED, STARTED, COMPLETED, FAILED = "dispatched", "started", "completed", "failed"
TERMINAL = (COMPLETED, FAILED)


# ── the JOB side: append only (this is the job's ENTIRE database surface) ────────────────────────────
def append_event(connect, investigation_id, event_type, job_run_id, case_id=None,
                 verdict=None, detail=None):
    """Append one event. The only write the investigate job is permitted to make.

    verdict is stored as JSONB — `default=str` on the dump because the verdict carries values pulled
    straight from Spark SQL rows (datetime/Decimal), which plain json.dumps rejects. Stringifying for
    transport is fine: these are display/audit fields, and the app re-parses the JSON on read.
    """
    pg_exec(connect,
            """INSERT INTO investigation_events
                 (investigation_id, case_id, event_type, job_run_id, verdict, detail)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s)""",
            (investigation_id, case_id, event_type, str(job_run_id) if job_run_id else None,
             json.dumps(verdict, default=str) if verdict is not None else None,
             str(detail)[:2000] if detail is not None else None))


# ── the APP side: read pending events, then mark them applied ────────────────────────────────────────
def pending_terminal_events(connect):
    """Terminal ('completed'/'failed') events not yet folded into `investigations`.

    Joined to `investigations` so the run-id check happens IN SQL: `e.job_run_id = i.job_run_id` is the
    authenticity binding (see the module docstring). An event whose run id doesn't match the dispatched one
    simply never selects — it can't affect state. Only rows still 'running' are considered, so a verdict
    can't re-open a finished investigation (replay-safe).
    """
    return pg_query(connect,
                    """SELECT e.event_id, e.investigation_id, e.case_id, e.event_type,
                              e.verdict, e.detail, e.job_run_id
                       FROM investigation_events e
                       JOIN investigations i ON i.investigation_id = e.investigation_id
                       WHERE e.applied_at IS NULL
                         AND e.event_type IN (%s, %s)
                         AND i.status = 'running'
                         AND i.job_run_id IS NOT NULL
                         AND e.job_run_id = i.job_run_id
                       ORDER BY e.event_id""", (COMPLETED, FAILED))


def mark_applied(connect, event_id):
    """Stamp one event as reconciled. Separate from the state write on purpose: record_verdict is an
    idempotent upsert, so a crash between applying and stamping just replays harmlessly next sweep."""
    pg_exec(connect, "UPDATE investigation_events SET applied_at=now() WHERE event_id=%s", (event_id,))


def last_event_type(connect, investigation_id):
    """The most recent event for an investigation, or None. Lets startup reconcile tell apart
    'dispatched but never got compute' from 'started then died mid-run' — a distinction the single
    `status='running'` could not express, and a free by-product of journalling."""
    rows = pg_query(connect,
                    """SELECT event_type FROM investigation_events
                       WHERE investigation_id=%s ORDER BY event_id DESC LIMIT 1""", (investigation_id,))
    return rows[0]["event_type"] if rows else None
