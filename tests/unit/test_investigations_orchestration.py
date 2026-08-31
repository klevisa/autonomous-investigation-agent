"""Tier 1 — app/investigations.py orchestration (mock-heavy).

This is the layer the e2e used to be the only cover for: dispatch (in_process thread vs job run_now),
the job-state classifier, the reconcile action executor around the pure reconcile.decide, and the journal
apply. Everything external (the store, the Jobs API, the journal, threads) is mocked, so these run offline
and deterministically — giving real coverage of the branching BEFORE the expensive e2e layer.
"""
import threading
import types

import pytest

from tests.unit.conftest import FakeStore

CASE = {"case_id": "C1", "title": "phishing", "description": "d", "severity": "medium",
        "indicator_value": "1.2.3.4", "indicator_type": "ip", "account_id": "A1", "scenario_label": "x"}


# ── trigger: in_process ────────────────────────────────────────────────────────
def test_trigger_inprocess_opens_and_launches(load_app_module, monkeypatch):
    inv = load_app_module(mode="in_process")
    store = FakeStore(case=CASE)
    launched = []
    monkeypatch.setattr(inv, "_get_store", lambda: store)
    monkeypatch.setattr(inv, "_launch_inprocess", lambda i, c, case: launched.append((i, c)))
    monkeypatch.setattr(inv.resolve, "app_sp_id", lambda: "app-sp")

    out = inv.trigger_investigation("C1")

    assert out["mode"] == "in_process"
    assert out["status"] == "investigation_started"
    assert out["investigation_id"] == "INV-1"
    open_call = store.find("open_investigation")[0]
    assert open_call[3] == "app-sp"                 # investigated_by = app SP (audit)
    assert launched == [("INV-1", "C1")]            # background worker spawned


def test_trigger_inprocess_missing_case_raises(load_app_module, monkeypatch):
    inv = load_app_module(mode="in_process")
    monkeypatch.setattr(inv, "_get_store", lambda: FakeStore(case=None))
    with pytest.raises(ValueError, match="case C1 not found"):
        inv.trigger_investigation("C1")


# ── trigger: job ───────────────────────────────────────────────────────────────
def test_trigger_job_fires_run_now_and_records(load_app_module, monkeypatch):
    inv = load_app_module(mode="job_warehouse", env={"AIA_JOB_SP": "job-sp"})
    store = FakeStore(case=CASE)
    captured = {}
    appended = []

    def run_now(job_id, job_parameters):
        captured["job_id"] = job_id
        captured["params"] = job_parameters
        return types.SimpleNamespace(run_id=555)

    monkeypatch.setattr(inv, "_get_store", lambda: store)
    monkeypatch.setattr(inv, "_resolve_investigate_job_id", lambda: 4242)
    monkeypatch.setattr(inv, "_w", types.SimpleNamespace(jobs=types.SimpleNamespace(run_now=run_now)))
    monkeypatch.setattr(inv.journal, "append_event",
                        lambda *a, **k: appended.append((a, k)))

    out = inv.trigger_investigation("C1")

    assert out["mode"] == "job_warehouse"
    assert out["job_id"] == 4242 and out["run_id"] == 555
    assert captured["job_id"] == 4242
    # case CONTENT is marshalled as case_* params (the job has no Lakebase read access)
    assert captured["params"]["case_id"] == "C1"
    assert captured["params"]["case_title"] == "phishing"
    assert captured["params"]["catalog"] == "cat"
    assert ("set_job_run_id", "INV-1", 555) in store.calls
    assert appended and appended[0][0][2] == inv.journal.DISPATCHED  # DISPATCHED event appended


def test_trigger_job_none_case_fields_become_empty_string(load_app_module, monkeypatch):
    inv = load_app_module(mode="job", env={"AIA_JOB_SP": "job-sp"})
    case = dict(CASE, account_id=None)     # a null field must serialize to "" (not the string "None")
    captured = {}
    monkeypatch.setattr(inv, "_get_store", lambda: FakeStore(case=case))
    monkeypatch.setattr(inv, "_resolve_investigate_job_id", lambda: 1)
    monkeypatch.setattr(inv, "_w", types.SimpleNamespace(
        jobs=types.SimpleNamespace(run_now=lambda job_id, job_parameters: captured.update(job_parameters) or types.SimpleNamespace(run_id=1))))
    monkeypatch.setattr(inv.journal, "append_event", lambda *a, **k: None)
    inv.trigger_investigation("C1")
    assert captured["case_account_id"] == ""


