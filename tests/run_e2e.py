#!/usr/bin/env python3
"""FULL END-TO-END — one command that exercises the whole AIA PoC across BOTH deploy strategies and modes.

    python3 -m tests.run_e2e

It sequences the unified orchestrator (python -m tests.e2e.run_all) once per (config, mode) — it adds NO test logic
of its own; it just runs the combinations in order, each in its OWN environment, and prints one clear
PASS/FAIL matrix at the end. The deploy STRATEGY (dev vs cicd) is derived from each config; the SAME scenario
suite runs either way (mode-conditional only). The runs are:

    #  STRATEGY           MODE           WORKSPACE            CONFIG
    1  dev (manual)       in_process     staging workspace    config.stage-inproc.env
    2  dev (manual)       job            staging workspace    config.stage.env
    3  cicd → prod        in_process     prod workspace       config.prod-inproc.env
    4  cicd → prod        job            prod workspace       config.prod.env

WHY THIS SHAPE (see tests/README.md):
  * dev is the *shipped manual path* — a regular user deploys by hand (scripts/deploy.py) and runs
    investigations. We exercise it on STAGING, all modes.
  * cicd is the *CI/CD path* — GitHub Actions deploys to the PROD workspace as the CI service principal. We
    exercise it, all modes, targeting PROD.
  * Each mode gets a FRESH environment (its own TEST_SUFFIX → its own app / Lakebase / schema), so modes
    never share seeded state. The stage/prod config files are already split this way.

ENVIRONMENT POLICY (isolated + auto-teardown-on-success):
  * Before a run's scenarios, its env is deployed from scratch; after the run PASSES, we tear that env down
    immediately (frees cloud resources as we go). On the FIRST failure we STOP and LEAVE that environment
    live so you can inspect the broken state (its teardown command is printed). A fully green run leaves
    nothing behind.

Overrides (rarely needed): pass config filenames to replace the defaults, e.g.
    python3 -m tests.run_e2e --dev-inproc config.stage-inproc.env --cicd-job config.prod.env
    python3 -m tests.run_e2e --only 1,2          # run just rows 1 and 2
    python3 -m tests.run_e2e --keep              # never auto-teardown (leave every env up)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from tests.harness import config as _config

PY = sys.executable

C = {
    "bold": "\033[1m", "dim": "\033[2m", "green": "\033[32m", "red": "\033[31m",
    "yellow": "\033[33m", "cyan": "\033[36m", "reset": "\033[0m",
}


def c(s: str, color: str) -> str:
    return f"{C[color]}{s}{C['reset']}" if sys.stdout.isatty() else s


def banner(msg: str) -> None:
    bar = "═" * 72
    print(f"\n{c(bar, 'cyan')}\n  {c(msg, 'bold')}\n{c(bar, 'cyan')}")


# Optional private PyPI index for the wheel build. The build (`python -m build`) pip-installs hatchling in an
# ISOLATED env from PUBLIC PyPI; on a network where public PyPI is BLOCKED the deploy dies mid-build unless
# the build's pip points at a reachable index. If your network needs one, export PIP_PROXY=<index-url> (or set
# PIP_INDEX_URL directly). Left unset on a box with normal PyPI access — nothing to do.
PIP_PROXY = os.environ.get("PIP_PROXY", "")


def _ensure_pip_index() -> None:
    """Auto-set PIP_INDEX_URL for the wheel build so nobody has to remember the export. If the caller already
    set PIP_INDEX_URL, respect it. Else, if PIP_PROXY is configured, PROBE it: reachable → set it (child runs
    inherit via os.environ); unreachable/unset → leave unset (a box with real PyPI builds fine)."""
    if os.environ.get("PIP_INDEX_URL"):
        print(f"  {c('✓', 'green')} PIP_INDEX_URL already set: {os.environ['PIP_INDEX_URL']}")
        return
    if not PIP_PROXY:
        print(f"  {c('•', 'yellow')} PIP_INDEX_URL unset (no PIP_PROXY configured) — wheel build uses public PyPI")
        return
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(PIP_PROXY, timeout=3)
        reachable = True
    except urllib.error.HTTPError:
        reachable = True                # any HTTP response means the host is up (a 404/403 still proves reach)
    except Exception:                   # noqa: BLE001 — DNS/timeout/connection → not reachable
        reachable = False
    if reachable:
        os.environ["PIP_INDEX_URL"] = PIP_PROXY
        print(f"  {c('✓', 'green')} PIP_INDEX_URL auto-set from PIP_PROXY (wheel build)")
    else:
        print(f"  {c('•', 'yellow')} PIP_PROXY set but unreachable — wheel build will use public PyPI")


# One "run" = a (config, mode) pair. There are no layers — the deploy strategy (dev | cicd) is derived from
# the config, and run_all + teardown are the single tests/e2e/ pair.
class Run:
    def __init__(self, num: int, mode: str, config_file: str):
        self.num, self.mode, self.config_file = num, mode, config_file

    @property
    def label(self) -> str:
        return f"#{self.num} {self.mode} · {self.config_file}"

    def _env(self) -> dict:
        return {**os.environ, "AIA_CONFIG": self.config_file}

    def run_all_argv(self) -> list[str]:
        # ONE unified orchestrator, run as a module (repo root on sys.path). The deploy strategy (dev vs cicd)
        # is derived from the config AIA_CONFIG points at. It takes the mode; reads AIA_CONFIG for the rest.
        return [PY, "-m", "tests.e2e.run_all", self.mode]

    def execute(self) -> bool:
        banner(f"RUN {self.label}")
        rc = subprocess.run(self.run_all_argv(), env=self._env()).returncode
        return rc == 0

    def teardown(self) -> None:
        print(c(f"  tearing down {self.config_file} …", "dim"))
        subprocess.run([PY, "-m", "tests.e2e.teardown"], env=self._env())

    def teardown_cmd(self) -> str:
        return f"AIA_CONFIG={self.config_file} python3 -m tests.e2e.teardown"


# ── preflight ───────────────────────────────────────────────────────────────────────────────────────
def preflight(runs: list[Run]) -> None:
    """Fail fast BEFORE touching the cloud: every config must exist and carry the keys it needs, and any
    cicd-strategy config needs the gh CLI authenticated. A clear message here beats dying deep in a run."""
    banner("PREFLIGHT — configs + tools + auth")
    problems: list[str] = []
    need_gh = False
    admin_profiles: set[str] = set()
    aws_profiles: set[str] = set()

    # AIA_LLM_SECRET_ARN is intentionally NOT required: it is a DERIVED value that 00b_admin_aws_secret
    # writes back after creating the Secrets Manager secret. On a first-ever run it is empty, and requiring it
    # here would make the very first run (the one that exercises 00b) impossible. The inputs 00b needs to
    # create the secret (AIA_LLM_SECRET_NAME, AIA_LLM_IAM_ROLE, AWS_PROFILE_FOR_SETUP) are what matter, and
    # 00_admin_prereqs step 4 only rotates when the ARN is present (post-00b), so the ordering is safe.
    common_keys = ["ADMIN_PROFILE", "CATALOG", "SCHEMA", "LAKEBASE_PROJECT", "PG_DATABASE", "APP_NAME",
                   "WAREHOUSE_ID", "AIA_LLM_ENDPOINT_URL", "AIA_LLM_SERVICE_CREDENTIAL",
                   "AIA_LLM_SECRET_NAME", "AIA_LLM_IAM_ROLE", "AWS_PROFILE_FOR_SETUP"]
    for r in runs:
        os.environ["AIA_CONFIG"] = r.config_file
        try:
            cfg = _config.load()
        except SystemExit as e:
            problems.append(f"{r.config_file}: {e}")
            continue
        for k in common_keys:
            if not cfg.get(k):
                problems.append(f"{r.config_file}: missing {k}")
        if cfg.deploy_strategy == "cicd":
            need_gh = True
            if not cfg.get("GH_REPO"):
                problems.append(f"{r.config_file}: missing GH_REPO (cicd strategy needs it)")
        if cfg.get("ADMIN_PROFILE"):
            admin_profiles.add(cfg.get("ADMIN_PROFILE"))
        if cfg.get("AWS_PROFILE_FOR_SETUP"):
            aws_profiles.add(cfg.get("AWS_PROFILE_FOR_SETUP"))
        print(f"  {c('✓', 'green')} {r.config_file}  "
              f"(admin={cfg.get('ADMIN_PROFILE')}, target={cfg.bundle_target}, app={cfg.get('APP_NAME')})")
    os.environ.pop("AIA_CONFIG", None)

    if need_gh:
        rc = subprocess.run(["gh", "auth", "status"], capture_output=True).returncode
        if rc != 0:
            problems.append("gh is not authenticated (cicd strategy needs it) — run `gh auth login`")
        else:
            print(f"  {c('✓', 'green')} gh authenticated")

    # Workspace auth: the ADMIN_PROFILE(s) must be authed NOW (Databricks OAuth expires) — fail fast here
    # rather than dying deep in a run. (DEPLOYER/SEEDER profiles are synthesized per run, so not checked.)
    for prof in sorted(admin_profiles):
        if subprocess.run(["databricks", "current-user", "me", "-p", prof], capture_output=True).returncode == 0:
            print(f"  {c('✓', 'green')} databricks profile authed: {prof}")
        else:
            problems.append(f"databricks profile '{prof}' not authed — run `databricks auth login -p {prof}`")

    # AWS is needed only for FIRST-TIME LLM-credential provisioning; 00b is idempotent now (a re-run where the
    # UC credential already exists needs no AWS), so a missing AWS session is a WARNING, not a hard failure.
    for prof in sorted(aws_profiles):
        if subprocess.run(["aws", "sts", "get-caller-identity", "--profile", prof], capture_output=True).returncode == 0:
            print(f"  {c('✓', 'green')} AWS profile authed: {prof}")
        else:
            print(f"  {c('•', 'yellow')} AWS profile '{prof}' not authed — fine for a re-run (00b idempotent); "
                  f"first-time provisioning needs `aws sso login --profile {prof}`")

    _ensure_pip_index()

    if problems:
        print(c("\nPREFLIGHT FAILED:", "red"))
        for p in problems:
            print(f"  {c('✗', 'red')} {p}")
        sys.exit(2)
    print(c("\n  preflight OK", "green"))


# ── main ────────────────────────────────────────────────────────────────────────────────────────────
DEFAULTS = {
    "dev_inproc": "config.stage-inproc.env",
    "dev_job": "config.stage.env",
    "dev_warehouse": "config.stage-warehouse.env",
    "cicd_inproc": "config.prod-inproc.env",
    "cicd_job": "config.prod.env",
    "cicd_warehouse": "config.prod-warehouse.env",
}


def build_runs(args) -> list[Run]:
    plan = [
        Run(1, "in_process", args.dev_inproc),
        Run(2, "job", args.dev_job),
        Run(3, "job_warehouse", args.dev_warehouse),
        Run(4, "in_process", args.cicd_inproc),
        Run(5, "job", args.cicd_job),
        Run(6, "job_warehouse", args.cicd_warehouse),
    ]
    if args.only:
        want = {int(x) for x in args.only.split(",")}
        plan = [r for r in plan if r.num in want]
    return plan


def main() -> None:
    ap = argparse.ArgumentParser(description="Full AIA e2e (both layers, both modes).")
    for k, v in DEFAULTS.items():
        ap.add_argument(f"--{k.replace('_', '-')}", default=v, dest=k, help=f"config for that run (default {v})")
    ap.add_argument("--only", help="comma-separated run numbers to include, e.g. 1,2")
    ap.add_argument("--keep", action="store_true", help="never auto-teardown (leave every env up)")
    ap.add_argument("--preflight", action="store_true",
                    help="run ONLY the preflight checklist (configs, auth, tooling) and exit — touches no cloud")
    args = ap.parse_args()

    runs = build_runs(args)
    if args.preflight:
        preflight(runs)   # exits 2 on problems; reaching here means all green
        sys.exit(0)
    banner("AIA FULL END-TO-END")
    print("  plan:")
    for r in runs:
        print(f"    {r.label}")
    print(f"  teardown policy: {'KEEP all (no auto-teardown)' if args.keep else 'auto-teardown each run on PASS; stop+keep on FAIL'}")

    preflight(runs)

    results: list[tuple[Run, bool]] = []
    started = time.time()
    for r in runs:
        ok = r.execute()
        results.append((r, ok))
        if ok:
            print(c(f"\n  RUN {r.label} — PASSED", "green"))
            if not args.keep:
                r.teardown()
        else:
            print(c(f"\n  RUN {r.label} — FAILED", "red"))
            print(c(f"  STOPPING. Environment left live for inspection. Tear it down with:", "yellow"))
            print(f"    {r.teardown_cmd()}")
            break

    # ── final matrix ──
    banner("E2E SUMMARY")
    ran = {r.num for r, _ in results}
    for r in runs:
        if r.num not in ran:
            print(f"  {c('· SKIPPED', 'dim')}  {r.label}")
            continue
        ok = dict((rr.num, o) for rr, o in results)[r.num]
        tag = c("✓ PASS", "green") if ok else c("✗ FAIL", "red")
        print(f"  {tag}  {r.label}")
    elapsed = int(time.time() - started)
    all_ok = results and all(o for _, o in results) and len(results) == len(runs)
    print(f"\n  {len(results)}/{len(runs)} runs executed · {elapsed // 60}m{elapsed % 60}s")
    if all_ok:
        print(c("  ALL GREEN — full e2e passed; all environments torn down." if not args.keep
                else "  ALL GREEN — full e2e passed; environments left up (--keep).", "green"))
        sys.exit(0)
    print(c("  E2E FAILED — see the failing run above.", "red"))
    sys.exit(1)


if __name__ == "__main__":
    main()
