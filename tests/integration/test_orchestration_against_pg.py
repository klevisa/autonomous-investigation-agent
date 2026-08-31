"""Tier 2 — app/investigations.py orchestration driven against a REAL Postgres.

The unit tests (tests/unit/test_investigations_orchestration.py) prove the branching with a mocked store;
this proves the SAME orchestration functions against real SQL — apply_journal_events and the startup
reconcile sweep really read/write cases + investigations + investigation_events. Only the Databricks Jobs
API is mocked (no cloud); the store IS a PostgresStateStore over the ephemeral local Postgres. This is the
bridge between the fast unit layer and the expensive e2e.
"""
import importlib
import sys
import types

import pytest

from lib.pg import pg_exec, pg_query
from lib.state_store import PostgresStateStore
from lib import journal

pytestmark = pytest.mark.integration

_ENV = {
    "AIA_CATALOG": "cat", "AIA_SCHEMA": "sch", "AIA_LAKEBASE_PROJECT": "proj",
    "AIA_LAKEBASE_BRANCH": "production", "AIA_LAKEBASE_ENDPOINT": "primary",
    "AIA_PG_DATABASE": "db", "AIA_INVESTIGATE_JOB_NAME": "aia-investigate",
    "AIA_APP_NAME": "aia-app", "AIA_WAREHOUSE_ID": "wh-1", "AIA_JOB_SP": "job-sp",
    "DATABRICKS_HOST": "https://example.invalid", "DATABRICKS_CLIENT_ID": "app-sp",
}


def _load_investigations(monkeypatch, connect, *, mode, max_attempts=3):
    env = dict(_ENV, AIA_AGENT_MODE=mode, AIA_MAX_ATTEMPTS=str(max_attempts))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import databricks.sdk as sdk
    monkeypatch.setattr(sdk, "WorkspaceClient", lambda *a, **k: types.SimpleNamespace())
    for m in ("app.investigations", "app.main", "app.config"):
        sys.modules.pop(m, None)
    inv = importlib.import_module("app.investigations")
    store = PostgresStateStore(connect)
    monkeypatch.setattr(inv, "_get_store", lambda: store)
    return inv, store


def _seed_case(connect, case_id="CASE-1", status="new"):
    pg_exec(connect,
            """INSERT INTO cases (case_id, title, severity, status, indicator_value, indicator_type,
                                  account_id, scenario_label, created_at, updated_at)
               VALUES (%s,'t','medium',%s,'1.2.3.4','ip','ACC-1','s', now(), now())""",
            (case_id, status))


def _status(connect, case_id="CASE-1"):
    return pg_query(connect, "SELECT status FROM cases WHERE case_id=%s", (case_id,))[0]["status"]


def _attempts(connect, inv_id):
    return int(pg_query(connect, "SELECT attempts FROM investigations WHERE investigation_id=%s",
                        (inv_id,))[0]["attempts"])


# ── apply_journal_events (job mode) against real SQL ──────────────────────────
def test_apply_journal_completed_rolls_up_case(monkeypatch, connect):
    inv, store = _load_investigations(monkeypatch, connect, mode="job")
    _seed_case(connect)
    inv_id = store.open_investigation("CASE-1", "ep", "", "job-sp")
    store.set_job_run_id(inv_id, "900")
    journal.append_event(store._connect, inv_id, journal.COMPLETED, "900", case_id="CASE-1",
                         verdict={"assessed_severity": "high", "escalate_to_high": True,
                                  "recommended_play": "account_suspended", "confidence": 0.9,
                                  "summary": "s", "rationale": "r",
                                  "evidence": {}, "tools_called": ["enrich_indicator"]})

    summary = inv.apply_journal_events()

    assert summary["applied"] == 1
    assert pg_query(connect, "SELECT status FROM investigations WHERE investigation_id=%s",
                    (inv_id,))[0]["status"] == "complete"
    assert _status(connect) == "escalated"                       # verdict rolled up to the case
    assert journal.pending_terminal_events(store._connect) == []  # event stamped applied