# ── _classify_job_state ──────────────────────────────────────────────────────
def _ws_with_run(life=None, raise_exc=None):
    def get_run(run_id):
        if raise_exc:
            raise raise_exc
        state = types.SimpleNamespace(life_cycle_state=types.SimpleNamespace(value=life)) if life else None
        return types.SimpleNamespace(state=state)
    return types.SimpleNamespace(jobs=types.SimpleNamespace(get_run=get_run))


@pytest.mark.parametrize("life,expected_attr", [
    ("RUNNING", "JOB_RUNNING"), ("PENDING", "JOB_RUNNING"), ("QUEUED", "JOB_RUNNING"),
    ("TERMINATED", "JOB_GONE"), ("INTERNAL_ERROR", "JOB_GONE"),
])
def test_classify_job_state_lifecycle(load_app_module, monkeypatch, life, expected_attr):
    inv = load_app_module(mode="job")
    monkeypatch.setattr(inv, "_w", _ws_with_run(life=life))
    assert inv._classify_job_state("123") == getattr(inv.reconcile, expected_attr)


def test_classify_job_state_no_run_id(load_app_module):
    inv = load_app_module(mode="job")
    assert inv._classify_job_state("") == inv.reconcile.JOB_NO_RUN_ID


def test_classify_job_state_notfound_is_gone(load_app_module, monkeypatch):
    inv = load_app_module(mode="job")
    monkeypatch.setattr(inv, "_w", _ws_with_run(raise_exc=inv.NotFound()))
    assert inv._classify_job_state("123") == inv.reconcile.JOB_GONE


def test_classify_job_state_transient_error_leaves(load_app_module, monkeypatch):
    inv = load_app_module(mode="job")
    monkeypatch.setattr(inv, "_w", _ws_with_run(raise_exc=RuntimeError("5xx blip")))
    assert inv._classify_job_state("123") == inv.reconcile.JOB_TRANSIENT


# ── _reconcile_one (the executor around reconcile.decide) ────────────────────────
def _summary():
    return {"scanned": 0, "requeued": 0, "recovered": 0, "left": 0, "abandoned": 0, "errors": 0}


def test_reconcile_inprocess_over_cap_abandons(load_app_module, monkeypatch):
    inv = load_app_module(mode="in_process", env={"AIA_MAX_ATTEMPTS": "3"})
    store = FakeStore(case=CASE)
    monkeypatch.setattr(inv, "_launch_inprocess", lambda *a: pytest.fail("must not re-run over cap"))
    s = _summary()
    inv._reconcile_one(store, {"investigation_id": "INV-1", "case_id": "C1", "attempts": 3}, s)
    assert store.find("abandon_investigation") and s["abandoned"] == 1


def test_reconcile_inprocess_under_cap_reruns(load_app_module, monkeypatch):
    inv = load_app_module(mode="in_process", env={"AIA_MAX_ATTEMPTS": "3"})
    store = FakeStore(case=CASE)
    launched = []
    monkeypatch.setattr(inv, "_launch_inprocess", lambda i, c, case: launched.append(i))
    s = _summary()
    inv._reconcile_one(store, {"investigation_id": "INV-1", "case_id": "C1", "attempts": 1}, s)
    assert store.find("bump_attempts") and launched == ["INV-1"] and s["requeued"] == 1


def test_reconcile_job_running_is_left(load_app_module, monkeypatch):
    inv = load_app_module(mode="job")
    store = FakeStore(case=CASE)
    monkeypatch.setattr(inv, "_classify_job_state", lambda rid: inv.reconcile.JOB_RUNNING)
    monkeypatch.setattr(inv.journal, "last_event_type", lambda conn, i: "started")
    monkeypatch.setattr(inv, "_fire_investigate_job", lambda *a: pytest.fail("must not re-fire a running job"))
    s = _summary()
    inv._reconcile_one(store, {"investigation_id": "INV-1", "case_id": "C1", "attempts": 1,
                               "job_run_id": "999"}, s)
    assert s["left"] == 1


