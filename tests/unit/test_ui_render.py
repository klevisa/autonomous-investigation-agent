"""Tier 1 — app/ui.py pure HTML rendering (smoke).

ui.py is dependency-free string building; these guard against template KeyErrors / crashes and confirm the
key data (case ids, escalation flag, evidence) makes it into the HTML. Not a pixel test — a "does it render
without blowing up, with the right content" test.
"""
from app import ui

CASE = {"case_id": "CASE-0001", "title": "Phish <b>", "severity": "medium", "status": "investigated",
        "assessed_severity": "high", "escalate_to_high": True, "account_id": "ACC-1",
        "indicator_value": "1.2.3.4", "indicator_type": "ip", "description": "desc"}
STATS = {"total": 3, "new": 1, "investigating": 0, "investigated": 1, "escalated": 1, "closed": 0}
INV = {"investigation_id": "INV-1", "status": "complete", "assessed_severity": "high",
       "escalate_to_high": True, "recommended_play": "account_suspended", "confidence": 0.92,
       "summary": "s", "rationale": "r", "evidence": {"enrich": [1]}, "tools_called": ["enrich_indicator"],
       "model_endpoint": "aia-app-in-process"}


def test_board_renders_cases_and_stats():
    h = ui.page(ui.board([CASE], STATS))
    assert "CASE-0001" in h
    assert "🚩" in h                       # escalate_to_high flag shown
    assert "<!doctype html>" in h.lower()


def test_board_escapes_html_in_title():
    h = ui.board([CASE], STATS)
    assert "Phish &lt;b&gt;" in h     # the title's markup is escaped, not emitted raw
    assert "Phish <b>" not in h


def test_case_detail_with_investigation():
    h = ui.case_detail(CASE, INV, [INV])
    assert "INV-1" in h
    assert "account_suspended" in h
    assert "enrich_indicator" in h
    assert "92%" in h                      # confidence formatted


def test_case_detail_no_investigation_yet():
    h = ui.case_detail(CASE, None, [])
    assert "None yet" in h


def test_error_and_empty():
    assert "Error:" in ui.error(RuntimeError("boom")) and "boom" in ui.error(RuntimeError("boom"))
    assert "nothing here" in ui.empty("nothing here")


def test_chips_handle_none():
    assert "—" in ui.sev_chip(None)
    assert ui.status_chip("needs_review")  # underscores become spaces; no crash
