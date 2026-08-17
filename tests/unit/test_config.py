"""Tier 1 — app.config.Config.from_env: env parsing, defaults, normalization."""
import pytest

from app.config import Config

_REQUIRED = {
    "AIA_CATALOG": "cat",
    "AIA_SCHEMA": "sch",
    "AIA_LAKEBASE_PROJECT": "proj",
    "AIA_LAKEBASE_BRANCH": "production",
    "AIA_LAKEBASE_ENDPOINT": "primary",
    "AIA_PG_DATABASE": "aia_stage",
    "AIA_INVESTIGATE_JOB_NAME": "[stage] AIA · Investigate",
}


def test_required_fields_and_defaults():
    cfg = Config.from_env(dict(_REQUIRED))
    assert (cfg.catalog, cfg.schema, cfg.pg_database) == ("cat", "sch", "aia_stage")
    # optionals fall back to their documented defaults
    assert cfg.agent_mode == "job_warehouse"   # the default mode
    assert cfg.max_attempts == 3
    assert cfg.journal_poll_seconds == 10
    assert cfg.app_name == "" and cfg.warehouse_id == "" and cfg.job_sp == ""


def test_missing_required_raises():
    incomplete = {k: v for k, v in _REQUIRED.items() if k != "AIA_CATALOG"}
    with pytest.raises(KeyError):
        Config.from_env(incomplete)


def test_normalization_and_job_sp_sentinel():
    env = dict(_REQUIRED, AIA_AGENT_MODE="  JOB  ", AIA_JOB_SP="none", AIA_WAREHOUSE_ID=" wh ")
    cfg = Config.from_env(env)
    assert cfg.agent_mode == "job"        # stripped + lowercased
    assert cfg.job_sp == ""               # "none" in_process sentinel → ""
    assert cfg.warehouse_id == "wh"       # stripped


def test_is_job_across_modes():
    def mode(m):
        return Config.from_env(dict(_REQUIRED, AIA_AGENT_MODE=m))
    assert mode("job").is_job and mode("job_warehouse").is_job
    assert not mode("in_process").is_job


def test_frozen():
    cfg = Config.from_env(dict(_REQUIRED))
    with pytest.raises(Exception):   # FrozenInstanceError
        cfg.catalog = "other"        # type: ignore[misc]