def test_reconcile_job_gone_started_refires_counted(load_app_module, monkeypatch):
    inv = load_app_module(mode="job")
    store = FakeStore(case=CASE)
    fired = []
    monkeypatch.setattr(inv, "_classify_job_state", lambda rid: inv.reconcile.JOB_GONE)
    monkeypatch.setattr(inv.journal, "last_event_type", lambda conn, i: "started")
    monkeypatch.setattr(inv, "_fire_investigate_job", lambda i, c, case: fired.append(i))
    s = _summary()
    inv._reconcile_one(store, {"investigation_id": "INV-1", "case_id": "C1", "attempts": 1,
                               "job_run_id": "999"}, s)
    assert store.find("bump_attempts") and fired == ["INV-1"] and s["requeued"] == 1


def test_reconcile_job_gone_never_started_refires_uncounted(load_app_module, monkeypatch):
    inv = load_app_module(mode="job")
    store = FakeStore(case=CASE)
    fired = []
    monkeypatch.setattr(inv, "_classify_job_state", lambda rid: inv.reconcile.JOB_GONE)
    monkeypatch.setattr(inv.journal, "last_event_type", lambda conn, i: "dispatched")
    monkeypatch.setattr(inv, "_fire_investigate_job", lambda i, c, case: fired.append(i))
    s = _summary()
    inv._reconcile_one(store, {"investigation_id": "INV-1", "case_id": "C1", "attempts": 1,
                               "job_run_id": "999"}, s)
    assert not store.find("bump_attempts"), "a never-started job must NOT burn an attempt"
    assert fired == ["INV-1"] and s["requeued"] == 1


def test_reconcile_refire_failure_fails_investigation(load_app_module, monkeypatch):
    inv = load_app_module(mode="job")
    store = FakeStore(case=CASE)
    monkeypatch.setattr(inv, "_classify_job_state", lambda rid: inv.reconcile.JOB_GONE)
    monkeypatch.setattr(inv.journal, "last_event_type", lambda conn, i: "started")

    def boom(*a):
        raise RuntimeError("run_now 500")
    monkeypatch.setattr(inv, "_fire_investigate_job", boom)
    s = _summary()
    inv._reconcile_one(store, {"investigation_id": "INV-1", "case_id": "C1", "attempts": 1,
                               "job_run_id": "999"}, s)
    assert store.find("fail_investigation") and s["left"] == 1


# ── apply_journal_events ──────────────────────────────────────────────────────
def test_apply_journal_completed_and_failed(load_app_module, monkeypatch):
    inv = load_app_module(mode="job")
    store = FakeStore(case=CASE)
    applied = []
    events = [
        {"event_id": 1, "investigation_id": "INV-1", "case_id": "C1", "event_type": inv.journal.COMPLETED,
         "verdict": {"assessed_severity": "high"}, "job_run_id": "r1"},
        {"event_id": 2, "investigation_id": "INV-2", "case_id": "C2", "event_type": inv.journal.FAILED,
         "detail": "boom", "job_run_id": "r2"},
    ]
    monkeypatch.setattr(inv, "_get_store", lambda: store)
    monkeypatch.setattr(inv.journal, "pending_terminal_events", lambda conn: events)
    monkeypatch.setattr(inv.journal, "mark_applied", lambda conn, eid: applied.append(eid))

    summary = inv.apply_journal_events()

    assert summary == {"applied": 1, "failed": 1, "errors": 0}
    assert ("record_verdict", "INV-1", "C1", {"assessed_severity": "high"}) in store.calls
    assert store.find("fail_investigation")[0][1:] == ("INV-2", "C2", "boom")
    assert applied == [1, 2]   # both stamped applied (idempotency marker)