# ── startup reconcile (in_process) against real SQL ───────────────────────────
def test_reconcile_inprocess_requeues_and_bumps(monkeypatch, connect):
    inv, store = _load_investigations(monkeypatch, connect, mode="in_process")
    _seed_case(connect)
    inv_id = store.open_investigation("CASE-1", "ep", "", "app-sp")   # a 'running' orphan (attempts=1)
    launched = []
    monkeypatch.setattr(inv, "_launch_inprocess", lambda i, c, case: launched.append(i))

    summary = inv.reconcile_running_investigations()

    assert summary["requeued"] == 1
    assert launched == [inv_id]
    assert _attempts(connect, inv_id) == 2                       # attempt counted in the real row


def test_reconcile_over_cap_abandons_to_needs_review(monkeypatch, connect):
    inv, store = _load_investigations(monkeypatch, connect, mode="in_process", max_attempts=3)
    _seed_case(connect)
    inv_id = store.open_investigation("CASE-1", "ep", "", "app-sp")
    store.bump_attempts(inv_id); store.bump_attempts(inv_id)      # now at the cap (3)
    monkeypatch.setattr(inv, "_launch_inprocess", lambda *a: pytest.fail("must not re-run over cap"))

    summary = inv.reconcile_running_investigations()

    assert summary["abandoned"] == 1
    assert _status(connect) == "needs_review"


# ── startup reconcile (job) re-fires a gone-but-started run, against real SQL ──
def test_reconcile_job_gone_started_refires_counted(monkeypatch, connect):
    inv, store = _load_investigations(monkeypatch, connect, mode="job")
    _seed_case(connect)
    inv_id = store.open_investigation("CASE-1", "ep", "", "job-sp")
    store.set_job_run_id(inv_id, "900")
    journal.append_event(store._connect, inv_id, journal.STARTED, "900", case_id="CASE-1")  # got compute

    # Jobs API: the original run is gone; run_now returns a fresh run id for the re-fire.
    def get_run(run_id):
        return types.SimpleNamespace(state=types.SimpleNamespace(
            life_cycle_state=types.SimpleNamespace(value="TERMINATED")))

    def run_now(job_id, job_parameters):
        return types.SimpleNamespace(run_id=901)

    monkeypatch.setattr(inv, "_w", types.SimpleNamespace(
        jobs=types.SimpleNamespace(get_run=get_run, run_now=run_now)))
    monkeypatch.setattr(inv, "_resolve_investigate_job_id", lambda: 4242)

    summary = inv.reconcile_running_investigations()

    assert summary["requeued"] == 1
    assert _attempts(connect, inv_id) == 2                       # started-then-died burns an attempt
    # the re-fire recorded the new run id + appended a fresh DISPATCHED event
    assert pg_query(connect, "SELECT job_run_id FROM investigations WHERE investigation_id=%s",
                    (inv_id,))[0]["job_run_id"] == "901"
    disp = pg_query(connect, "SELECT COUNT(*) n FROM investigation_events "
                    "WHERE investigation_id=%s AND event_type='dispatched' AND job_run_id='901'", (inv_id,))
    assert int(disp[0]["n"]) == 1


# ── UI read functions (the board/detail queries) against real SQL ─────────────
def test_ui_read_functions(monkeypatch, connect):
    inv, store = _load_investigations(monkeypatch, connect, mode="in_process")
    _seed_case(connect, "CASE-1", status="new")
    _seed_case(connect, "CASE-2", status="investigating")

    ids = {c["case_id"] for c in inv.list_cases()}
    assert {"CASE-1", "CASE-2"} <= ids

    assert inv.get_case("CASE-1")["case_id"] == "CASE-1"
    assert inv.get_case("NOPE") is None

    s = inv.stats()
    assert s["total"] >= 2 and s["new"] >= 1 and s["investigating"] >= 1

    # record a verdict so investigations_for / latest_investigation have JSONB to parse
    inv_id = store.open_investigation("CASE-1", "ep", "", "by")
    store.record_verdict(inv_id, "CASE-1", {
        "assessed_severity": "high", "escalate_to_high": False, "recommended_play": "manual_review",
        "confidence": 0.5, "summary": "s", "rationale": "r",
        "evidence": {"enrich_indicator": [{"x": 1}]}, "tools_called": ["enrich_indicator"]})

    invs = inv.investigations_for("CASE-1")
    assert invs and invs[0]["investigation_id"] == inv_id
    latest = inv.latest_investigation("CASE-1")
    assert isinstance(latest["evidence"], dict)          # JSONB parsed to dict
    assert isinstance(latest["tools_called"], list)
