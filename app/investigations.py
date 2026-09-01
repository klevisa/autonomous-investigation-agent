"""AIA app · investigations — orchestration + state access + job triggering. No HTML here.

The Lakebase version: cases/investigations are read straight from **Lakebase Postgres** (fast indexed
point reads — the reason we moved off the warehouse for the UI), while investigations are *produced* by
the async `investigate` job (jobs.run_now). The app runs as its own service principal (autonomous tier).

Postgres connection: autoscaling Lakebase isn't bindable as an app `database` resource, so we connect
via host + a minted OAuth token (pg8000, pure-Python → serverless-safe). See lib/pg.py.

Config: the app is deployed ONCE (by CI) from only the STATIC names AIA chooses up front. The three
values that don't exist until after that deploy — the app SP id, the Lakebase host/endpoint, and the
investigate job id — are resolved LAZILY on first use from those static names (see lib/resolve.py), so
there's no "fill config.yml + redeploy" second pass, AND the app comes up healthy before setup.sh has
provisioned Lakebase (data calls just render the error page until then; no restart needed afterward).

Env (set by the DAB app resource — all static, all known before deploy):
  AIA_CATALOG, AIA_SCHEMA          the Delta catalog/schema (substrate + tools)
  AIA_LAKEBASE_PROJECT              the Lakebase project id (host/endpoint are derived from it)
  AIA_LAKEBASE_BRANCH/_ENDPOINT     the Lakebase branch + endpoint names (default production/primary)
  AIA_PG_DATABASE                   the Postgres database holding cases/investigations
  AIA_INVESTIGATE_JOB_NAME          the investigate job's name (its id is resolved by name at startup)
  AIA_AGENT_MODE                    in_process (app runs tools itself) | job_warehouse (default; job SP runs
                                    tools on the warehouse) | job (job SP runs tools on its own Spark)
  AIA_WAREHOUSE_ID                  warehouse the in-process app runs the UC-function tools through
  AIA_JOB_SP                        job mode: the job SP's client id — stamped as `investigated_by` (audit)
  DATABRICKS_HOST / DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET are injected by the Apps runtime
    (the app's own SP — a MEMBER of the AIA role group — runs the in-process tools directly).
"""
import json

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound   # tell "run genuinely gone" from a transient Jobs-API error

# The app deploys with the repo root as source_code_path (see databricks.yml), so the top-level `lib/`
# package ships alongside it and uvicorn runs with the repo root as cwd — `from lib.x import` just works.
# One committed copy of lib/, no generated dir, no sync step.
from lib.pg import make_pg_connect
from lib.state_store import PostgresStateStore
from lib import resolve
from lib import journal
from app import reconcile   # the pure reconcile decision (app/reconcile.py) — table-tested
from app.config import Config

cfg = Config.from_env()   # the ONE place AIA_* env is parsed (see app/config.py); the module-level names
# below are terse aliases onto it, so call sites stay readable while cfg remains the single source of truth.
CATALOG = cfg.catalog
SCHEMA = cfg.schema
PROJECT = cfg.project
BRANCH = cfg.branch
ENDPOINT = cfg.endpoint
PG_DATABASE = cfg.pg_database
INVESTIGATE_JOB_NAME = cfg.investigate_job_name
APP_NAME = cfg.app_name

_w = WorkspaceClient()

# EVERYTHING that depends on the backing infra is resolved LAZILY (on first use), not at import — so the
# app process starts and stays healthy even before setup.sh has provisioned Lakebase + granted the app
# SP. Until then, any data call raises and the routes render the error page (see app.py's try/except);
# the moment setup.sh runs, the next request resolves and works, with NO app restart. Each resolved
# handle is cached after the first success. resolve.* still raises loudly on genuine misconfiguration.
_store = None
_investigate_job_id = None


