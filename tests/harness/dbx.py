"""Databricks access for the harness: the SDK client (reused from databricks_ops) + thin CLI shellouts.

Two flavours:
  * SDK — `client(profile)` returns a WorkspaceClient built by `databricks_ops.dbx.workspace` (the SAME factory
    the product uses). Account-scoped work goes through its `api_client.do(...)` on workspace-proxied paths.
  * CLI — a few things are still cleanest via the `databricks` CLI: the bundle verbs (deploy/run/summary/
    destroy) and reading a raw field out of ~/.databrickscfg. `cli(...)` is the `dbx()` wrapper (adds
    --profile); `bundle(...)` runs a bundle verb from the repo root with -t/-p.

Identity is explicit: every call names its profile.
"""
from __future__ import annotations

import configparser
import json
import os
import subprocess
from pathlib import Path

from databricks.sdk import WorkspaceClient

_REPO_ROOT = Path(__file__).resolve().parents[2]   # bundle()'s default cwd = the product bundle (repo root)
# import the PRODUCT's client factory — do not re-implement it here
from databricks_ops import dbx as _dbx  # noqa: E402


def client(profile: str) -> WorkspaceClient:
    """A WorkspaceClient for a CLI profile (its identity is who the calls run as)."""
    return _dbx.workspace(profile)


# A generous default timeout so a wedged CLI call FAILS LOUD (TimeoutExpired) instead of hanging the whole
# run forever. Long ops (bundle deploy/run) pass a bigger value. Tuned above normal latency, below "give up".
_CLI_TIMEOUT = 300


def cli(profile: str, *args: str, check: bool = True, capture: bool = True,
        timeout: int = _CLI_TIMEOUT) -> subprocess.CompletedProcess:
    """Run `databricks <args> --profile <profile>`, with a timeout."""
    cmd = ["databricks", *args, "--profile", profile]
    return subprocess.run(cmd, check=check, text=True, capture_output=capture, timeout=timeout)


def cli_json(profile: str, *args: str):
    """Run a CLI command with `-o json` and parse stdout (empty/non-JSON → None, tolerant like `jget`)."""
    cp = cli(profile, *args, "-o", "json", check=False)
    try:
        return json.loads(cp.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


def bundle(profile: str, *args: str, target: str, check: bool = True,
           timeout: int = 900, cwd: str = "") -> subprocess.CompletedProcess:
    """Run a `databricks bundle <args> -t <target> -p <profile>`. `cwd` defaults to the repo root (the
    product bundle); pass a subdir to drive a different bundle (e.g. the demo/ bundle the seeder deploys).

    Longer default timeout than `cli` — deploy/run build wheels + wait on app compute — but still bounded so
    a wedged `bundle run` (e.g. an app that never reports ready) fails loud instead of hanging forever.
    """
    cmd = ["databricks", "bundle", *args, "-t", target, "-p", profile]
    return subprocess.run(cmd, cwd=(cwd or str(_REPO_ROOT)), check=check, text=True,
                          capture_output=True, timeout=timeout)


def profile_field(profile: str, field: str) -> str:
    """Read a field (host/token/client_id/client_secret/account_id) from ~/.databrickscfg for a profile."""
    cfg = configparser.ConfigParser()
    cfg.read(os.path.expanduser("~/.databrickscfg"))
    return cfg[profile].get(field, "") if cfg.has_section(profile) else ""


def account_id(profile: str) -> str:
    """The Databricks account id for a profile — from ~/.databrickscfg, else `databricks auth describe`."""
    a = profile_field(profile, "account_id")
    if a:
        return a
    cp = cli(profile, "auth", "describe", check=False)
    for line in (cp.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("account_id:"):
            return line.split(":", 1)[1].strip()
    return ""


def app(profile: str, app_name: str) -> dict:
    """`apps get` as a dict (empty dict on miss)."""
    return cli_json(profile, "apps", "get", app_name) or {}


def app_url(profile: str, app_name: str) -> str:
    return app(profile, app_name).get("url", "")


def app_sp(profile: str, app_name: str) -> str:
    return app(profile, app_name).get("service_principal_client_id", "")
