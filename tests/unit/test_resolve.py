"""Tier 1 — lib/resolve.py runtime resolution of post-deploy values.

Offline: fake WorkspaceClient surfaces (jobs.list, api_client.do), no network. Covers the two branches
that carry real logic — investigate_job_id's exact-then-suffix (prod vs dev-mode `[dev user]` prefix)
match with ambiguity/absence handling, and pg_host's endpoint lookup — plus app_sp_id env precedence.
"""
import types

import pytest

from lib import resolve


# ── fakes ───────────────────────────────────────────────────────────────────
class _Job:
    def __init__(self, job_id, name):
        self.job_id = job_id
        self.settings = types.SimpleNamespace(name=name)


class _FakeJobs:
    """Models jobs.list(name=...) server-side EXACT match; list() returns everything (dev fallback)."""
    def __init__(self, jobs):
        self._jobs = jobs

    def list(self, name=None):
        if name is None:
            return list(self._jobs)
        return [j for j in self._jobs if j.settings.name == name]


class _FakeWS:
    def __init__(self, jobs=(), endpoints_resp=None):
        self.jobs = _FakeJobs(list(jobs))
        self._endpoints_resp = endpoints_resp
        self.api_client = types.SimpleNamespace(do=self._do)

    def _do(self, method, path, **kw):
        assert method == "GET"
        return self._endpoints_resp


# ── investigate_job_id ────────────────────────────────────────────────────────
def test_job_id_exact_match_prod():
    w = _FakeWS(jobs=[_Job(11, "aia-investigate"), _Job(22, "something-else")])
    assert resolve.investigate_job_id(w, "aia-investigate") == 11


def test_job_id_suffix_fallback_dev_mode():
    # dev-mode prepends `[dev user] ` — no exact match, but the suffix matches.
    w = _FakeWS(jobs=[_Job(99, "[dev alice] aia-investigate")])
    assert resolve.investigate_job_id(w, "aia-investigate") == 99


def test_job_id_none_found_raises():
    w = _FakeWS(jobs=[_Job(1, "unrelated")])
    with pytest.raises(RuntimeError, match="no job named"):
        resolve.investigate_job_id(w, "aia-investigate")


def test_job_id_ambiguous_exact_raises():
    w = _FakeWS(jobs=[_Job(1, "aia-investigate"), _Job(2, "aia-investigate")])
    with pytest.raises(RuntimeError, match="must be unique"):
        resolve.investigate_job_id(w, "aia-investigate")


def test_job_id_ambiguous_suffix_raises():
    w = _FakeWS(jobs=[_Job(1, "[dev a] aia-investigate"), _Job(2, "[dev b] aia-investigate")])
    with pytest.raises(RuntimeError, match="must be unique"):
        resolve.investigate_job_id(w, "aia-investigate")


# ── pg_host ───────────────────────────────────────────────────────────────────
def _endpoints(*eps):
    return {"endpoints": list(eps)}


def test_pg_host_found():
    resp = _endpoints({"name": "projects/p/branches/production/endpoints/primary",
                       "status": {"hosts": {"host": "db.example.com"}}})
    w = _FakeWS(endpoints_resp=resp)
    assert resolve.pg_host(w, "p", "production", "primary") == "db.example.com"


def test_pg_host_endpoint_present_but_no_host_raises():
    resp = _endpoints({"name": "projects/p/branches/production/endpoints/primary",
                       "status": {"hosts": {}}})
    w = _FakeWS(endpoints_resp=resp)
    with pytest.raises(RuntimeError, match="no host for endpoint 'primary'"):
        resolve.pg_host(w, "p", "production", "primary")


def test_pg_host_no_matching_endpoint_raises():
    resp = _endpoints({"name": "projects/p/branches/production/endpoints/secondary",
                       "status": {"hosts": {"host": "other"}}})
    w = _FakeWS(endpoints_resp=resp)
    with pytest.raises(RuntimeError, match="no host for endpoint 'primary'"):
        resolve.pg_host(w, "p", "production", "primary")


def test_pg_host_empty_response_raises():
    w = _FakeWS(endpoints_resp={})
    with pytest.raises(RuntimeError):
        resolve.pg_host(w, "p", "production", "primary")


# ── app_sp_id + endpoint_path ──────────────────────────────────────────────────
def test_app_sp_id_prefers_pg_user(monkeypatch):
    monkeypatch.setenv("AIA_PG_USER", "  pg-user  ")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "client-id")
    assert resolve.app_sp_id() == "pg-user"


def test_app_sp_id_falls_back_to_client_id(monkeypatch):
    monkeypatch.delenv("AIA_PG_USER", raising=False)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", " client-id ")
    assert resolve.app_sp_id() == "client-id"


def test_app_sp_id_empty_when_unset(monkeypatch):
    monkeypatch.delenv("AIA_PG_USER", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
    assert resolve.app_sp_id() == ""


def test_endpoint_path_format():
    assert resolve.endpoint_path("proj", "production", "primary") == \
        "projects/proj/branches/production/endpoints/primary"