def _get_store():
    """The PostgresStateStore, built on first use. Resolves the Lakebase host/endpoint from the project
    name and the app SP (its own OAuth client id) as the Postgres user; schema=SCHEMA sets the search_path
    so cases/investigations resolve to AIA's schema."""
    global _store
    if _store is None:
        connect = make_pg_connect(
            _w, host=resolve.pg_host(_w, PROJECT, BRANCH, ENDPOINT), database=PG_DATABASE,
            user=resolve.app_sp_id(), endpoint_path=resolve.endpoint_path(PROJECT, BRANCH, ENDPOINT),
            schema=SCHEMA)
        _store = PostgresStateStore(connect)
    return _store


def _resolve_investigate_job_id():
    global _investigate_job_id
    if _investigate_job_id is None:
        _investigate_job_id = resolve.investigate_job_id(_w, INVESTIGATE_JOB_NAME)
    return _investigate_job_id


def _query(sql, params=()):
    return _get_store()._query(sql, params)


# --- reads for the UI (straight from Lakebase — fast indexed reads) --------------------------------
def list_cases():
    return _query("""SELECT case_id, title, severity, status, account_id, indicator_value,
                            assessed_severity, escalate_to_high, latest_investigation_id, updated_at
                     FROM cases ORDER BY updated_at DESC NULLS LAST, case_id""")


def get_case(case_id):
    rows = _query("""SELECT case_id, title, description, severity, status, account_id, indicator_value,
                            indicator_type, assessed_severity, escalate_to_high,
                            latest_investigation_id, created_at, updated_at
                     FROM cases WHERE case_id = %s LIMIT 1""", (case_id,))
    return rows[0] if rows else None


def investigations_for(case_id):
    return _query("""SELECT investigation_id, status, assessed_severity, escalate_to_high,
                            recommended_play, confidence, summary, rationale, evidence, tools_called,
                            model_endpoint, job_run_id, started_at, finished_at
                     FROM investigations WHERE case_id = %s
                     ORDER BY started_at DESC NULLS LAST""", (case_id,))


def job_run_url(job_run_id):
    """The Databricks Jobs UI URL for an investigation's triggering run, or None. Only job/job_warehouse
    investigations HAVE a run (in_process runs in the app thread and stores no run id), so this returns
    None for in_process — the UI then simply omits the link. Best-effort: any resolution failure (job not
    found, Jobs API blip) yields None rather than breaking the page."""
    if not job_run_id or not IS_JOB:
        return None
    try:
        job_id = _resolve_investigate_job_id()
        host = _w.config.host.rstrip("/")
        return f"{host}/jobs/{job_id}/runs/{job_run_id}?o={_w.get_workspace_id()}"
    except Exception as e:
        print(f"[ui] could not build job run url for {job_run_id}: {e}")
        return None


def latest_investigation(case_id):
    invs = investigations_for(case_id)
    if not invs:
        return None
    inv = invs[0]
    # pg8000 returns JSONB as already-parsed Python objects; tolerate str too.
    for k in ("evidence", "tools_called"):
        v = inv.get(k)
        if isinstance(v, str):
            try:
                inv[k] = json.loads(v)
            except (TypeError, ValueError):
                inv[k] = {} if k == "evidence" else []
        elif v is None:
            inv[k] = {} if k == "evidence" else []
    return inv


def stats():
    rows = _query("SELECT status, count(*) AS n FROM cases GROUP BY status")
    by = {r["status"]: int(r["n"]) for r in rows}
    total = sum(by.values())
    return {"total": total, "new": by.get("new", 0), "investigating": by.get("investigating", 0),
            "investigated": by.get("investigated", 0), "escalated": by.get("escalated", 0),
            "closed": by.get("closed", 0)}


