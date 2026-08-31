"""AIA app · runtime config — the ONE place the app parses its AIA_* environment.

The DAB app resource injects a fixed set of AIA_* env vars (all static, all known before deploy — see
databricks.yml). `Config.from_env()` reads + normalizes them once into a frozen, typed value object, so the
rest of the app can read `cfg.catalog` instead of scattering `os.environ` lookups — and it's unit-testable
(pass a dict).

The three values that only exist AFTER deploy — the app SP id, the Lakebase host/endpoint, the investigate
job id — are deliberately NOT here; they're resolved lazily at runtime (see lib/resolve.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Config:
    catalog: str                 # AIA_CATALOG          — Delta catalog (substrate + tools)
    schema: str                  # AIA_SCHEMA           — schema (Delta + the Lakebase schema of the same name)
    project: str                 # AIA_LAKEBASE_PROJECT — host/endpoint are derived from it
    branch: str                  # AIA_LAKEBASE_BRANCH
    endpoint: str                # AIA_LAKEBASE_ENDPOINT
    pg_database: str             # AIA_PG_DATABASE      — the Lakebase database holding cases/investigations
    investigate_job_name: str    # AIA_INVESTIGATE_JOB_NAME — its id is resolved by name at first use
    app_name: str                # AIA_APP_NAME         — the app's own resource name (optional)
    warehouse_id: str            # AIA_WAREHOUSE_ID     — tools run here (in_process + job_warehouse; unused in `job`)
    agent_mode: str              # AIA_AGENT_MODE       — job_warehouse (default) | job | in_process
    job_sp: str                  # AIA_JOB_SP           — job modes: the job SP's client id (audit)
    max_attempts: int            # AIA_MAX_ATTEMPTS     — reconcile re-fire cap before needs_review
    journal_poll_seconds: int    # AIA_JOURNAL_POLL_SECONDS — job-mode journal apply cadence

    JOB_MODES = ("job", "job_warehouse")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        """Parse the app's config from the environment (defaults to os.environ). Required vars raise
        KeyError if missing; optional ones fall back. Pass a dict to test."""
        e = os.environ if env is None else env
        job_sp = e.get("AIA_JOB_SP", "").strip()
        if job_sp == "none":   # in_process sentinel — Apps env vars can't be empty (see databricks.yml)
            job_sp = ""
        return cls(
            catalog=e["AIA_CATALOG"],
            schema=e["AIA_SCHEMA"],
            project=e["AIA_LAKEBASE_PROJECT"],
            branch=e["AIA_LAKEBASE_BRANCH"],
            endpoint=e["AIA_LAKEBASE_ENDPOINT"],
            pg_database=e["AIA_PG_DATABASE"],
            investigate_job_name=e["AIA_INVESTIGATE_JOB_NAME"],
            app_name=e.get("AIA_APP_NAME", "").strip(),
            warehouse_id=e.get("AIA_WAREHOUSE_ID", "").strip(),
            agent_mode=e.get("AIA_AGENT_MODE", "job_warehouse").strip().lower(),
            job_sp=job_sp,
            max_attempts=int(e.get("AIA_MAX_ATTEMPTS", "3")),
            journal_poll_seconds=int(e.get("AIA_JOURNAL_POLL_SECONDS", "10")),
        )

    @property
    def is_job(self) -> bool:
        """`job` and `job_warehouse` both drive the investigate job; only in_process runs tools in-app."""
        return self.agent_mode in self.JOB_MODES
