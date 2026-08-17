#!/usr/bin/env python3
"""Shared `databricks bundle …` runner for the deploy/setup entrypoints (scripts/deploy.py, scripts/setup.py).
Both drive the bundle from the repo root with the same -t/-p handling; this keeps that in one place."""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_bundle(args: list[str], profile: str | None, target: str, capture: bool = False):
    """Run a `databricks bundle …` command from the repo root, adding -t/-p (omit -p under env-var auth).
    Returns the CompletedProcess; pass capture=True to capture stdout (e.g. `bundle summary -o json`)."""
    cmd = ["databricks", "bundle", *args, "-t", target] + (["-p", profile] if profile else [])
    return subprocess.run(cmd, cwd=str(REPO_ROOT), check=True, capture_output=capture, text=True)
