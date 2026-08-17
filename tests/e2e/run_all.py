#!/usr/bin/env python3
"""Unified e2e orchestrator — run the whole AIA flow for ONE (config, mode). There are no "layers": the
deploy STRATEGY (dev | cicd) comes from the config (cfg.deploy_strategy) and changes only HOW the deploy
happens + whether CI creds get pushed. Everything else — admin prereqs, seeder grant, seed, post-deploy
grants, and the scenario suite — is identical across strategies. The scenario set is purely mode-conditional.

    AIA_CONFIG=config.stage-inproc.env python3 -m tests.e2e.run_all in_process       # dev strategy
    AIA_CONFIG=config.prod-inproc.env  python3 -m tests.e2e.run_all in_process        # cicd strategy

Phases + scenarios are IMPORTED and their main() called directly — no subprocess. Per-phase state still flows
through the config file AIA_CONFIG points at (each main() reads/writes it), so any phase or scenario is still
independently runnable on its own. A strategy-aware restart callable is built once and INJECTED into every
scenario (applifecycle.make_restart). Phases stop on the first failure; scenarios are collected.
"""
import importlib
import sys
from tests.harness import applifecycle, config

# Phase modules (the tests.e2e package). `deploy`/`setup`/`seed` are aliased to avoid shadowing common names.
from tests.e2e import (
    admin_aws_secret,
    admin_prereqs,
    create_cicd_config,
    push_to_trigger_deploy,
    deploy as deploy_phase,
    setup as setup_phase,
    grant_seeder_lakebase,
    seed as seed_phase,
    admin_postdeploy,
)

MODES = ("in_process", "job", "job_warehouse")


def banner(msg: str) -> None:
    print("\n" + "═" * 66 + f"\n  {msg}\n" + "═" * 66)


def run_phase(title: str, fn, *args) -> None:
    """Call a phase's main(). A phase signals failure by sys.exit() (→ SystemExit) or by raising (e.g. a
    CalledProcessError from a shipped-script subprocess); either stops the run. Success = normal return or a
    zero exit."""
    banner(title)
    try:
        fn(*args)
    except SystemExit as e:
        if e.code:
            print(f"PHASE FAILED (exit {e.code}) — stopping.")
            sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"PHASE FAILED ({type(e).__name__}: {e}) — stopping.")
        sys.exit(1)


def phases_for(mode: str, strategy: str, remote: str):
    """Ordered phase list for a (mode, strategy). Each entry: (title, callable, *args)."""
    p = [
        ("admin_aws_secret (secret + UC credential + token rotation)", admin_aws_secret.main),
        ("admin_prereqs (role, seeder SP, job SP, deployer)", admin_prereqs.main, mode),
    ]
    if strategy == "cicd":
        # Push the deployer creds/vars to GitHub, then CI deploys prod AND provisions Lakebase +
        # build_structure in one run (targets=both) — so there is NO separate local setup phase for cicd.
        p += [
            ("create_cicd_config (push creds + prod vars to GitHub)", create_cicd_config.main, mode),
            ("push_to_trigger_deploy (trigger + watch the CI deploy)", push_to_trigger_deploy.main, remote),
        ]
    else:
        p += [
            ("deploy (local — scripts/deploy.py)", deploy_phase.main, mode),
            ("setup (Lakebase + build_structure)", setup_phase.main, mode),
        ]
    p += [
        ("grant_seeder_lakebase (deployer grants the seeder)", grant_seeder_lakebase.main),
        ("seed (seeder deploys + runs the demo/ bundle)", seed_phase.main, mode),
        ("admin_postdeploy (app SP → role membership + warehouse)", admin_postdeploy.main, mode),
    ]
    return p


def scenarios_for(mode: str):
    """ONE mode-conditional scenario set for BOTH strategies. Returns [(name, main_callable), …]."""
    names = ["s01_happy_investigation"]
    names += (["s02_recover_in_process", "s03_attempts_cap"] if mode == "in_process"
              else ["s04_recover_job", "s07_restart_during_job"])
    names += ["s05_concurrency", "s06_reconcile_noop", "s08_sp_boundary"]
    return [(n, importlib.import_module(f"tests.e2e.scenarios.{n}").main) for n in names]


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in MODES:
        sys.exit("usage: python -m tests.e2e.run_all <in_process|job|job_warehouse> [git-remote]")
    mode = sys.argv[1]
    remote = sys.argv[2] if len(sys.argv) > 2 else "origin"

    cfg = config.load()
    strategy = cfg.deploy_strategy
    banner(f"AIA e2e · mode={mode} · strategy={strategy} · target={cfg.bundle_target} "
           f"· admin={cfg.get('ADMIN_PROFILE')} · deployer={cfg.get('DEPLOYER_PROFILE') or '<synth>'}")

    for entry in phases_for(mode, strategy, remote):
        run_phase(entry[0], entry[1], *entry[2:])

    # ONE restart callable — built after the phases (so the profiles it needs exist) and injected into every
    # scenario. Reload config: the phases wrote DEPLOYER_PROFILE / APP_NAME / etc. back to it.
    restart = applifecycle.make_restart(config.load())

    banner(f"SCENARIOS (mode={mode}, strategy={strategy})")
    failed: list[str] = []
    for name, fn in scenarios_for(mode):
        print(f"\n── {name} ──")
        try:
            fn(mode, restart)
        except SystemExit as e:
            if e.code:
                failed.append(name)
        except Exception as e:  # noqa: BLE001 — a scenario blew up unexpectedly; record + keep going
            print(f"  {name} raised {type(e).__name__}: {e}")
            failed.append(name)

    banner(f"SUMMARY (mode={mode}, strategy={strategy})")
    if not failed:
        print("  ALL SCENARIOS PASSED")
    else:
        print(f"  FAILED SCENARIOS: {' '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