WAREHOUSE_ID = cfg.warehouse_id
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# WHERE INVESTIGATIONS RUN — the app is ALWAYS the orchestrator + state owner. There is ONE set of tool
# grants: a AIA-owned RBAC ROLE (an account group with the evidence SELECT / tool EXECUTE grants). BOTH
# runner identities are MEMBERS of that role and inherit its grants, so tool queries run with the member
# SP's identity — no role assumption, no minted token. AIA_AGENT_MODE picks the compute:
#   in_process — the app runs the pure investigation itself in a background thread, running the
#     UC-function TOOLs on a warehouse with its OWN SP identity (a member of the role, with warehouse
#     CAN_USE granted directly). Because it owns Lakebase it writes the verdict straight through
#     record_verdict. Durability = the 'running' rows are a queue startup reconcile re-runs.
#   job / job_warehouse (job_warehouse is the DEFAULT) — the app fires the investigate job via jobs.run_now; the job runs as the JOB SP (also a
#     MEMBER of the role, so it inherits the same tool grants). It cannot read/write state; it appends its
#     verdict to the append-only journal (investigation_events, INSERT-only), and the app reconciles that
#     event through the SAME record_verdict (see lib/journal.py). Durability = reconcile via job_run_id.
# See databricks.yml var.agent_mode + the README. Both modes reconcile orphaned 'running' rows at startup.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
AGENT_MODE = cfg.agent_mode

# The two JOB modes are identical from the app's side — same dispatch (jobs.run_now), journal reporting,
# reconcile-via-run-id, and journal poll. They differ ONLY inside the investigate notebook: `job` runs the
# tools on its own Spark session; `job_warehouse` runs them on the warehouse (WarehouseSqlRunner, no Spark).
# The app treats both the same, so every job-vs-in_process branch below tests membership in this set.
JOB_MODES = Config.JOB_MODES
IS_JOB = cfg.is_job

# In-process tools run on the warehouse with the app's OWN SP identity: the app SP is made a MEMBER of the
# AIA role group post-deploy (02_admin_postdeploy), so it inherits the evidence SELECT / tool EXECUTE grants
# and holds warehouse CAN_USE directly — no role assumption, no minted token. (The job SP gets the same by
# membership in job mode.) The app never needs the role's name at runtime — access comes purely from the
# runner SP's group membership, so the role group is not a config value the app reads.
# JOB MODE — the dedicated job SP's client id. It runs the job (run_as), inherits the tool grants by being a
# MEMBER of the role group, and holds INSERT on the journal. Used here ONLY to stamp `investigated_by` for
# audit and authenticity of the
# job's events comes from the platform-injected run id instead (see lib/journal.py). Unused in in_process.
JOB_SP = cfg.job_sp   # Config.from_env already maps the "none" in_process sentinel → ""
# Attempts cap for startup reconcile: a case that keeps crashing its runner is abandoned to 'needs_review'
# rather than re-run forever. open_investigation counts the first start as attempt 1; each reconcile bumps.
MAX_ATTEMPTS = cfg.max_attempts

# Case fields handed to the JOB as parameters — so the agent (which has NO Lakebase access) never reads
# the case; the app supplies its content. Keep in sync with what Investigator.investigate() consumes.
_CASE_PARAM_FIELDS = ("title", "description", "severity", "indicator_value", "indicator_type",
                      "account_id", "scenario_label")


def trigger_investigation(case_id):
    """Open an investigation and start it — in-process or as a job, per AIA_AGENT_MODE. The app owns ALL
    state either way: it opens the investigation row (status='running') + flips the case to 'investigating'
    HERE, then runs (in_process) or fires the job (job) and returns immediately. Many cases run at once."""
    if IS_JOB:
        return _trigger_job(case_id)
    return _trigger_inprocess(case_id)


