"""App restart, STRATEGY-AWARE — used by the recovery/restart scenarios to replace the process that owned
in-flight work, so the fresh process's startup reconcile is exercised.

run_all builds ONE restart callable via `make_restart(cfg)` and injects it into each scenario's
`main(mode, restart)`; a scenario just calls `restart()` where it needs a process replacement. So scenarios
are strategy-agnostic — this module is the only place that knows dev vs cicd. Two implementations, picked by
`cfg.deploy_strategy`:

  * dev  — `bundle run aia_app` on the already-deployed app. This is an APP restart, matching what a prod
           app recycle does: `bundle run` performs an app deployment (re-uploads the app source + recycles the
           app process, re-applying its command+env from the DAB `config` block), so the fresh process's
           lifespan → startup reconcile runs. Crucially it restarts ONLY the app resource — it does NOT
           `bundle deploy`, which would redeploy the investigate JOB and kill an in-flight job run (the whole
           point of s07 is that a job on its own compute SURVIVES an app restart). No `apps stop` first: a
           STOPPED bundle-managed app comes back "No command to run" → FAILED (verified); `bundle run`
           restarts a running app in place.
  * cicd — trigger a REAL CI redeploy via `gh workflow_dispatch (targets=code)` and watch it. The prod bundle
           state is CI-owned/remote, so a LOCAL `bundle deploy` would hit a state-lineage conflict; the CI
           deploy is the correct restart there. A dispatched CODE deploy fires deploy.yml's "Restart the app"
           step (an empty commit would not — the path filter sees no app change).

EITHER WAY, restart() does not return until the app is HEALTHY *and* SERVING its data route again
(`_wait_app_serving`). This matters most for cicd: `gh watch` returns at WORKFLOW completion, but the app
container then cold-starts and re-resolves Lakebase LAZILY on first request — so a scenario reading app data
immediately after restart() would otherwise race that (s06 hit exactly this: /healthz was 200 but /api/cases
still errored). The dev `bundle run` already blocks until the app is running; the explicit serve-wait makes
both paths uniform: when restart() returns, the app answers /api/cases.
"""
import time

from . import appapi, dbx, gh, report


def _wait_app_serving(deployer: str, app_name: str, timeout: int = 240) -> None:
    """Block until the app is healthy AND its data route actually serves — container up + Lakebase re-resolved.
    A restart() must not return before this, or a scenario reading app data right after races the post-restart
    cold start. Best-effort: on timeout, warn and return (the scenario's own assert is the real signal)."""
    app = appapi.AppClient(deployer, app_name)
    app.wait_healthy(timeout)                       # container up (/healthz 200)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = app.get("/api/cases")            # the real data route — forces the lazy Lakebase resolve
        except Exception:  # noqa: BLE001 — non-JSON/transient body during the cold start → treat as not-ready
            resp = {"error": "not ready"}
        if isinstance(resp, dict) and "error" not in resp:
            return
        time.sleep(5)
    report.step(f"  (app still not serving /api/cases {timeout}s after restart — continuing to the assert)")


def make_restart(cfg):
    """Return a zero-arg `restart()` that replaces the app process the strategy-appropriate way (module doc)
    and returns only once the app is serving again. Captures what it needs from cfg at build time."""
    app_name = cfg.require("APP_NAME")
    deployer = cfg.require("DEPLOYER_PROFILE")   # also used to poll the app's health/data route after restart

    if cfg.deploy_strategy == "cicd":
        repo = cfg.require("GH_REPO")

        def _do() -> None:
            report.step(f"restart {app_name} via a REAL CI redeploy (workflow_dispatch, targets=code)")
            prev = gh.latest_run_id(repo)                # capture BEFORE dispatch so we watch OUR run
            gh.dispatch(repo, "deploy.yml", ref="master", inputs={"targets": "code"})
            rid = gh.wait_new_run_id(repo, prev)
            if rid:
                gh.watch(repo, rid)
            else:
                report.step("  (no new deploy.yml run appeared — continuing to check recovery)")
    else:
        target = cfg.bundle_target

        def _do() -> None:
            report.step(f"restart {app_name} via bundle run aia_app (app-only recycle; does NOT redeploy the job)")
            # Best-effort (check=False): the scenario asserts the OUTCOME, not the CLI exit code. Only the app
            # resource — no `bundle deploy`, no `apps stop` — see the module doc.
            dbx.bundle(deployer, "run", "aia_app", target=target, check=False)

    def restart() -> None:
        _do()
        _wait_app_serving(deployer, app_name)   # return only once the app is healthy AND serving data again

    return restart
