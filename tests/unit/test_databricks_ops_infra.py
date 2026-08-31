"""Tier 1 — databricks_ops account/workspace helpers (groups, grants, dbx).

Offline: fake api_client.do / typed SDK surfaces. Covers the logic that isn't just a passthrough — the
account-SCIM poll-with-ambiguity in groups.py, the propagation wait, the grant ACL shapes, and the
profile-vs-env client construction in dbx.py. time.sleep is patched so polls run instantly.
"""
import types

import pytest

from databricks_ops import groups as groupsmod
from databricks_ops import grants as grantsmod
from databricks_ops import dbx as dbxmod


def _seq(*vals):
    box = {"i": 0}

    def _r(*a, **k):
        i = min(box["i"], len(vals) - 1)
        box["i"] += 1
        v = vals[i]
        if isinstance(v, Exception):
            raise v
        return v
    return _r


class _WS:
    def __init__(self, do):
        self.api_client = types.SimpleNamespace(do=do)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(groupsmod.time, "sleep", lambda s: None)


# ── resolve_account_group_id ──────────────────────────────────────────────────
def test_resolve_group_found():
    w = _WS(lambda m, p: {"Resources": [{"id": "grp-1"}]})
    assert groupsmod.resolve_account_group_id(w, "aia-role") == "grp-1"


def test_resolve_group_ambiguous_raises():
    w = _WS(lambda m, p: {"Resources": [{"id": "a"}, {"id": "b"}]})
    with pytest.raises(RuntimeError, match="name must be unique"):
        groupsmod.resolve_account_group_id(w, "aia-role")


def test_resolve_group_polls_then_finds():
    # transient read, then empty (propagation lag), then visible
    w = _WS(_seq(RuntimeError("scim 500"), {"Resources": []}, {"Resources": [{"id": "grp-9"}]}))
    assert groupsmod.resolve_account_group_id(w, "aia-role", tries=5, delay=0) == "grp-9"


def test_resolve_group_absent_after_poll_raises():
    w = _WS(lambda m, p: {"Resources": []})
    with pytest.raises(RuntimeError, match="no account group named"):
        groupsmod.resolve_account_group_id(w, "aia-role", tries=3, delay=0)


# ── wait_for_sp_in_account_dir ────────────────────────────────────────────────
def test_wait_for_sp_returns_when_visible():
    w = _WS(_seq({"Resources": []}, {"Resources": [{"id": "sp"}]}))
    groupsmod.wait_for_sp_in_account_dir(w, "app-123", tries=3, delay=0)   # returns (no raise)


def test_wait_for_sp_never_visible_raises():
    w = _WS(lambda m, p: {"Resources": []})
    with pytest.raises(RuntimeError, match="not visible in the account directory"):
        groupsmod.wait_for_sp_in_account_dir(w, "app-123", tries=2, delay=0)


# ── grants ────────────────────────────────────────────────────────────────────
def test_grant_app_can_manage_run_on_job():
    captured = {}

    class _Jobs:
        def update_permissions(self, job_id, access_control_list):
            captured["job_id"] = job_id
            captured["acl"] = access_control_list
    w = types.SimpleNamespace(jobs=_Jobs())

    grantsmod.grant_app_can_manage_run_on_job(w, 777, "app-sp-id")

    assert captured["job_id"] == "777"                        # id is stringified
    acl = captured["acl"][0]
    assert acl.service_principal_name == "app-sp-id"
    from databricks.sdk.service import jobs
    assert acl.permission_level == jobs.JobPermissionLevel.CAN_MANAGE_RUN


def test_grant_dir_read_uses_object_id_and_can_read():
    captured = {}

    class _WsSvc:
        def get_status(self, path):
            captured["path"] = path
            return types.SimpleNamespace(object_id=4242)

        def update_permissions(self, workspace_object_type, workspace_object_id, access_control_list):
            captured["type"] = workspace_object_type
            captured["obj_id"] = workspace_object_id
            captured["acl"] = access_control_list
    w = types.SimpleNamespace(workspace=_WsSvc())

    grantsmod.grant_dir_read(w, "/Workspace/Shared/.bundle/x", "job-sp-id")

    assert captured["path"] == "/Workspace/Shared/.bundle/x"
    assert captured["type"] == "directories"
    assert captured["obj_id"] == "4242"
    from databricks.sdk.service import workspace as ws
    assert captured["acl"][0].permission_level == ws.WorkspaceObjectPermissionLevel.CAN_READ


# ── dbx.workspace ─────────────────────────────────────────────────────────────
def test_workspace_passes_profile(monkeypatch):
    seen = {}
    monkeypatch.setattr(dbxmod, "WorkspaceClient", lambda **k: seen.update(k) or "WS")
    assert dbxmod.workspace("my-profile") == "WS"
    assert seen == {"profile": "my-profile"}


def test_workspace_env_auth_when_no_profile(monkeypatch):
    seen = {}
    monkeypatch.setattr(dbxmod, "WorkspaceClient", lambda **k: seen.update(k, called=True) or "WS")
    assert dbxmod.workspace(None) == "WS"
    assert seen == {"called": True}      # no profile kwarg → SDK default env-var auth