# ── in_process mode ─────────────────────────────────────────────────────────────────────────────
def _make_investigation_deps():
    """Build the (tool_fn, llm_fn) an in-process investigation needs. Three tool-delivery variations,
    side by side, over the SAME 5 UC functions (no new capability, no changed data):
      * blast_radius / get_account_risk / get_account_actions — unchanged direct UC SQL, on the
        warehouse, run with the app's OWN SP identity (a member of the AIA role, so it inherits the
        tool grants and holds warehouse CAN_USE).
      * pivot_indicator — Databricks managed MCP; the app's ambient WorkspaceClient identity already
        has EXECUTE on the function via AIA-role membership, enforced natively per caller.
      * enrich_indicator — the custom MCP server (app/mcp_server.py) behind a UC HTTP Connection +
        MCP Service (databricks_ops/mcp_connection.py) — the door-key model (see README).
    Built per run (cheap)."""
    from lib.tools import WarehouseSqlRunner, make_tool_fn
    from lib.mcp_tools import make_mcp_tool_fn, make_mcp_clients, make_routed_tool_fn
    from lib.llm import GatewayLLM
    from lib.investigator import MAX_TOKENS
    if not WAREHOUSE_ID:
        raise RuntimeError("AIA_WAREHOUSE_ID is not set — required for in_process mode (warehouse tools).")
    sql_tool_fn = make_tool_fn(WarehouseSqlRunner(_w, WAREHOUSE_ID), CATALOG, SCHEMA)
    mcp_tool_fn = make_mcp_tool_fn(make_mcp_clients(_w, CATALOG, SCHEMA))
    tool_fn = make_routed_tool_fn(sql_tool_fn, mcp_tool_fn)
    llm = GatewayLLM()
    # no temperature: reasoning models (Claude Opus 5) reject it; the gateway defaults are fine.
    llm_fn = lambda messages, tools: llm.chat(messages, tools=tools, max_tokens=MAX_TOKENS)
    return tool_fn, llm_fn


def _run_investigation_inprocess(inv_id, case_id, case):
    """The in_process worker body — runs in a background thread (see _launch_inprocess). Runs the pure
    investigation and persists the verdict ITSELF (the app owns state, so no callback). On any error the
    investigation is failed and the case returns to 'new', so the next trigger / startup reconcile can
    retry — bounded by the attempts cap. CAVEAT: this is re-run, not resume — a restart mid-run redoes
    the whole investigation (fresh LLM + tool cost)."""
    from lib.investigator import Investigator
    store = _get_store()
    try:
        tool_fn, llm_fn = _make_investigation_deps()
        verdict = Investigator(tool_fn, llm_fn).investigate(case)   # returns the verdict; app persists it
        store.record_verdict(inv_id, case_id, verdict)
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            store.fail_investigation(inv_id, case_id, e)
        except Exception:
            traceback.print_exc()


def _launch_inprocess(inv_id, case_id, case):
    """Spawn the investigation on a daemon thread and return immediately. Verified: a Databricks App runs
    background threads past the HTTP response. Threads (not a process pool) are fine — investigations are
    I/O-bound (warehouse + LLM), so the GIL isn't the bottleneck. Concurrency at volume is untested."""
    import threading
    threading.Thread(target=_run_investigation_inprocess, args=(inv_id, case_id, case),
                     name=f"investigate-{inv_id}", daemon=True).start()


def _trigger_inprocess(case_id):
    store = _get_store()
    case = store.load_case(case_id)   # full case CONTENT (title..scenario_label) for the Investigator
    if not case:
        raise ValueError(f"case {case_id} not found")
    inv_id = store.open_investigation(case_id, model_endpoint="aia-app-in-process",
                                      run_ref="", investigated_by=resolve.app_sp_id() or "app")
    _launch_inprocess(inv_id, case_id, case)
    return {"case_id": case_id, "investigation_id": inv_id, "mode": "in_process",
            "status": "investigation_started"}


