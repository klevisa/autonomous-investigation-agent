"""Tier 1 — app/main.py HTTP surface (FastAPI TestClient).

Offline: the investigations orchestration module is monkeypatched (no Lakebase/jobs), and lifespan startup
(reconcile + journal poll) is stubbed so bringing the app up touches nothing. Asserts the route→status
mapping the Tines/SOC clients depend on: 400 on missing case_id, 404 on ValueError (unknown case), 500 on
other errors, JSON shapes, and the HTML error-page fallback.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(load_app_module, monkeypatch):
    main = load_app_module("app.main")
    inv = main.investigations           # app/main.py does `from app import investigations`
    # neutralize startup side-effects
    monkeypatch.setattr(inv, "reconcile_running_investigations", lambda: {})
    monkeypatch.setattr(inv, "start_journal_poll", lambda: False)
    with TestClient(main.app) as c:
        c._inv = inv        # stash for tests to monkeypatch route deps
        yield c


def test_start_investigation_happy(client, monkeypatch):
    monkeypatch.setattr(client._inv, "trigger_investigation",
                        lambda cid: {"case_id": cid, "status": "investigation_started"})
    r = client.post("/api/investigations", json={"case_id": "C1"})
    assert r.status_code == 200
    assert r.json()["status"] == "investigation_started"


def test_start_investigation_missing_case_id_400(client):
    r = client.post("/api/investigations", json={})
    assert r.status_code == 400
    assert "case_id is required" in r.json()["error"]


def test_start_investigation_unknown_case_404(client, monkeypatch):
    def boom(cid):
        raise ValueError(f"case {cid} not found")
    monkeypatch.setattr(client._inv, "trigger_investigation", boom)
    r = client.post("/api/investigations", json={"case_id": "NOPE"})
    assert r.status_code == 404


def test_start_investigation_other_error_500(client, monkeypatch):
    def boom(cid):
        raise RuntimeError("db down")
    monkeypatch.setattr(client._inv, "trigger_investigation", boom)
    r = client.post("/api/investigations", json={"case_id": "C1"})
    assert r.status_code == 500


def test_api_cases_lists_and_is_no_store(client, monkeypatch):
    monkeypatch.setattr(client._inv, "list_cases", lambda: [{"case_id": "C1"}])
    r = client.get("/api/cases")
    assert r.status_code == 200
    assert r.json()["cases"] == [{"case_id": "C1"}]
    assert r.headers.get("Cache-Control") == "no-store"


def test_api_case_found(client, monkeypatch):
    monkeypatch.setattr(client._inv, "get_case", lambda cid: {"case_id": cid})
    monkeypatch.setattr(client._inv, "investigations_for", lambda cid: [])
    r = client.get("/api/cases/C1")
    assert r.status_code == 200
    assert r.json()["case"]["case_id"] == "C1"


def test_api_case_not_found_404(client, monkeypatch):
    monkeypatch.setattr(client._inv, "get_case", lambda cid: None)
    r = client.get("/api/cases/NOPE")
    assert r.status_code == 404


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["catalog"] == "cat" and body["schema"] == "sch"


def test_board_renders_error_page_on_failure(client, monkeypatch):
    def boom():
        raise RuntimeError("lakebase not ready")
    monkeypatch.setattr(client._inv, "list_cases", boom)
    r = client.get("/")
    assert r.status_code == 200          # the board renders an error page, never 500s the UI
    assert "text/html" in r.headers["content-type"]


def test_update_case_builds_parameterized_update(client, monkeypatch):
    calls = {}

    class _S:
        def _exec(self, sql, params):
            calls["sql"] = sql
            calls["params"] = params
    monkeypatch.setattr(client._inv, "_get_store", lambda: _S())
    r = client.post("/api/cases/C1/update", json={"status": "closed", "severity": "high"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "status = %s" in calls["sql"] and "severity = %s" in calls["sql"]
    assert calls["params"] == ("closed", "high", "C1")


def test_update_case_nothing_to_update(client, monkeypatch):
    monkeypatch.setattr(client._inv, "_get_store", lambda: pytest.fail("store must not be touched"))
    r = client.post("/api/cases/C1/update", json={})
    assert r.status_code == 200 and r.json()["ok"] is False
