# AIA PoC — Test Suites

A self-contained e2e harness that exercises the built system end-to-end. Everything reads **one config file**
(`tests/config/config.env`, selected via `$AIA_CONFIG`) and runs from **scripts in this tree** — nothing
depends on notes, memory, or prior context. If you can fill in a config file, you can run these.

```
tests/
  harness/                importable Python helpers (config, asserts, dbx/app/Lakebase state, app restart, config.yml renderer)
  config/                 the config files you fill (config.*.env)
    config.env.example      → copy to config.*.env and fill in (the ONLY thing you edit)
  e2e/                    the ONE e2e flow — phase modules + scenarios/ + run_all.py + teardown.py
  unit/                   Tier 1 — pure/offline unit tests
  integration/            Tier 2 — ephemeral local Postgres (no Docker)
  run_e2e.py              the matrix runner (all configs × modes, auto-teardown-on-pass)
```

There are **no "layers."** One flow runs for a `(config, mode)`; the **deploy strategy** — `dev` or `cicd` —
is derived from the config (`DEPLOY_STRATEGY`, else prod→cicd) and changes only **how deploy happens** and
whether CI creds get pushed. Everything else — admin prereqs, seed, post-deploy grants, and the scenario
suite — is identical. See `tests/e2e/README.md` for the phase + scenario tables.

## The identity boundary (read this first)

The security model has **one data-access grant surface: a AIA RBAC role** (an account group). Setting it up
requires **account-admin** actions. In production **your org's admin** does these once; **we do the same in
the test** using the admin profile. Everything *after* the prereqs runs as a **regular deployer**, because
that regular deployer is who we ship this code to.

| Done by **ADMIN** (out of band) | Done by the **regular DEPLOYER** (the shipped path) |
|---|---|
| Create the role group (account group) + grant it evidence `SELECT` / tool `EXECUTE` | `scripts/deploy.py` — bundle deploy + start the app |
| Create the **seeder SP** (test-only) + its direct `CREATE_*` grants | `scripts/setup.py` — Lakebase + build_structure |
| (job) create the job SP, **add it to the role**, `ACCESS` on the LLM credential, warehouse `CAN_USE` (job_warehouse) | Run investigations (Tines-style API calls) |
| **post-deploy**: **add the app SP to the role** (in_process) + warehouse `CAN_USE` + LLM `ACCESS` | Restart / redeploy, observe recovery |
| **post-deploy showcase (all modes)**: provision the external-MCP **UC HTTP Connection + MCP Service** (a teaching layer) — create the external-MCP client SP, embed it in the connection with `CAN_USE` on the app, grant the agent caller `EXECUTE` on the service — `tests/harness/mcp.py` | — |
| (cicd) create the CI SP + push GitHub secrets/variables | Open a PR, merge, watch CI deploy |

`config.env` names the profiles: `ADMIN_PROFILE`, `DEPLOYER_PROFILE`, `SEEDER_PROFILE`. Each phase uses the
correct one explicitly; the harness never silently falls back to admin.

## The membership model (what the tests prove about identity)

Both tool-runners — the **app SP** (in_process) and the **job SP** (job) — are **members** of the AIA role
and inherit its grants; there is no role assumption and no minted token. `s08_sp_boundary` proves the
deployer SP (synth in dev, CI SP in cicd) is **not** a `group.manager`, so it can't add itself/the app SP to
the role and escalate to AIA's data. See the README "Identity & permissions".

## Prerequisites

- Databricks CLI, `python3`. For the `cicd` strategy also `gh` (authenticated) + `git`.
- The **RBAC preview enabled** at account + workspace level.
- The LLM gateway token in **AWS Secrets Manager** + a UC **service credential** wrapping the IAM role that
  reads it (`admin_aws_secret` provisions + rotates it; the other phases don't).
- `ADMIN_PROFILE` can perform account-group + rule-set + SP operations; `DEPLOYER_PROFILE` is a plain user.

## How to run

### Full matrix (one command)

```bash
python3 -m tests.run_e2e
```

Runs every `(config, mode)` in order, each in its own fresh environment, and prints one PASS/FAIL matrix:

| # | Strategy | Mode | Workspace | Config |
|---|----------|------|-----------|--------|
| 1 | dev (manual) | in_process | staging | `config.stage-inproc.env` |
| 2 | dev (manual) | job | staging | `config.stage.env` |
| 3 | dev (manual) | job_warehouse | staging | `config.stage-warehouse.env` |
| 4 | cicd → prod | in_process | prod | `config.prod-inproc.env` |
| 5 | cicd → prod | job | prod | `config.prod.env` |
| 6 | cicd → prod | job_warehouse | prod | `config.prod-warehouse.env` |

`job_warehouse` is the product's **default** agent mode (see `databricks.yml`) — the job SP runs tools
against the SQL warehouse (via the Statement Execution API) rather than plain `job`'s ambient Spark.

It **auto-tears-down each run once it passes**, and on the first failure **stops and leaves that environment
live** (printing its teardown command). Flags: `--only 1,2`, `--keep`, and `--dev-inproc / --dev-job /
--dev-warehouse / --cicd-inproc / --cicd-job / --cicd-warehouse` to override a config file.
`python3 -m tests.run_e2e --help` for details.

### One run at a time

```bash
AIA_CONFIG=config.stage-inproc.env python3 -m tests.e2e.run_all in_process   # dev strategy (stage)
AIA_CONFIG=config.prod-inproc.env  python3 -m tests.e2e.run_all in_process   # cicd strategy (prod)
AIA_CONFIG=config.stage.env        python3 -m tests.e2e.teardown             # tear an env down
```

`run_all.py` imports each phase + scenario and calls it directly (no subprocess), stopping on the first phase
failure and collecting scenario failures. Every phase (`admin_prereqs`, `deploy`, `setup`, …) and every
scenario (`s01_…`) is also runnable on its own. See `tests/e2e/README.md` for the full tables.

## What the e2e proves

- **Functional (both strategies):** both agent modes investigate correctly (tools run as a role member);
  **recovery** works — an app restart mid-investigation is reconciled (in_process re-runs; job reconciles via
  `job_run_id`); the attempts cap parks a crash-looping case; concurrent investigations all complete; and the
  job's Spark run **survives** an app restart (`s07`).
- **cicd-specific:** GitHub Actions deploys prod as the CI SP, and the deployer-SP permission boundary holds
  (`s08`). The restart-based scenarios use a **real CI redeploy** to trigger the restart under cicd (the
  injected restart is strategy-aware).
```
