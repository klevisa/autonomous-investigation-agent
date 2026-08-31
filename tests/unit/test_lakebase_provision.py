"""Tier 1 — databricks_ops/lakebase.py autoscaling-project provisioning.

Offline: a fake WorkspaceClient records/answers api_client.do calls (the postgres REST surface has no typed
SDK), time.sleep is monkeypatched so the endpoint-wait loop runs instantly. Covers the branches that carry
real logic: soft-deleted purge-before-recreate, live reuse, the wait-until-ACTIVE poll (success / transient /
timeout), the scale-to-zero PATCH, create-if-absent database, and branch-role ownership selection.
"""
import types

import pytest

from databricks_ops import lakebase as lb


def _seq(*vals):
    """A responder that yields each value in turn, then sticks on the last (for progressive endpoint state)."""
    box = {"i": 0}

    def _r():
        i = min(box["i"], len(vals) - 1)
        box["i"] += 1
        v = vals[i]
        if isinstance(v, Exception):
            raise v
        return v
    return _r


class _FakeWS:
    """Answers api_client.do by matching (method, path-suffix). Suffix (endswith) matching is used because
    the project id appears in EVERY path's prefix — only the suffix distinguishes projects/ vs /endpoints vs
    /databases vs /roles. `.on` REPLACES a same-key handler so a test can override the _base_ws default."""
    def __init__(self, user_name="me@example.com"):
        self.calls = []
        self._handlers = []   # (method, path_suffix, responder)
        self.current_user = types.SimpleNamespace(me=lambda: types.SimpleNamespace(user_name=user_name))
        self.api_client = types.SimpleNamespace(do=self._do)

    def on(self, method, suffix, responder):
        self._handlers = [(m, s, r) for (m, s, r) in self._handlers if not (m == method and s == suffix)]
        self._handlers.append((method, suffix, responder))
        return self

    def _do(self, method, path, body=None, query=None):
        self.calls.append((method, path, body, query))
        norm = path.rstrip("/")
        for m, suf, r in self._handlers:
            if m == method and norm.endswith(suf):
                return r() if callable(r) else r
        return {}

    def made(self, method, suffix):
        return [c for c in self.calls if c[0] == method and c[1].rstrip("/").endswith(suffix)]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(lb.time, "sleep", lambda s: None)


def _base_ws(user_name="me@example.com"):
    """A WS wired for the common happy tail: endpoint ACTIVE, PATCH ok, no db yet, one role, db create ok."""
    return (_FakeWS(user_name)
            .on("GET", "/endpoints", _seq({"endpoints": [{"status": {"current_state": "ACTIVE"}}]}))
            .on("PATCH", "/endpoints/primary", lambda: {})
            .on("GET", "/databases", lambda: {"databases": []})
            .on("GET", "/roles", lambda: {"roles": [{"role_id": "sp-me"}]})
            .on("POST", "/databases", lambda: {}))


def test_fresh_create_when_project_absent():
    w = _base_ws().on("GET", "/projects/proj", _seq(RuntimeError("404 not found")))
    out = lb.provision(w, "proj", "aia_db", min_cu=0.5, max_cu=2.0)
    assert out["endpoint_state"] == "ACTIVE"
    assert out["database"] == "created"
    assert w.made("POST", "/postgres/projects"), "should create the project when absent"
    assert not w.made("DELETE", "/projects/proj"), "no purge when project never existed"


def test_live_project_is_reused_not_recreated():
    w = _base_ws().on("GET", "/projects/proj", lambda: {"display_name": "AIA"})  # no delete_time → live
    w.on("GET", "/databases", lambda: {"databases": [{"status": {"postgres_database": "aia_db"}}]})
    out = lb.provision(w, "proj", "aia_db")
    assert out["database"] == "reused"
    assert not w.made("POST", "/postgres/projects"), "a live project must not be recreated"


def test_soft_deleted_project_is_purged_then_recreated():
    w = _base_ws().on("GET", "/projects/proj", lambda: {"delete_time": "2026-01-01T00:00:00Z"})
    lb.provision(w, "proj", "aia_db")
    purge = w.made("DELETE", "/projects/proj")
    assert purge, "soft-deleted project must be purged"
    assert purge[0][3] == {"purge": "true"}, "purge must pass ?purge=true to free the name"
    assert w.made("POST", "/postgres/projects"), "then recreate"


def test_endpoint_wait_tolerates_transient_then_active():
    # first endpoints read blows up (project not materialized yet), then PENDING, then ACTIVE
    w = (_base_ws()
         .on("GET", "/projects/proj", _seq(RuntimeError("absent")))
         .on("GET", "/endpoints", _seq(RuntimeError("404 Project not found"),
                                       {"endpoints": [{"status": {"current_state": "PENDING"}}]},
                                       {"endpoints": [{"status": {"current_state": "ACTIVE"}}]})))
    out = lb.provision(w, "proj", "aia_db")
    assert out["endpoint_state"] == "ACTIVE"


def test_endpoint_never_active_raises():
    w = (_base_ws()
         .on("GET", "/projects/proj", _seq(RuntimeError("absent")))
         .on("GET", "/endpoints", lambda: {"endpoints": [{"status": {"current_state": "PENDING"}}]}))
    with pytest.raises(RuntimeError, match="not ACTIVE"):
        lb.provision(w, "proj", "aia_db")


def test_scale_to_zero_patch_issued_with_cu_and_mask():
    w = _base_ws().on("GET", "/projects/proj", _seq(RuntimeError("absent")))
    lb.provision(w, "proj", "aia_db", min_cu=0.25, max_cu=4.0)
    patches = w.made("PATCH", "/endpoints/primary")
    assert patches, "must set autoscaling limits"
    body, query = patches[0][2], patches[0][3]
    assert body["spec"]["autoscaling_limit_min_cu"] == 0.25
    assert body["spec"]["autoscaling_limit_max_cu"] == 4.0
    assert "autoscaling_limit_min_cu" in query["update_mask"]


def test_ensure_pg_database_reused_when_present():
    w = (_FakeWS()
         .on("GET", "/databases", lambda: {"databases": [{"status": {"postgres_database": "aia_db"}}]}))
    assert lb._ensure_pg_database(w, "projects/p/branches/production", "aia_db") == "reused"
    assert not w.made("POST", "/databases"), "present db must not be recreated"


# ── _own_branch_role ──────────────────────────────────────────────────────────
def test_own_branch_role_prefers_callers_own_role():
    w = _FakeWS(user_name="sp-abc123").on(
        "GET", "/roles", lambda: {"roles": [{"role_id": "sp-xyz"}, {"role_id": "sp-abc123"}]})
    assert lb._own_branch_role(w, "projects/p/branches/production").endswith("/roles/sp-abc123")


def test_own_branch_role_falls_back_to_first():
    w = _FakeWS(user_name="nobody@x").on(
        "GET", "/roles", lambda: {"roles": [{"role_id": "sp-first"}, {"role_id": "sp-second"}]})
    assert lb._own_branch_role(w, "projects/p/branches/production").endswith("/roles/sp-first")


def test_own_branch_role_no_roles_raises():
    w = _FakeWS().on("GET", "/roles", lambda: {"roles": []})
    with pytest.raises(RuntimeError, match="no Postgres roles"):
        lb._own_branch_role(w, "projects/p/branches/production")
