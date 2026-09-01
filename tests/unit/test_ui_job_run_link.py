"""Tier 1 — the case-detail UI links to the Databricks job run when (and only when) an investigation
has one. job/job_warehouse investigations carry a job_run_url (decorated onto the inv dict by the
case_detail route); in_process ones don't, and the link must then be absent. ui.case_detail is pure, so
this needs no app/workspace setup — just dicts in, HTML out.
"""
from app import ui

CASE = {
    "case_id": "CASE-0001", "title": "Suspicious download", "severity": "medium",
    "status": "investigated", "account_id": "ACC-1", "indicator_value": "http://bad.example/x",
    "indicator_type": "url", "description": "d", "assessed_severity": "low",
}


def _inv(**over):
    base = {"investigation_id": "INV-abc", "status": "complete", "assessed_severity": "low",
            "escalate_to_high": False, "recommended_play": "manual_review", "confidence": 0.7,
            "summary": "s", "rationale": "r", "tools_called": ["enrich_indicator"], "evidence": {},
            "model_endpoint": "aia-investigate-job_warehouse"}
    base.update(over)
    return base


def test_job_run_link_rendered_when_investigation_has_a_run_url():
    url = "https://host/jobs/123/runs/456?o=7"
    html = ui.case_detail(CASE, _inv(job_run_url=url), [_inv(job_run_url=url)])
    assert url in html
    assert "open in Databricks" in html


def test_no_job_run_link_when_url_absent_in_process():
    # in_process: the route leaves job_run_url unset → the "Job run" row must not appear.
    html = ui.case_detail(CASE, _inv(job_run_url=None), [_inv(job_run_url=None)])
    assert "open in Databricks" not in html
    assert "/jobs/" not in html


def test_history_rows_link_their_runs():
    inv_a = _inv(investigation_id="INV-a", job_run_url="https://host/jobs/1/runs/11?o=7")
    inv_b = _inv(investigation_id="INV-b", job_run_url="https://host/jobs/1/runs/22?o=7")
    html = ui.case_detail(CASE, inv_a, [inv_a, inv_b])
    assert "Investigation history (2)" in html
    assert "runs/11" in html and "runs/22" in html
