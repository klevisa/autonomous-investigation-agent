#!/usr/bin/env python3
"""The regular USER deploys (as DEPLOYER_PROFILE). Renders config.yml from config.env
for the chosen mode, then runs the SHIPPED scripts/deploy.py. Births the app SP.

    python3 -m tests.e2e.deploy <in_process|job>

(config.yml is rendered from config.env by harness.render_config; then the SHIPPED scripts/deploy.py — the
product's deliberate deploy recipe — is driven as the deployer, and we verify the app SP resolved. We keep
scripts/deploy.py as a subprocess on purpose: the e2e exercises the shipped entrypoint exactly as a user does.)
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from tests.harness import config, dbx, report, render_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def main(mode: str) -> None:
    cfg = config.load()
    deployer = cfg.require("DEPLOYER_PROFILE")
    target = cfg.bundle_target

    report.step(f"render config.yml (mode={mode}) from config.env")
    render_config.render(cfg, mode)
    print("  config.yml written")

    # `.databricks/bundle/<target>/` is a local Terraform-state CACHE; we drop it before EACH deploy attempt
    # (see the loop). Reasons: a FRESH deployer each run means a per-deployer remote root, so a leftover local
    # dir mismatches lineage ("lineage mismatch in state files"); and a prior FAILED attempt can leave partial
    # local state. A clean drop forces a re-sync from (this deployer's) remote so every attempt is a true
    # clean deploy.
    local_state = REPO_ROOT / ".databricks" / "bundle" / target

    report.step(f"deploy as the regular user ({deployer}) via the shipped scripts/deploy.py")
    # Retry the shipped deploy on failure. The sandbox control plane trips it intermittently in several
    # independent ways — 504 stream-timeout on file upload, `workspace/get-status` 60s timeout on app-source
    # download, or `bundle run` transiently failing to resolve a just-deployed resource — and these can hit
    # back-to-back, so give it a few clean tries (mirrors the cicd path's push_to_trigger_deploy retry).
    # `bundle deploy` + the app run are idempotent. Bounded: a persistent failure still raises so a real error
    # stays loud rather than being masked.
    attempts = 3
    for i in range(1, attempts + 1):
        # Clear stale local state ONLY before the FIRST attempt (drop a PRIOR run's state — see the note
        # above). Do NOT clear between retries: terraform tracks what it created in the failed attempt (e.g. an
        # app that was created but didn't finish going active), so KEEPING the state lets the retry UPDATE +
        # re-wait. Clearing it here would make terraform forget the app and try to CREATE it again → "failed to
        # create app: already exists" (which is exactly what a per-attempt clear caused).
        if i == 1 and local_state.exists():
            shutil.rmtree(local_state)
        cp = subprocess.run(["python3", "scripts/deploy.py"], cwd=str(REPO_ROOT),
                            env={**os.environ, "PROFILE": deployer, "TARGET": target})
        if cp.returncode == 0:
            break
        if i < attempts:
            # Explicit, intentional backoff between retries: an IMMEDIATE re-try just hits the same
            # in-flight control-plane blip (504 upload / get-status timeout). Space attempts out so a
            # transient clears before we try again. (The only wait the harness adds here, and it's on the
            # failure path only — the shipped deploy itself owns its own readiness waits.)
            backoff = 10 * i
            report.step(f"  deploy attempt {i}/{attempts} failed (rc={cp.returncode}); "
                        f"retrying in {backoff}s (transient?)")
            time.sleep(backoff)
        else:
            sys.exit(f"shipped deploy.py failed after {attempts} attempts (rc={cp.returncode})")

    report.step("verify the app came up + expose its SP (needed for the admin post-deploy grants)")
    app_name = cfg.require("APP_NAME")
    sp = dbx.app_sp(deployer, app_name)
    url = dbx.app_url(deployer, app_name)
    print(f"  app SP = {sp}")
    print(f"  app URL = {url}")
    if not sp:
        sys.exit("FAIL: could not resolve the app SP — did deploy.py succeed?")
    print(f"\nDeployed (mode={mode}). Next: ADMIN runs 02_admin_postdeploy.py")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("in_process", "job", "job_warehouse"):
        sys.exit("usage: python -m tests.e2e.deploy <in_process|job>")
    main(sys.argv[1])
