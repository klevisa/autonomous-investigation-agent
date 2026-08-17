"""State store for cases + investigations — the operational (OLTP) tables, on Lakebase Postgres.

This is the "split" the Lakebase design calls for: the orchestrator's **state access** (load case,
open/finish/fail investigation, update case) lives here against Postgres, while **tool access** (the 5
UC functions) stays on Delta/UC via the warehouse or Spark runner. Two backends, one investigation.

Why a dedicated store instead of a generic SqlRunner: a real Postgres driver needs **parameterized**
queries — our indicators are URLs full of `?` and `%` that inline-literal SQL would mangle or expose to
injection. Every method here binds parameters. All SQL is Postgres dialect (no three-part names,
`now()` not `current_timestamp()`, JSONB casts).

Each public method opens a short-lived connection (autoscaling Lakebase resumes in ~2.4s cold, else
warm) via the injected `connect` factory (see pg.py), runs, commits, closes.
"""
import json
import uuid

from lib.pg import pg_exec, pg_query


class CaseStatus:
    """The case lifecycle vocabulary — one enumerated home for the states a case moves through, instead of
    string literals scattered across the store's UPDATEs (and matching the accent map in app/ui.py):

        new ──open──▶ investigating ──verdict──▶ investigated | escalated
                           │
                           ├──fail (retryable)──▶ new
                           └──abandon (over cap)──▶ needs_review
    """
    NEW = "new"
    INVESTIGATING = "investigating"
    INVESTIGATED = "investigated"
    ESCALATED = "escalated"
    NEEDS_REVIEW = "needs_review"
    CLOSED = "closed"


