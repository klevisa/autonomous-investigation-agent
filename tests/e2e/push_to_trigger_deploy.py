#!/usr/bin/env python3
"""Trigger the prod CI/CD deploy via `workflow_dispatch` and watch it to completion.

    python3 -m tests.e2e.push_to_trigger_deploy

We trigger via `gh workflow run deploy.yml -f targets=both` (the workflow's documented manual trigger),
NOT a git push, because:
  * DETERMINISTIC + REPEATABLE — dispatch fires a run every time, regardless of whether HEAD differs from
    remote main. A `git push HEAD:main` is a no-op when nothing is unpushed → no workflow → the run
    stalls, so the suite could only ever run once per new commit. Dispatch also avoids junk commits on main.
  * The run builds off the dispatched ref's current HEAD, so it deploys exactly the committed code. The ref
    defaults to main; set DEPLOY_REF (config.env) to validate a feature branch (e.g. mcp) through the real CI
    path without merging first — deploy.yml is dispatchable off any ref because it also lives on main.

`targets=both` makes CI do the WHOLE prod bring-up in one run: `bundle deploy` (jobs + app) AND the setup
job (provision Lakebase + build_structure), all as the CI SP (which owns the prod project). We deliberately
do NOT run scripts/setup.py locally afterward: that path needs a local prod config.yml (the L2 flow keeps prod
values in GitHub repo VARIABLES, not a local file), and CI's own setup job already does the identical
provision+build_structure from those variables. So Lakebase exists as soon as this step returns.
(In_process still needs the ADMIN to add the CI-deployed app SP to the role afterward — the post-deploy grants.)

A legacy `remote` positional arg is accepted but ignored (kept so run_all.py's call site is unchanged).
"""
import subprocess
import sys
from pathlib import Path

from tests.harness import config, report, waiters

REPO_ROOT = Path(__file__).resolve().parents[2]


def _latest_run_id(repo: str) -> str:
    cp = subprocess.run(
        ["gh", "run", "list", "--repo", repo, "--workflow", "deploy.yml",
         "--limit", "1", "--json", "databaseId", "-q", ".[0].databaseId"],
        capture_output=True, text=True)
    return (cp.stdout or "").strip()


def _dispatch_and_watch(repo: str, ref: str = "main", attempts: int = 2) -> bool:
    """workflow_dispatch deploy.yml (targets=both → deploy + Lakebase provision + build_structure) and watch
    to completion; retry up to `attempts` times on failure. The CI run occasionally dies on a genuinely
    transient server-side blip (observed: `bundle run aia_app` → SCIM `GET /Me` returns 500 SCIM_500), which
    a plain re-dispatch clears — a repeatable multi-run e2e must not die on a one-off 5xx. `ref` is the branch
    CI builds off (default main; set DEPLOY_REF to validate a feature branch). Returns True once a run
    succeeds, False if all attempts fail."""
    for i in range(1, attempts + 1):
        prev = _latest_run_id(repo)   # newest run id BEFORE dispatch → poll for the run WE create
        disp = subprocess.run(
            ["gh", "workflow", "run", "deploy.yml", "--repo", repo, "--ref", ref, "-f", "targets=both"],
            capture_output=True, text=True)
        if disp.returncode != 0:
            print(f"  dispatch attempt {i} failed to submit: {disp.stderr.strip() or disp.stdout.strip()}")
            continue
        print(f"  dispatched deploy.yml (attempt {i}/{attempts}, ref={ref}, targets=both)")
        rid = waiters.wait_value("a new deploy.yml run to appear", 90,
                                 lambda: (lambda c: c if (c and c != prev) else None)(_latest_run_id(repo)),
                                 interval=3)
        if not rid:
            print("  no new workflow run appeared — did the dispatch register?")
            continue
        print(f"  run id: {rid} — watching…")
        if subprocess.run(["gh", "run", "watch", rid, "--repo", repo, "--exit-status"]).returncode == 0:
            return True
        print(f"  CI run {rid} FAILED (attempt {i}/{attempts})"
              + ("; retrying (transient?)" if i < attempts else ""))
    return False


def main(remote: str) -> None:
    cfg = config.load()
    repo = cfg.require("GH_REPO")
    # DEPLOY_REF lets the cicd suite build off a FEATURE BRANCH (e.g. mcp) rather than main, so a branch can
    # be validated through the real CI path without merging first. deploy.yml is dispatchable off any ref
    # because it also lives on main (the default branch); the run then builds that ref's HEAD. Defaults to main.
    ref = cfg.get("DEPLOY_REF") or "main"
    if subprocess.run(["gh", "--version"], capture_output=True).returncode != 0:
        sys.exit("need gh")

    report.step(f"trigger the prod CI deploy via workflow_dispatch (deploy.yml, targets=both, ref={ref}) + watch")
    if not _dispatch_and_watch(repo, ref, attempts=2):
        sys.exit("CI deploy FAILED after retry — inspect: gh run view <id> --repo %s --log" % repo)
    print("  CI deploy + Lakebase setup SUCCEEDED (CI did deploy + provision + build_structure)")

    print("\nDeployed via CI. If in_process, now grant the app SP Assume + LLM ACCESS: "
          "python3 -m tests.e2e.admin_postdeploy in_process")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "origin")