# ── job mode ─────────────────────────────────────────────────────────────────────────────────────
def _fire_investigate_job(inv_id, case_id, case):
    """jobs.run_now the investigate job for an already-opened investigation, passing the case CONTENT as
    parameters (the job has no read access to Lakebase), record the resulting run id, and append a
    'dispatched' event. Shared by the initial trigger and the reconcile re-trigger.

    The job needs NOTHING about the app — no URL, no OAuth audience, no CAN_USE. It reports by appending to
    the journal (lib/journal.py), so the only thing that must be recorded here is the run id: it is the
    authenticity binding the app later checks each event against."""
    job_id = _resolve_investigate_job_id()   # resolved by name (raises if not deployed yet)
    params = {"case_id": case_id, "investigation_id": inv_id, "catalog": CATALOG}
    params.update({f"case_{k}": ("" if case.get(k) is None else str(case.get(k)))
                   for k in _CASE_PARAM_FIELDS})
    run = _w.jobs.run_now(job_id=int(job_id), job_parameters=params)
    # The run id only exists now (after run_now) — record it against the investigation the app just opened.
    # The job independently learns the SAME id from the platform ({{job.run_id}}); the two must match for
    # any of its events to be applied.
    store = _get_store()
    store.set_job_run_id(inv_id, run.run_id)
    journal.append_event(store._connect, inv_id, journal.DISPATCHED, run.run_id, case_id=case_id)
    return int(job_id), run.run_id


def _trigger_job(case_id):
    store = _get_store()
    case = store.load_case(case_id)
    if not case:
        raise ValueError(f"case {case_id} not found")
    # Tag the runner by mode so traces/tests can tell the two job variants apart (spark vs warehouse tools).
    inv_id = store.open_investigation(case_id, model_endpoint=f"aia-investigate-{AGENT_MODE}",
                                      run_ref="", investigated_by=JOB_SP or "job")
    job_id, run_id = _fire_investigate_job(inv_id, case_id, case)
    return {"case_id": case_id, "investigation_id": inv_id, "job_id": job_id,
            "run_id": run_id, "mode": AGENT_MODE, "status": "investigation_started"}


def apply_journal_events():
    """Fold the investigate job's terminal journal events into `investigations` (job mode).

    The job appends 'completed' (with the full verdict) or
    'failed' to an append-only journal it can only INSERT into; the app — the sole owner of state — reads
    those events and applies them through the SAME record_verdict the in_process path uses. So there is one
    completion contract, and the job needs no network path to the app, no token exchange, and no CAN_USE.

    Authenticity is enforced IN SQL by journal.pending_terminal_events: an event is only selected when its
    job_run_id matches the run id this app recorded at jobs.run_now for that investigation, and the row is
    still 'running'. A forged or replayed event simply never selects — there's no Python check to get wrong.

    Returns a small summary dict. Never raises (it runs on a background thread and at startup): one bad row
    must not stop the rest, and Lakebase being briefly unreachable is not an error worth crashing for."""
    summary = {"applied": 0, "failed": 0, "errors": 0}
    try:
        store = _get_store()
        events = journal.pending_terminal_events(store._connect)
    except Exception as e:
        print(f"[journal] skipped — could not read pending events: {e}")
        return summary
    for ev in events:
        inv_id, case_id = ev["investigation_id"], ev["case_id"]
        try:
            if ev["event_type"] == journal.COMPLETED:
                verdict = ev["verdict"]
                # pg8000 returns JSONB already parsed; tolerate a string just in case.
                if isinstance(verdict, str):
                    verdict = json.loads(verdict)
                store.record_verdict(inv_id, case_id, verdict or {})
                summary["applied"] += 1
                print(f"[journal] {inv_id} ({case_id}) verdict applied from run {ev['job_run_id']}")
            else:   # FAILED — the job told us WHY, so record that instead of inferring from the Jobs API
                store.fail_investigation(inv_id, case_id, ev.get("detail") or "job reported failure")
                summary["failed"] += 1
                print(f"[journal] {inv_id} ({case_id}) failed: {str(ev.get('detail'))[:120]}")
            # Stamp AFTER the state write. record_verdict is idempotent, so a crash in between just replays.
            journal.mark_applied(store._connect, ev["event_id"])
        except Exception as e:
            summary["errors"] += 1
            print(f"[journal] {inv_id} ({case_id}) apply error: {e}")
    return summary


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# STARTUP RECONCILE — the other half of durability. There is no immortal compute here (see the README):
# an app restart kills every in_process worker thread mid-run, and a job that finished while the app was
# down leaves its verdict waiting in the journal. So on startup we sweep the 'running' rows (the durable
# queue), apply any pending journal events, and recover each. This is
# what makes the design at-least-once.
#
# RECONCILE (re-running orphans) is STARTUP-ONLY, and that hasn't changed: a live in_process worker's row is
# ALSO status='running', so a periodic sweep couldn't tell a genuinely-orphaned row from one a thread is
# actively working — it would double-run live investigations. A freshly-started process has NO live worker
# threads, so every 'running' row it sees is provably orphaned.
#
# APPLYING JOURNAL EVENTS is different and IS safe to run periodically (see _journal_poll below): a terminal
# event is positive evidence the job finished, not an inference from a row's status. So the periodic sweep
# only ever *applies verdicts the job already produced* — it never starts work.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
def reconcile_running_investigations():
    """Recover investigations left 'running' by a crash/restart. Returns a small summary dict (also useful
    for a /admin probe). Best-effort: logs + continues per row so one bad row can't block the rest, and
    never raises to the caller (startup must not fail because Lakebase isn't reachable yet)."""
    summary = {"scanned": 0, "requeued": 0, "recovered": 0, "left": 0, "abandoned": 0, "errors": 0}
    # FIRST fold in any terminal journal events — a job may have finished while the app was down. Doing this
    # before the sweep means those rows are already 'complete'/'failed' and won't be re-run needlessly.
    if IS_JOB:
        summary["recovered"] = apply_journal_events().get("applied", 0)
    try:
        store = _get_store()
        rows = store.running_investigations()
    except Exception as e:
        # Lakebase not provisioned yet (pre-setup.sh) or transiently unreachable — nothing to reconcile.
        print(f"[reconcile] skipped — could not read running investigations: {e}")
        return summary
    summary["scanned"] = len(rows)
    for r in rows:
        inv_id, case_id = r["investigation_id"], r["case_id"]
        try:
            _reconcile_one(store, r, summary)
        except Exception as e:
            summary["errors"] += 1
            print(f"[reconcile] {inv_id} ({case_id}) error: {e}")
    print(f"[reconcile] {AGENT_MODE}: {summary}")
    return summary