def test_apply_journal_one_bad_row_counts_error_and_continues(load_app_module, monkeypatch):
    inv = load_app_module(mode="job")
    store = FakeStore(case=CASE)

    def bad_record(inv_id, case_id, verdict):
        raise RuntimeError("db write failed")
    store.record_verdict = bad_record
    events = [{"event_id": 1, "investigation_id": "INV-1", "case_id": "C1",
               "event_type": inv.journal.COMPLETED, "verdict": {}, "job_run_id": "r1"}]
    monkeypatch.setattr(inv, "_get_store", lambda: store)
    monkeypatch.setattr(inv.journal, "pending_terminal_events", lambda conn: events)
    monkeypatch.setattr(inv.journal, "mark_applied", lambda conn, eid: None)

    summary = inv.apply_journal_events()
    assert summary["errors"] == 1 and summary["applied"] == 0


def test_apply_journal_unreadable_never_raises(load_app_module, monkeypatch):
    inv = load_app_module(mode="job")
    monkeypatch.setattr(inv, "_get_store", lambda: FakeStore())
    monkeypatch.setattr(inv.journal, "pending_terminal_events",
                        lambda conn: (_ for _ in ()).throw(RuntimeError("Lakebase unreachable")))
    assert inv.apply_journal_events() == {"applied": 0, "failed": 0, "errors": 0}


# ── in_process worker body / launch / journal-poll start ──────────────────────
def test_run_investigation_inprocess_records_verdict(load_app_module, monkeypatch):
    inv = load_app_module(mode="in_process")
    store = FakeStore(case=CASE)
    verdict = {"assessed_severity": "low", "escalate_to_high": False}
    monkeypatch.setattr(inv, "_get_store", lambda: store)
    monkeypatch.setattr(inv, "_make_investigation_deps", lambda: ("tool_fn", "llm_fn"))

    class _FakeInvestigator:
        def __init__(self, tool_fn, llm_fn):
            assert (tool_fn, llm_fn) == ("tool_fn", "llm_fn")

        def investigate(self, case):
            return verdict
    monkeypatch.setattr("lib.investigator.Investigator", _FakeInvestigator)

    inv._run_investigation_inprocess("INV-1", "C1", CASE)
    assert ("record_verdict", "INV-1", "C1", verdict) in store.calls


def test_run_investigation_inprocess_failure_fails_investigation(load_app_module, monkeypatch):
    inv = load_app_module(mode="in_process")
    store = FakeStore(case=CASE)
    monkeypatch.setattr(inv, "_get_store", lambda: store)
    monkeypatch.setattr(inv, "_make_investigation_deps", lambda: ("t", "l"))

    class _BoomInvestigator:
        def __init__(self, *a):
            pass

        def investigate(self, case):
            raise RuntimeError("llm down")
    monkeypatch.setattr("lib.investigator.Investigator", _BoomInvestigator)

    inv._run_investigation_inprocess("INV-1", "C1", CASE)   # must NOT raise (durability: caught + failed)
    assert store.find("fail_investigation")


def test_launch_inprocess_spawns_named_daemon_thread(load_app_module, monkeypatch):
    inv = load_app_module(mode="in_process")
    made = {}

    class _FakeThread:
        def __init__(self, target, args, name, daemon):
            made.update(target=target, args=args, name=name, daemon=daemon)

        def start(self):
            made["started"] = True
    monkeypatch.setattr(threading, "Thread", _FakeThread)

    inv._launch_inprocess("INV-1", "C1", CASE)
    assert made["daemon"] is True and made["name"] == "investigate-INV-1" and made["started"] is True


def test_start_journal_poll_job_spawns_thread(load_app_module, monkeypatch):
    inv = load_app_module(mode="job")
    made = {}

    class _FakeThread:
        def __init__(self, target, name, daemon):
            made.update(name=name, daemon=daemon)

        def start(self):
            made["started"] = True
    monkeypatch.setattr(threading, "Thread", _FakeThread)

    assert inv.start_journal_poll() is True
    assert made == {"name": "journal-poll", "daemon": True, "started": True}


def test_start_journal_poll_inprocess_is_noop(load_app_module):
    inv = load_app_module(mode="in_process")
    assert inv.start_journal_poll() is False
