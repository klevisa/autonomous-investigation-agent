"""Tier 1 — the CI/CD path-filter routing (.github/workflows/deploy.yml).

deploy.yml uses dorny/paths-filter to decide, on a push, whether a change is app-affecting (→ deploy AND
restart the app) or jobs-only (→ deploy, NO app restart — restarting would kill in-flight in_process
investigations). That routing is DETERMINISTIC config, so we assert it here in the fast suite by reading the
workflow directly — no git push, no probe commits, no workspace. (This replaces the push-based Layer-2 S03
"src-only change" assertion, which committed throwaway `.ci-probe` markers to master to exercise the same
logic. The one thing a config test can't cover — that `bundle deploy` itself doesn't bounce the app — is a
separate, deferred live check; see memory/harness-improvements.)
"""
import pathlib

import pytest
import yaml

DEPLOY_YML = pathlib.Path(__file__).resolve().parents[2] / ".github/workflows/deploy.yml"


def _load_filters():
    """Pull the app/jobs glob lists out of the dorny/paths-filter step in the `changes` job."""
    doc = yaml.safe_load(DEPLOY_YML.read_text())
    steps = doc["jobs"]["changes"]["steps"]
    filter_step = next(s for s in steps if s.get("id") == "filter")
    return yaml.safe_load(filter_step["with"]["filters"])   # {"app": [...globs], "jobs": [...globs]}


def _matches(glob: str, path: str) -> bool:
    # deploy.yml uses only two glob shapes: 'dir/**' (prefix) and exact filenames.
    return path.startswith(glob[:-2]) if glob.endswith("/**") else path == glob


def _routes(path: str):
    filters = _load_filters()
    return {name: any(_matches(g, path) for g in globs) for name, globs in filters.items()}


def _restarts_app(path: str) -> bool:
    # deploy.yml's decide step: the app restart runs iff the `app` filter matched.
    return _routes(path)["app"]


# (changed path, app-affecting?, jobs-affecting?)
CASES = [
    ("app/main.py",            True,  False),
    ("app/ui.py",              True,  False),
    ("requirements.txt",       True,  False),
    ("src/investigate.py",  False, True),
    ("seed/accounts.csv",      False, True),
    ("lib/tools.py",           True,  True),   # lib triggers BOTH (app + jobs import it)
    ("databricks.yml",         True,  True),
    ("README.md",              False, False),  # no deploy at all
    ("docs/security-model.md", False, False),
]


@pytest.mark.parametrize("path,app,jobs", CASES)
def test_path_routing(path, app, jobs):
    r = _routes(path)
    assert r["app"] == app
    assert r["jobs"] == jobs


def test_src_only_change_does_not_restart_app():
    # the core guarantee: a notebook/job-only change must NOT restart the app
    assert _restarts_app("src/investigate.py") is False


def test_app_change_restarts_app():
    assert _restarts_app("app/main.py") is True


def test_lib_change_restarts_app_and_refreshes_jobs():
    # lib/ is imported by both, so it must trigger both paths
    assert _routes("lib/investigator.py") == {"app": True, "jobs": True}
