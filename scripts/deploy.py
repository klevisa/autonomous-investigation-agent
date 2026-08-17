#!/usr/bin/env python3
"""Deploy the bundle CODE (jobs + app) and start/restart the app. This is the deploy ACTION — separate from
the post-deploy setup (scripts/setup.py). Used two ways:
  * staging: an engineer runs it by hand (manual deploy is allowed in stage),
  * prod:    CI/CD does the equivalent on merge to master (.github/workflows/deploy.yml).

All per-env values come from config.yml (the DAB reads it natively via `include`), so this only needs a
PROFILE + TARGET — no --var, no env vars for the values. It does NOT provision Lakebase, build structure, or
grant the app SP — that's setup.py, run AFTER this.

    PROFILE=<cli-profile> TARGET=stage python3 scripts/deploy.py
    python3 scripts/deploy.py --profile <cli-profile> --target stage

App health note: the app comes up healthy immediately, but its data routes render an error page until
setup.py has provisioned Lakebase + granted the app SP (the app resolves + connects LAZILY on first request
— see lib/resolve.py). No restart needed once setup.py runs.
"""
from __future__ import annotations

import argparse
import os
import sys

from bundle import run_bundle   # sibling module in scripts/ (this file is run as `python3 scripts/deploy.py`)


def main() -> None:
    ap = argparse.ArgumentParser(description="Deploy the AIA bundle (jobs + app) and start/restart the app.")
    ap.add_argument("--profile", default=os.environ.get("PROFILE"))
    ap.add_argument("--target", default=os.environ.get("TARGET"))
    a = ap.parse_args()
    if not a.target:
        sys.exit("set TARGET (stage|prod) via --target or the TARGET env var")

    print(f"=== Deploy the bundle (jobs + app) — target={a.target}, values from config.yml ===")
    run_bundle(["deploy"], a.profile, a.target)

    print("=== Start/restart the app ===")
    run_bundle(["run", "aia_app"], a.profile, a.target)

    print(f"Deployed to {a.target}. Next: run scripts/setup.py (post-deploy — Lakebase + structure + grants).")


if __name__ == "__main__":
    main()
