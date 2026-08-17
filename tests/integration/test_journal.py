"""Tier 2 — the append-only journal against a real Postgres (lib/journal.py).

The important property is the AUTHENTICITY JOIN in pending_terminal_events: an event is applied only when
its job_run_id matches the run id the app recorded for that investigation AND the row is still 'running'.
A forged/mismatched run id must never select. Also covers last_event_type + mark_applied.
"""
import pytest

from lib.pg import pg_exec, pg_query
from lib import journal

pytestmark = pytest.mark.integration


def _seed_running_investigation(connect, inv_id="INV-1", case_id="CASE-1", job_run_id="900"):
    pg_exec(connect,
            """INSERT INTO cases (case_id, status, created_at, updated_at)
               VALUES (%s, 'investigating', now(), now())""", (case_id,))
    pg_exec(connect,
            """INSERT INTO investigations (investigation_id, case_id, status, job_run_id, attempts, started_at)
               VALUES (%s, %s, 'running', %s, 1, now())""", (inv_id, case_id, job_run_id))


def test_completed_event_with_matching_run_id_is_pending(connect):
    _seed_running_investigation(connect, job_run_id="900")
    journal.append_event(connect, "INV-1", journal.COMPLETED, "900", case_id="CASE-1",
                         verdict={"assessed_severity": "high"})
    pending = journal.pending_terminal_events(connect)
    assert len(pending) == 1
    assert pending[0]["investigation_id"] == "INV-1"
    assert pending[0]["verdict"] == {"assessed_severity": "high"}


def test_mismatched_run_id_is_never_selected(connect):
    # app dispatched run 900; a forged event claims run 999 -> must not select
    _seed_running_investigation(connect, job_run_id="900")
    journal.append_event(connect, "INV-1", journal.COMPLETED, "999", case_id="CASE-1",
                         verdict={"assessed_severity": "high"})
    assert journal.pending_terminal_events(connect) == []


def test_non_running_investigation_is_not_reopened(connect):
    _seed_running_investigation(connect, job_run_id="900")
    pg_exec(connect, "UPDATE investigations SET status='complete' WHERE investigation_id='INV-1'")
    journal.append_event(connect, "INV-1", journal.COMPLETED, "900", case_id="CASE-1",
                         verdict={"assessed_severity": "high"})
    assert journal.pending_terminal_events(connect) == []   # replay-safe


def test_started_event_is_not_terminal(connect):
    _seed_running_investigation(connect, job_run_id="900")
    journal.append_event(connect, "INV-1", journal.STARTED, "900", case_id="CASE-1")
    assert journal.pending_terminal_events(connect) == []   # only completed/failed are terminal


def test_mark_applied_removes_from_pending(connect):
    _seed_running_investigation(connect, job_run_id="900")
    journal.append_event(connect, "INV-1", journal.COMPLETED, "900", case_id="CASE-1",
                         verdict={"assessed_severity": "low"})
    ev = journal.pending_terminal_events(connect)[0]
    journal.mark_applied(connect, ev["event_id"])
    assert journal.pending_terminal_events(connect) == []


def test_last_event_type_tracks_most_recent(connect):
    _seed_running_investigation(connect, job_run_id="900")
    assert journal.last_event_type(connect, "INV-1") is None
    journal.append_event(connect, "INV-1", journal.DISPATCHED, "900", case_id="CASE-1")
    assert journal.last_event_type(connect, "INV-1") == journal.DISPATCHED
    journal.append_event(connect, "INV-1", journal.STARTED, "900", case_id="CASE-1")
    assert journal.last_event_type(connect, "INV-1") == journal.STARTED


def test_failed_event_carries_detail(connect):
    _seed_running_investigation(connect, job_run_id="900")
    journal.append_event(connect, "INV-1", journal.FAILED, "900", case_id="CASE-1",
                         detail="LLM 503")
    pending = journal.pending_terminal_events(connect)
    assert len(pending) == 1
    assert pending[0]["event_type"] == journal.FAILED
    assert pending[0]["detail"] == "LLM 503"
