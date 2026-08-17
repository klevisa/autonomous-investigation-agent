"""AIA app · a Databricks App (FastAPI). Two surfaces on one app:

  1. **API (Tines-facing).** The endpoint a Tines story calls when a medium case needs the autonomous
     agent. It accepts a case id, triggers the `investigate` JOB (async), and returns immediately with
     the run info — so the agent runs on job compute, not here, and many can run at once.
       POST /api/investigations           {"case_id": "CASE-0001"}   -> {run_id, status}
       POST /api/cases/{case_id}/update   (the update-case hook — see note)  -> {ok}
       GET  /api/cases                    list cases (JSON)
       GET  /api/cases/{case_id}          one case + its investigations (JSON)

  2. **UI (SOC-facing).** A live board of cases by status (new / investigating / investigated /
     escalated) and a drill-down into a case + its investigation evidence trail.

The app runs as its own service principal (autonomous tier) and is ALWAYS the orchestrator + state
owner. AIA_AGENT_MODE picks where an investigation's compute runs: `job_warehouse` (the default — the app
fires the investigate job, which runs the tools on the warehouse as the job SP and appends its verdict to
the append-only journal, reconciled in by the app), `job` (same, on the job's own Spark session), or
`in_process` (the app runs it in a background thread with warehouse tools, verdict written directly). See
app/investigations.py. On startup the app reconciles any investigations left 'running' by a crash/restart —
that (Lakebase-as-queue + reconcile) is what makes the design durable.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder   # serializes datetime/Decimal from Lakebase rows
from fastapi.responses import HTMLResponse, JSONResponse

from app import investigations
from app import ui
from lib import resolve


@asynccontextmanager
async def lifespan(app):
    # STARTUP: recover investigations orphaned by the previous process (see investigations.reconcile...). It's
    # best-effort and never raises — if Lakebase isn't provisioned yet the app must still come up healthy
    # (data routes render the error page until setup.sh runs). Runs in a thread so a slow sweep doesn't
    # block the event loop / the app's readiness.
    import anyio
    try:
        await anyio.to_thread.run_sync(investigations.reconcile_running_investigations)
    except Exception as e:
        print(f"[startup] reconcile failed (continuing): {e}")
    # JOB MODE: start the journal poll — the job appends its verdict to an append-only table rather than
    # calling us back, so the app applies those events as they land (no-op in in_process). See lib/journal.py.
    try:
        investigations.start_journal_poll()
    except Exception as e:
        print(f"[startup] journal poll failed to start (continuing): {e}")
    yield


app = FastAPI(title="AIA Investigation Console", version="1.0", lifespan=lifespan)


# ============================== API (Tines-facing) =================================================
@app.post("/api/investigations")
async def start_investigation(request: Request):
    """Tines calls this to start an autonomous investigation for a medium case.

    Body: {"case_id": "CASE-0001"}. Triggers the investigate job and returns the run id immediately;
    the agent runs asynchronously on job compute. In production Tines would authenticate here (e.g. a
    shared secret / OAuth) and the case content could be fetched live from Tines rather than the mirror.
    """
    body = await _json(request)
    case_id = (body.get("case_id") or "").strip()
    if not case_id:
        return JSONResponse({"error": "case_id is required"}, status_code=400)
    try:
        return JSONResponse(investigations.trigger_investigation(case_id))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/cases/{case_id}/update")
async def update_case(case_id: str, request: Request):
    """The update-case hook.

    The agent job already updates the case + (stub) notifies Tines when it finishes. This endpoint is
    the inbound counterpart: a place for Tines (or an operator) to push a case update INTO the mirror —
    e.g. status/severity changed in Tines. For the PoC it accepts {status?, severity?} and updates the
    mirror. NOTE: the write-back TO Tines lives in the agent job's update_case() (stubbed there).
    """
    body = await _json(request)
    try:
        return JSONResponse(update_case_in_store(case_id, body))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/cases")
def api_cases():
    # jsonable_encoder converts Lakebase datetime/Decimal cells to JSON-safe values (plain json.dumps can't).
    return JSONResponse(jsonable_encoder({"cases": investigations.list_cases()}), headers={"Cache-Control": "no-store"})


@app.get("/api/cases/{case_id}")
def api_case(case_id: str):
    case = investigations.get_case(case_id)
    if not case:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(jsonable_encoder({"case": case, "investigations": investigations.investigations_for(case_id)}))


# ============================== UI (SOC-facing) ====================================================
@app.get("/", response_class=HTMLResponse)
def board():
    try:
        cases = investigations.list_cases()
        st = investigations.stats()
    except Exception as e:
        return HTMLResponse(ui.page(ui.error(e), active="board"))
    return HTMLResponse(ui.page(ui.board(cases, st), active="board"))


@app.get("/case/{case_id}", response_class=HTMLResponse)
def case_detail(case_id: str):
    try:
        case = investigations.get_case(case_id)
        if not case:
            return HTMLResponse(ui.page(ui.empty(f"No case {case_id}."), active="board"))
        inv = investigations.latest_investigation(case_id)
        all_invs = investigations.investigations_for(case_id)
    except Exception as e:
        return HTMLResponse(ui.page(ui.error(e), active="board"))
    return HTMLResponse(ui.page(ui.case_detail(case, inv, all_invs), active="board"))


@app.get("/healthz")
def healthz():
    return {"status": "ok", "catalog": investigations.CATALOG, "schema": investigations.SCHEMA}


# --- small helpers ---------------------------------------------------------------------------------
async def _json(request):
    try:
        return await request.json()
    except Exception:
        return {}


def update_case_in_store(case_id, body):
    """Inbound case update from Tines/operator -> the Lakebase mirror (parameterized Postgres)."""
    sets, params = [], []
    if body.get("status"):
        sets.append("status = %s"); params.append(body["status"])
    if body.get("severity"):
        sets.append("severity = %s"); params.append(body["severity"])
    if not sets:
        return {"ok": False, "reason": "nothing to update (accepts status, severity)"}
    sets.append("updated_at = now()")
    params.append(case_id)
    investigations._get_store()._exec(f"UPDATE cases SET {', '.join(sets)} WHERE case_id = %s", tuple(params))
    return {"ok": True, "case_id": case_id, "updated": {k: body[k] for k in ("status", "severity") if body.get(k)}}
