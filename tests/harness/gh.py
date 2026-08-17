"""GitHub CLI helpers for the CI/CD scenarios — thin wrappers over the `gh` CLI (there's no Python GitHub
dep here).
"""
from __future__ import annotations

import json
import subprocess


def latest_run_id(repo: str, workflow: str = "deploy.yml") -> str:
    """The newest workflow run id (empty string if none)."""
    cp = subprocess.run(
        ["gh", "run", "list", "--repo", repo, "--workflow", workflow,
         "--limit", "1", "--json", "databaseId", "-q", ".[0].databaseId"],
        capture_output=True, text=True)
    return (cp.stdout or "").strip()


def dispatch(repo: str, workflow: str = "deploy.yml", *, ref: str = "master", inputs: dict | None = None) -> bool:
    """Trigger a workflow via `gh workflow run` (workflow_dispatch). `inputs` become -f key=value flags.
    Returns True if the dispatch was accepted (poll wait_new_run_id for the run it creates)."""
    argv = ["gh", "workflow", "run", workflow, "--repo", repo, "--ref", ref]
    for k, v in (inputs or {}).items():
        argv += ["-f", f"{k}={v}"]
    return subprocess.run(argv, capture_output=True).returncode == 0


def wait_new_run_id(repo: str, prev_id: str, timeout: int = 90, interval: int = 3) -> str:
    """Poll until deploy.yml's newest run id differs from `prev_id` (the run our just-pushed commit triggered),
    or '' on timeout. Capture `prev_id = latest_run_id(repo)` BEFORE pushing. Necessary because a workflow
    takes a few seconds to register after a push — calling latest_run_id() immediately races it and returns the
    PREVIOUS run (so consecutive pushes, e.g. S03a src-only then S03b app-change, would both read the first
    run's job graph and assert against the wrong deploy)."""
    import time
    waited = 0
    while waited < timeout:
        cur = latest_run_id(repo)
        if cur and cur != prev_id:
            return cur
        time.sleep(interval)
        waited += interval
    return ""


def watch(repo: str, run_id: str) -> bool:
    """Block until the run finishes; return True iff it succeeded (--exit-status)."""
    return subprocess.run(["gh", "run", "watch", run_id, "--repo", repo, "--exit-status"],
                          capture_output=True).returncode == 0


def job_conclusion(repo: str, run_id: str, name_regex: str) -> str:
    """The conclusion of the first job whose name matches name_regex (case-insensitive); '' if none."""
    cp = subprocess.run(
        ["gh", "run", "view", run_id, "--repo", repo, "--json", "jobs",
         "-q", f'.jobs[] | select(.name|test("{name_regex}";"i")) | .conclusion'],
        capture_output=True, text=True)
    return (cp.stdout or "").strip().splitlines()[0] if (cp.stdout or "").strip() else ""


def step_conclusion(repo: str, run_id: str, name_regex: str) -> str:
    """The conclusion of the first STEP (across all jobs) whose name matches name_regex (case-insensitive);
    '' if no step matched. Use this for things the workflow does as a STEP rather than a separate job — e.g.
    the app restart is a step named "Restart the app (app/lib changed)" INSIDE the `deploy` job, so a
    job-name match (job_conclusion) would never find it and always return ''."""
    cp = subprocess.run(
        ["gh", "run", "view", run_id, "--repo", repo, "--json", "jobs",
         "-q", f'.jobs[].steps[] | select(.name|test("{name_regex}";"i")) | .conclusion'],
        capture_output=True, text=True)
    return (cp.stdout or "").strip().splitlines()[0] if (cp.stdout or "").strip() else ""


def push(remote: str, repo_root: str, *, empty: bool = False, paths: list[str] | None = None,
         message: str = "test") -> None:
    """Commit and push HEAD to master. Commit ONLY what the test intends:
      * empty=True         → an empty commit (used just to trigger a redeploy).
      * paths=[...]        → stage and commit EXACTLY those paths (the probe file a path-filter test touched).
    NEVER `git commit -am` (commit-all): a scenario running while the harness itself is being edited would
    otherwise sweep unrelated working-tree changes — and any test-only probe edits — into a commit pushed to
    master. Committing only the intended paths keeps the test's git footprint to exactly its probe."""
    if empty:
        subprocess.run(["git", "commit", "--allow-empty", "-m", message], cwd=repo_root,
                       capture_output=True, check=True)
    elif paths:
        subprocess.run(["git", "add", "--", *paths], cwd=repo_root, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", message, "--", *paths], cwd=repo_root,
                       capture_output=True, check=True)
    else:
        raise ValueError("gh.push needs empty=True or paths=[...] — refusing to commit-all")
    subprocess.run(["git", "push", remote, "HEAD:master"], cwd=repo_root, check=True)
