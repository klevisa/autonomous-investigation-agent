# The e2e flow (one flow, two strategies)

`run_all.py` runs the whole thing for a `(config, mode)`. The deploy **strategy** — `dev` | `cicd` — comes
from the config (`cfg.deploy_strategy`: explicit `DEPLOY_STRATEGY`, else prod→cicd). It changes only the
**bolded** phases below; everything else is identical.

```bash
AIA_CONFIG=config.stage-inproc.env python3 -m tests.e2e.run_all in_process   # dev
AIA_CONFIG=config.prod-inproc.env  python3 -m tests.e2e.run_all in_process   # cicd
AIA_CONFIG=config.stage.env        python3 -m tests.e2e.teardown             # tear down
```

`run_all.py` **imports** each phase/scenario module and calls its `main()` directly (no subprocess); per-phase
state (synthesized profiles, discovered ids) flows through the config file, so every phase + scenario is also
runnable on its own (`python3 -m tests.e2e.<name> …`).

## Phases (in order)

| # | Phase (module) | Identity | What |
|---|---|---|---|
| 1 | `admin_aws_secret` | admin | AWS Secrets Manager secret + UC service credential + **rotate** a fresh gateway token in |
| 2 | `admin_prereqs` | admin | create the AIA role, the **seeder SP** (+ its CLI profile), (job) the job SP + membership + LLM `ACCESS` + (job_warehouse) warehouse `CAN_USE`; synth the deployer SP + `servicePrincipal.user` |
| 3a | **`create_cicd_config`** *(cicd only)* | admin + gh | push the deployer creds + `PROD_*` vars to GitHub |
| 3b | **`push_to_trigger_deploy`** *(cicd)* | CI SP | `workflow_dispatch` deploy.yml (targets=both) + watch — CI deploys **and** provisions Lakebase + build_structure |
| 3c | **`deploy`** *(dev)* | deployer | `scripts/deploy.py` — bundle deploy + start the app |
| 3d | **`setup`** *(dev)* | deployer | `scripts/setup.py` — Lakebase provision + build_structure |
| 4 | `grant_seeder_lakebase` | deployer (owner) | grant the seeder SP `USAGE` + `SELECT/INSERT/DELETE` on `cases` + `DELETE` on `investigations` |
| 5 | `seed` | **seeder** | deploy + run the separate `demo/` bundle (its own wheel) → Delta substrate + tools + 25 demo cases |
| 6 | `admin_postdeploy` | admin | in_process: **add the app SP to the role** + warehouse `CAN_USE` + LLM `ACCESS`; job: no-op |

## Scenarios (mode-conditional — the SAME set for both strategies)

The restart in the recovery/restart scenarios is **injected** and strategy-aware (`applifecycle.make_restart`):
dev = local `bundle deploy` + `run`; cicd = a real CI redeploy (`workflow_dispatch targets=code`).

| Scenario | Mode | Proves |
|---|---|---|
| `s01_happy_investigation` | both | end-to-end investigation → verdict + case rollup; a tool query ran (member SP → warehouse) |
| `s02_recover_in_process` | in_process | orphaned `running` row + **restart** → startup reconcile re-runs it, attempts bumped |
| `s03_attempts_cap` | in_process | orphan already at the cap + restart → reconcile **abandons** it (case → `needs_review`) |
| `s04_recover_job` | job, job_warehouse | the 5-case journal reconcile matrix (apply / re-fire±count / abandon / reject-forgery) |
| `s07_restart_during_job` | job, job_warehouse | a **restart** during a real job run doesn't kill the job; verdict still lands via the journal |
| `s05_concurrency` | both | N investigations fired back-to-back all complete |
| `s06_reconcile_noop` | both | restart with no `running` rows leaves terminal rows untouched + app healthy |
| `s08_sp_boundary` | both | the deployer SP (synth in dev, CI SP in cicd) is **not** a `group.manager` — can't escalate into the role |

Full matrix (all configs × modes, auto-teardown-on-pass): `python3 -m tests.run_e2e` — see `tests/README.md`.
```