def _classify_job_state(run_id):
    """Ask the Jobs API whether a run is still executing, and map its answer to a reconcile.JOB_* state.
    This is the only I/O the job-mode decision needs beyond the journal + the row itself."""
    if not run_id:
        return reconcile.JOB_NO_RUN_ID   # no run id was ever recorded — nothing to check, safe to re-fire
    try:
        run = _w.jobs.get_run(run_id=int(run_id))
        life = getattr(getattr(run.state, "life_cycle_state", None), "value", None) if run.state else None
        if life in ("PENDING", "RUNNING", "BLOCKED", "QUEUED", "TERMINATING"):
            return reconcile.JOB_RUNNING     # still executing — its terminal event will land in the journal
        return reconcile.JOB_GONE            # terminated with no applicable verdict → treat as gone, re-fire
    except NotFound:
        # The run was deleted, or the Jobs API auto-removed it after 60 days. Genuinely gone → safe to re-fire.
        return reconcile.JOB_GONE
    except Exception as e:
        # Transient (network/5xx/rate-limit): the run may be ALIVE. Don't act on a guess — leave it.
        print(f"[reconcile] run {run_id} get_run errored transiently ({e}); will leave as-is")
        return reconcile.JOB_TRANSIENT


def _reconcile_one(store, r, summary):
    """Gather the signals for one orphaned 'running' row, ask reconcile.decide() what to do, then do it.

    The DECISION (over-cap → abandon; still-running → leave; never-started → re-fire uncounted; started-
    then-died → re-fire counted; in_process → re-run counted) is the pure, table-tested reconcile.decide;
    this function is just the I/O around it. The journal's `started` event is what lets a job re-fire tell
    "never got compute" (don't burn an attempt) apart from "ran and died" (the crash-loop the cap bounds)."""
    inv_id, case_id = r["investigation_id"], r["case_id"]
    attempts = int(r.get("attempts") or 0)
    mode = "job" if IS_JOB else "in_process"

    case = store.load_case(case_id)             # needed to re-run, and to know the case still exists
    last_event, job_state = None, None
    if IS_JOB:
        try:
            last_event = journal.last_event_type(store._connect, inv_id)
        except Exception as e:
            print(f"[reconcile] {inv_id} journal read failed ({e}); proceeding on the Jobs API alone")
        job_state = _classify_job_state((r.get("job_run_id") or "").strip())

    action = reconcile.decide(mode, attempts, MAX_ATTEMPTS, case is not None, job_state, last_event)

    if action == reconcile.LEAVE:
        summary["left"] += 1
        print(f"[reconcile] {inv_id} ({case_id}) left as-is (job still running / transient error)")
        return
    if action == reconcile.ABANDON:
        reason = (f"exceeded {MAX_ATTEMPTS} attempts; abandoned by startup reconcile"
                  if attempts >= MAX_ATTEMPTS else "case disappeared; cannot re-run")
        store.abandon_investigation(inv_id, case_id, reason)
        summary["abandoned"] += 1
        print(f"[reconcile] {inv_id} ({case_id}) abandoned — {reason}")
        return

    # REQUEUE_COUNTED / REQUEUE_UNCOUNTED — re-run; only COUNTED bumps the attempt cap.
    if action == reconcile.REQUEUE_COUNTED:
        store.bump_attempts(inv_id)
    if IS_JOB:
        try:
            _fire_investigate_job(inv_id, case_id, case)
            summary["requeued"] += 1
            print(f"[reconcile] {inv_id} ({case_id}) re-fired as a job "
                  f"(last_event={last_event}, attempt {'' if action == reconcile.REQUEUE_COUNTED else 'not '}counted)")
        except Exception as e:
            store.fail_investigation(inv_id, case_id, f"could not re-fire the investigate job: {e}")
            summary["left"] += 1
    else:
        _launch_inprocess(inv_id, case_id, case)
        summary["requeued"] += 1
        print(f"[reconcile] {inv_id} ({case_id}) re-run in-process")


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# JOURNAL POLL (job mode only) — the app's side of the handoff.
# This is deliberately the cheapest possible loop:
# a single indexed Postgres read per tick, gated so it costs nothing when nothing is outstanding. It never
# touches the warehouse and never starts work — it only applies verdicts the job has already produced.
# Safe to run periodically (unlike re-running orphans) because a terminal event is positive evidence the job
# finished, not an inference from a row's status.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
JOURNAL_POLL_SECONDS = cfg.journal_poll_seconds


