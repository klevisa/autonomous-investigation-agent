"""Tier 2 — PostgresStateStore against a real Postgres (lib/state_store.py).

Proves the SQL is valid and the investigation lifecycle + case roll-up behave: open -> record_verdict
(idempotent) / fail / abandon, and the reconcile read (running_investigations, bump_attempts).
"""
import pytest

from lib.pg import pg_exec, pg_query
from lib.state_store import PostgresStateStore

pytestmark = pytest.mark.integration


def _seed_case(connect, case_id="CASE-1", **over):
    cols = {"title": "t", "description": "d", "severity": "medium", "status": "new",
            "indicator_value": "http://bad.example/x", "indicator_type": "url",
            "account_id": "ACC-000888", "scenario_label": "s"}
    cols.update(over)
    pg_exec(connect,
            """INSERT INTO cases (case_id, title, description, severity, status, indicator_value,
                                  indicator_type, account_id, scenario_label, created_at, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), now())""",
            (case_id, cols["title"], cols["description"], cols["severity"], cols["status"],
             cols["indicator_value"], cols["indicator_type"], cols["account_id"], cols["scenario_label"]))


def _case_status(connect, case_id="CASE-1"):
    return pg_query(connect, "SELECT status FROM cases WHERE case_id=%s", (case_id,))[0]["status"]


VERDICT = {"assessed_severity": "high", "escalate_to_high": True, "recommended_play": "account_suspended",
           "confidence": 0.9, "summary": "sum", "rationale": "why",
           "evidence": {"enrich_indicator": [{"threat": "malware"}]}, "tools_called": ["enrich_indicator"]}


def test_load_case_returns_content(connect):
    _seed_case(connect)
    store = PostgresStateStore(connect)
    case = store.load_case("CASE-1")
    assert case["case_id"] == "CASE-1"
    assert case["indicator_value"] == "http://bad.example/x"


def test_open_investigation_sets_running_and_investigating(connect):
    _seed_case(connect)
    store = PostgresStateStore(connect)
    inv_id = store.open_investigation("CASE-1", "ep", "", "me@x")
    assert inv_id.startswith("INV-")
    rows = store.running_investigations()
    assert len(rows) == 1
    assert rows[0]["investigation_id"] == inv_id
    assert int(rows[0]["attempts"]) == 1          # open counts as attempt 1
    assert _case_status(connect) == "investigating"


def test_record_verdict_rolls_up_and_is_idempotent(connect):
    _seed_case(connect)
    store = PostgresStateStore(connect)
    inv_id = store.open_investigation("CASE-1", "ep", "", "me@x")

    store.record_verdict(inv_id, "CASE-1", VERDICT)
    inv = pg_query(connect, "SELECT * FROM investigations WHERE investigation_id=%s", (inv_id,))[0]
    assert inv["status"] == "complete"
    assert inv["escalate_to_high"] is True
    assert inv["evidence"] == VERDICT["evidence"]          # JSONB round-trips to a dict
    assert inv["tools_called"] == ["enrich_indicator"]
    assert _case_status(connect) == "escalated"            # escalate_to_high -> escalated

    # replay (at-least-once): applying the same verdict again is a harmless overwrite
    store.record_verdict(inv_id, "CASE-1", VERDICT)
    again = pg_query(connect, "SELECT status FROM investigations WHERE investigation_id=%s", (inv_id,))[0]
    assert again["status"] == "complete"
    assert _case_status(connect) == "escalated"


def test_record_verdict_non_escalated_sets_investigated(connect):
    _seed_case(connect)
    store = PostgresStateStore(connect)
    inv_id = store.open_investigation("CASE-1", "ep", "", "me@x")
    store.record_verdict(inv_id, "CASE-1", {**VERDICT, "escalate_to_high": False})
    assert _case_status(connect) == "investigated"


def test_fail_investigation_returns_case_to_new(connect):
    _seed_case(connect)
    store = PostgresStateStore(connect)
    inv_id = store.open_investigation("CASE-1", "ep", "", "me@x")
    store.fail_investigation(inv_id, "CASE-1", RuntimeError("boom"))
    inv = pg_query(connect, "SELECT status, rationale FROM investigations WHERE investigation_id=%s",
                   (inv_id,))[0]
    assert inv["status"] == "failed"
    assert "boom" in inv["rationale"]
    assert _case_status(connect) == "new"                  # eligible for retry


def test_abandon_investigation_flags_needs_review(connect):
    _seed_case(connect)
    store = PostgresStateStore(connect)
    inv_id = store.open_investigation("CASE-1", "ep", "", "me@x")
    store.abandon_investigation(inv_id, "CASE-1", "over the cap")
    inv = pg_query(connect, "SELECT status FROM investigations WHERE investigation_id=%s", (inv_id,))[0]
    assert inv["status"] == "failed"
    assert _case_status(connect) == "needs_review"         # NOT returned to new


def test_bump_attempts_increments_and_returns_new_count(connect):
    _seed_case(connect)
    store = PostgresStateStore(connect)
    inv_id = store.open_investigation("CASE-1", "ep", "", "me@x")
    assert store.bump_attempts(inv_id) == 2
    assert store.bump_attempts(inv_id) == 3


def test_set_job_run_id(connect):
    _seed_case(connect)
    store = PostgresStateStore(connect)
    inv_id = store.open_investigation("CASE-1", "ep", "", "me@x")
    store.set_job_run_id(inv_id, 12345)
    row = pg_query(connect, "SELECT job_run_id FROM investigations WHERE investigation_id=%s", (inv_id,))[0]
    assert row["job_run_id"] == "12345"


def test_load_case_missing_returns_none(connect):
    assert PostgresStateStore(connect).load_case("NO-SUCH-CASE") is None


def test_verdict_timestamps_are_ordered(connect):
    _seed_case(connect)
    store = PostgresStateStore(connect)
    inv_id = store.open_investigation("CASE-1", "ep", "", "me@x")
    store.record_verdict(inv_id, "CASE-1", VERDICT)
    row = pg_query(connect, "SELECT started_at, finished_at FROM investigations WHERE investigation_id=%s",
                   (inv_id,))[0]
    assert row["started_at"] is not None and row["finished_at"] is not None
    assert row["finished_at"] >= row["started_at"]     # completion never precedes start