class PostgresStateStore:
    def __init__(self, connect):
        """connect: a zero-arg callable returning a fresh DB-API connection (see pg.make_pg_connect)."""
        self._connect = connect

    # --- low-level helpers (thin wrappers over the shared pg_query/pg_exec) -------------------------
    def _query(self, sql, params=()):
        return pg_query(self._connect, sql, params)

    def _exec(self, sql, params=()):
        pg_exec(self._connect, sql, params)

    def _set_case_status(self, case_id, status):
        """The single place a case's status flips (except update_case, which also writes the verdict
        columns in one statement). `status` is a CaseStatus value."""
        self._exec("UPDATE cases SET status=%s, updated_at=now() WHERE case_id=%s", (status, case_id))

    # --- case reads --------------------------------------------------------------------------------
    def load_case(self, case_id):
        # PoC: read the case from the Lakebase cases mirror.
        # In production the case content is the source of truth in Tines and could be fetched live:
        #   GET https://<tenant>.tines.com/api/v1/cases/{case_id}
        rows = self._query(
            """SELECT case_id, title, description, severity, indicator_value, indicator_type,
                      account_id, scenario_label
               FROM cases WHERE case_id = %s LIMIT 1""", (case_id,))
        return rows[0] if rows else None

    # --- investigation lifecycle -------------------------------------------------------------------
    def open_investigation(self, case_id, model_endpoint, run_ref, investigated_by):
        inv_id = "INV-" + uuid.uuid4().hex[:12]
        self._exec(
            """INSERT INTO investigations
                 (investigation_id, case_id, status, model_endpoint, job_run_id,
                  attempts, started_at, investigated_by)
               VALUES (%s, %s, 'running', %s, %s, 1, now(), %s)""",
            (inv_id, case_id, model_endpoint, run_ref, investigated_by))
        self._set_case_status(case_id, CaseStatus.INVESTIGATING)
        return inv_id

    def set_job_run_id(self, inv_id, job_run_id):
        """Record the triggering job run id — known only AFTER jobs.run_now returns, so it's a small
        update following open_investigation (which runs before the job exists)."""
        self._exec("UPDATE investigations SET job_run_id=%s WHERE investigation_id=%s",
                   (str(job_run_id), inv_id))

    # --- durability: startup-reconcile support (in_process + job modes) ----------------------------
    def running_investigations(self):
        """Every investigation still marked 'running' — the durable work queue the app reconciles on
        startup. In in_process mode a 'running' row whose runner thread died with the app is orphaned and
        must be re-run; in job mode it's reconciled against its job_run_id. Newest first is irrelevant to
        correctness but keeps reconcile output readable."""
        return self._query(
            """SELECT investigation_id, case_id, job_run_id, model_endpoint,
                      COALESCE(attempts, 0) AS attempts, started_at
               FROM investigations WHERE status='running'
               ORDER BY started_at ASC NULLS FIRST""")

    def bump_attempts(self, inv_id):
        """Increment the attempt counter (and restamp started_at) as a row is (re)run, returning the NEW
        count so the caller can enforce the cap. Atomic so a re-run is counted even if the app dies again
        mid-run — an orphaned row always reflects how many times it's actually been started."""
        rows = self._query(
            """UPDATE investigations SET attempts = COALESCE(attempts, 0) + 1, started_at = now()
               WHERE investigation_id=%s RETURNING attempts""", (inv_id,))
        return int(rows[0]["attempts"]) if rows else 0

    def record_verdict(self, inv_id, case_id, verdict):
        """THE completion contract — the single authoritative path that persists a verdict + rolls up the
        case. "Investigation as a service": the orchestrator (this app + Lakebase) owns state and exposes
        exactly this one write; the investigation is pluggable compute (in-process thread, a job, a future
        endpoint) that produces a verdict and hands it back HERE. Both modes reach this ONE method, and the
        APP is the only process that ever calls it:
          * in_process  → app/backend calls it directly from the worker thread,
          * job         → the job appends a 'completed' event (with the verdict) to the append-only journal;
                          the app's reconcile reads that event and calls this method (see lib/journal.py).
                          The job itself has no access to this table — it only appends events.
        Idempotent by design (a plain overwrite keyed by inv_id) so at-least-once re-runs / replayed journal
        events are safe to repeat."""
        self._exec(
            """UPDATE investigations SET
                 status='complete', assessed_severity=%s, escalate_to_high=%s,
                 recommended_play=%s, confidence=%s, summary=%s, rationale=%s,
                 evidence=%s::jsonb, tools_called=%s::jsonb, finished_at=now()
               WHERE investigation_id=%s""",
            (verdict["assessed_severity"], bool(verdict.get("escalate_to_high")),
             verdict.get("recommended_play"), float(verdict.get("confidence") or 0.0),
             verdict.get("summary", ""), verdict.get("rationale", ""),
             json.dumps(verdict.get("evidence", {}), default=str),
             json.dumps(verdict.get("tools_called", [])), inv_id))
        self.update_case(case_id, inv_id, verdict)

    def fail_investigation(self, inv_id, case_id, error):
        self._exec(
            """UPDATE investigations SET status='failed', rationale=%s, finished_at=now()
               WHERE investigation_id=%s""", (str(error)[:500], inv_id))
        # Return the case to NEW so it can be retried.
        self._set_case_status(case_id, CaseStatus.NEW)

    def abandon_investigation(self, inv_id, case_id, reason):
        """Permanently give up on an investigation that has exceeded the attempts cap (see the reconcile
        loop). Unlike fail_investigation this does NOT return the case to 'new' — auto-retry is exactly
        what we're stopping — it flags the case 'needs_review' for a human. The row leaves 'running', so
        the startup reconcile never picks it up again."""
        self._exec(
            """UPDATE investigations SET status='failed', rationale=%s, finished_at=now()
               WHERE investigation_id=%s""", (str(reason)[:500], inv_id))
        self._set_case_status(case_id, CaseStatus.NEEDS_REVIEW)

    def update_case(self, case_id, inv_id, verdict):
        """(1) update the Lakebase cases mirror with the verdict; (2) push back to Tines (stubbed)."""
        escalate = bool(verdict.get("escalate_to_high"))
        new_status = CaseStatus.ESCALATED if escalate else CaseStatus.INVESTIGATED
        self._exec(
            """UPDATE cases SET
                 status=%s, assessed_severity=%s, escalate_to_high=%s,
                 latest_investigation_id=%s, updated_at=now()
               WHERE case_id=%s""",
            (new_status, verdict["assessed_severity"], escalate, inv_id, case_id))

        # ───────────────────────────────────────────────────────────────────────────────────────
        # TODO(AIA / Tines write-back): Tines is the system of record — push the verdict back.
        #   PATCH https://<tenant>.tines.com/api/v1/cases/{case_id}
        #   body: {priority: high|medium, status: in_review|resolved,
        #          fields: {assessed_severity, agent_summary, investigation_id}}
        # ───────────────────────────────────────────────────────────────────────────────────────
        return {"case_id": case_id, "new_status": new_status, "escalate_to_high": escalate}