def _journal_poll():
    """Background loop: apply terminal journal events as the jobs finish. Started once at app startup in job
    mode (see start_journal_poll). Errors are swallowed per tick — a transient Lakebase blip must not kill
    the thread, or verdicts would stop landing until the next restart."""
    import time
    while True:
        try:
            # Gate: is anything actually outstanding? A 'running' job-mode row is the only reason to look for
            # events. When the board is idle this is one cheap indexed count and nothing else.
            store = _get_store()
            outstanding = store._query(
                "SELECT 1 FROM investigations WHERE status='running' AND job_run_id IS NOT NULL LIMIT 1")
            if outstanding:
                apply_journal_events()
        except Exception as e:
            print(f"[journal-poll] tick error (continuing): {e}")
        time.sleep(JOURNAL_POLL_SECONDS)


def start_journal_poll():
    """Start the journal poll thread — job mode only (in_process writes its verdict directly, so there is
    nothing to poll). Daemon so it never blocks app shutdown. Verified: a Databricks App runs background
    threads past the HTTP response."""
    if not IS_JOB:
        return False
    import threading
    threading.Thread(target=_journal_poll, name="journal-poll", daemon=True).start()
    print(f"[journal-poll] started (every {JOURNAL_POLL_SECONDS}s)")
    return True
