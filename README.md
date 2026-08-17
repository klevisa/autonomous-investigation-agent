# AIA — Autonomous Investigation Agent

AIA investigates **medium**-severity security cases and flags the ones that are actually **high** and should
be escalated. It runs as a **Databricks App**: a FastAPI service with an API (called from **Tines**), a live
UI, its state in **Lakebase**, and its evidence gathered through **governed Unity Catalog tools** and a
**gateway LLM**. Deploys go to production on **merge to `master`**.

The app is the **orchestrator and the state owner**. When a case comes in, it opens an investigation, runs a
bounded tool-calling loop where the LLM decides which evidence to pull, records a verdict, and rolls the case
up. *Where* that investigation computes is a mode switch (see [How it works](#3-how-it-works)); the app is
always in charge of state.

```
  Tines ──POST /api/investigations {case_id}──▶  Databricks App  ──▶  live UI (board + drill-down)
                                                 orchestrator +
                                                 state owner
                                                      │
                       ┌──────────────────────────────┼──────────────────────────────┐
                       ▼                               ▼                               ▼
               Lakebase                     the investigation runs            LLM: AI Gateway
               cases · investigations       (in-process, or a fired           (token from AWS
               · journal  (state)           investigate job) — calling        Secrets Manager)
                                            5 governed UC tools over
                                            Delta evidence, via a SQL
                                            warehouse or the job's Spark

  Packaged as a Databricks Asset Bundle · shipped to production on merge to master (CI/CD).
```

This is a **template** — nothing is hardcoded to a workspace. You supply every value. The bundled demo data is
for evaluation only; in production it is replaced by AIA's real evidence tables and live Tines cases.

---

**Contents**

1. [Getting started (staging)](#1-getting-started-staging) — stand it up and drive it
2. [Going to production with CI/CD](#2-going-to-production-with-cicd)
3. [How it works](#3-how-it-works) — the flow, the components, the three modes
4. [Identity & permissions](#4-identity--permissions) — the security model, in depth
5. [Reference](#5-reference) — config keys, grants by stage, layout

---

## 1. Getting started (staging)

This walks through standing AIA up in a **staging** workspace — one you deploy to by hand — running in
**`job_warehouse` mode**, the default (see [the three
modes](#the-three-modes-where-an-investigation-runs) for why). Production is the same code deployed by CI;
that's [section 2](#2-going-to-production-with-cicd).

By the end you'll have: the app running, its Lakebase state provisioned, and investigations executing on a
durable job that reaches the tools through a SQL warehouse.

### What you need first

**Local tooling** (on the machine you deploy from — the scripts fail fast if one is missing):

| Tool | Why |
|---|---|
| **Databricks CLI** (v0.230+) | every workspace call — `bundle deploy/run`, apps, Lakebase, grants |
| **`python3`** (3.9+) with `databricks-sdk` + `pyyaml` | the `scripts/*.py` deploy/setup recipes |
| **`python3 -m build`** | builds the `aia_lib` wheel at deploy (`pip install build`) |
| **`git`** | the repo is deployed via CI on merge to `master` |

You do **not** install the app's runtime deps (`fastapi`, `pg8000`, …) — the Databricks Apps runtime does.

### The one-time admin setup

AIA's security rests on account-level identity work a regular deployer can't do — in production your organization's
platform admin does it once; for staging, an admin does the same. It's scripted end-to-end in
`tests/e2e/admin_prereqs.py` if you want a working reference (note that file also creates test-only
scaffolding, like a separate seeder SP, that real AIA doesn't need). The checklist for `job_warehouse`:

1. **Create the AIA role** — an account group — and assign it to the workspace. *(admin)*
2. **Grant the role its data access** — `USE CATALOG` on the catalog, `USE SCHEMA` + `SELECT` on the evidence
   schema, and `EXECUTE` on the tool functions. This is done by the **catalog/schema owner** (AIA owns the
   evidence), not the admin. No warehouse grant on the role.
3. **Create the job service principal**, entitle it (`workspace-access`), assign it to the workspace, and
   **add it as a member of the AIA role** — so it inherits step 2's grants. *(admin)*
4. **Grant the job SP `CAN_USE` on the SQL warehouse** — directly on the SP, since it runs the tools there.
   *(admin)*
5. **Put the LLM gateway token in AWS Secrets Manager** and create a Unity Catalog **service credential**
   wrapping the IAM role that reads it; grant the job SP **`ACCESS`** on that credential. *(admin)*
6. **Grant the deployer `servicePrincipal.user` on the job SP** — so the deploy can set it as the job's
   `run_as`. *(admin)* Grants are read from your token, so if you were just given this, start a fresh CLI
   login before deploying so your token reflects it.

(In production these target the CI service principal instead — see [section 2](#2-going-to-production-with-cicd).
`in_process` mode instead adds the *app* SP to the role + warehouse + credential, but only *after* deploy, since
the app SP is born then — see the [grants-by-stage summary](#grants-by-stage-summary).)

Everything after this — deploy, Lakebase, table structure, app↔job wiring — is done by you as a **regular
deployer**, because that's who this ships to.

### Step 1 — fill in `config.yml`

`config.yml` is the single input, and it's gitignored (the repo is a template). **Copy the checked-in
[`config.yml.example`](config.yml.example) to `config.yml`** and fill the `stage` target's variables:

```yaml
targets:
  stage:
    variables:
      catalog:            your_catalog        # UC catalog holding the evidence tables + tool functions
      schema:             aia_stage          # the evidence schema (look up; reused as the Lakebase schema name)
      warehouse_id:       <sql-warehouse-id>  # tools run here (job_warehouse); `databricks warehouses list`
      lakebase_project:   aia-lakebase       # created for you; holds cases/investigations
      pg_database:        aia_stage          # Lakebase database name (use a distinct name per environment)
      agent_mode:         job_warehouse
      job_sp:             <job-sp-client-id>  # the pre-provisioned job SP (member of the role)
      llm_endpoint_url:            <gateway-invocations-url>
      llm_service_credential:      <uc-service-credential-name>
      llm_secret_arn:              <aws-secrets-manager-arn>
```

You supply *names you choose* (project, database) and *ids/names you look up* (catalog, schema,
warehouse, job SP). There's no state infrastructure to stand up by hand — the setup script creates the Lakebase
project, endpoint, and database from these names. Full key reference: [section 5](#config-keys).

### Step 2 — deploy the code

```bash
PROFILE=<your-cli-profile> TARGET=stage python3 scripts/deploy.py
```

This runs `bundle deploy` (creating the `investigate` job and the app — **the app's service principal is born
here**) and starts the app. The app comes up **healthy immediately**, but its data pages show an error until
step 3 provisions Lakebase — it resolves and connects lazily on first use, so no restart is ever needed.

### Step 3 — provision state and wire the job

```bash
PROFILE=<your-cli-profile> TARGET=stage python3 scripts/setup.py
```

This is a readable, idempotent recipe (open it — it's top-to-bottom SDK calls). It:

1. provisions the **Lakebase** project + scale-to-zero endpoint + database;
2. runs `build_structure` to create the `cases`, `investigations`, and `investigation_events` (journal) tables
   and grant the app SP read/write on state and the job SP `INSERT`-only on the journal;
3. wires the app→job path: the app SP gets `CAN_MANAGE_RUN` on the `investigate` job, and the job SP gets
   `CAN_READ` on the deployed notebook it executes.

`setup.py` never deploys code, so it's safe to run against a CI-locked production too (see
[section 2](#2-going-to-production-with-cicd)).

### Step 4 (optional) — load demo data

To see the agent work end-to-end on sample data, deploy and run the **separate `demo/` bundle**, which seeds a
throwaway evidence substrate (threat-intel tables + the 5 tools) and 25 medium cases. In staging you run it as
**yourself** — the same identity that deployed and ran setup, which owns the schema and the Lakebase tables, so
it can create the substrate and insert cases:

```bash
cd demo && databricks bundle deploy -t stage -p <profile> && databricks bundle run seed_demo_data -t stage -p <profile>
```

This is **evaluation only**. In production AIA's real evidence and tools already exist, and cases arrive from
Tines — you never run the seed.

### Step 5 — drive it

Open the app (URL in the Databricks Apps UI) and click **Investigate**, or call the API the way Tines will:

```bash
curl -X POST "$APP_URL/api/investigations" -H 'Content-Type: application/json' -d '{"case_id":"CASE-0001"}'
# -> {"investigation_id": "...", "status": "investigation_started"}
```

The case flips to **investigating** on the board; when the investigation finishes, the drill-down shows the
assessed severity, an escalate-to-high banner where warranted, the recommended containment play, the agent's
rationale, and the full evidence trail (every tool call and result). Fire several at once — they run
concurrently.

### Troubleshooting

- **The app's data pages show an error right after deploy.** Expected until step 3 — the app resolves Lakebase
  lazily. Run `setup.py`; the next request works, no restart needed.
- **`bundle deploy` is rejected setting `run_as`.** The deployer needs `servicePrincipal.user` on the job SP
  (admin checklist step 6). If it was just granted, your current token may predate it — start a fresh CLI
  login so the grant is in your token.
- **The job dies with "Unable to access the notebook…".** The job SP needs `CAN_READ` on the bundle files;
  `setup.py` grants it. Re-run setup if you redeployed to a new location.
- **`python3 -m build` not found at deploy.** `pip install build` — the deploy builds the `aia_lib` wheel.
- **Tool queries fail with "permission denied".** The tool-runner isn't inheriting the role: confirm the job
  SP (or the app SP, in `in_process`) is a *member* of the AIA role and the role holds `SELECT`/`EXECUTE`.

---

## 2. Going to production with CI/CD

Production is **the same bundle, same `scripts`**, with two differences: **who deploys** (a CI service
principal, not you) and **when** (through GitHub Actions, not by hand). The security setup is identical.

### The deployment split (why it's safe)

AIA separates the deploy into two halves that run on different triggers:

- **Code** — the `investigate` job and the app. `bundle deploy`, run automatically on merge to `master`.
- **State + infrastructure** — Lakebase, the table structure, the grants. `scripts/setup.py`, run **only on an
  explicit `workflow_dispatch`, never on a push.**

Lakebase holds live data, and `bundle deploy`/`destroy` reconcile the whole bundle every run — so keeping the
datastore setup off the push trigger means a merge can never touch production cases. `setup.py` is idempotent
and never deploys code, so it's safe to run against a CI-locked production.

### Step 1 — admin prereqs, targeting the CI SP (once)

The same [account-level checklist](#the-one-time-admin-setup) as staging, but every grant that targeted *you*
now targets the **CI service principal** — notably `servicePrincipal.user` on the job SP. The CI SP also needs
an **OAuth M2M secret** (its deploy credential, used in step 2). The job SP's role membership and
LLM-credential `ACCESS` are unchanged — those always target the *job* SP.

### Step 2 — push credentials + prod values to GitHub (once)

```bash
REPO=<owner/repo> \
  DBX_HOST=<workspace-url> DBX_CLIENT_ID=<ci-sp-app-id> DBX_CLIENT_SECRET=<ci-sp-oauth-secret> \
  CATALOG=<cat> SCHEMA=<schema> WAREHOUSE_ID=<wh> PROJECT=<lakebase-project> PG_DATABASE=<pg-db> \
  AGENT_MODE=job_warehouse JOB_SP=<job-sp-client-id> \
  LLM_ENDPOINT_URL=<url> LLM_SERVICE_CREDENTIAL=<cred> LLM_SECRET_ARN=<arn> \
  python3 scripts/setup_cicd.py <owner/repo>
```

Sets the three Actions **secrets** (the CI SP's `DATABRICKS_HOST`/`CLIENT_ID`/`CLIENT_SECRET` — the M2M
credentials the workflow authenticates with) and the `PROD_*` repo **variables** (prod's single source of
truth; the workflow assembles the prod `config.yml` from them). Needs `gh` authenticated to the repo owner.

### Step 3 — deploy code (merge to `master`)

Open a PR and merge. On merge, `.github/workflows/deploy.yml` runs `bundle deploy -t prod` as the CI SP,
**path-filtered**: an `app/` or `lib/` change redeploys and restarts the app; a `src/` change refreshes the job
only. PRs run `bundle validate`. This trigger **never** touches Lakebase, structure, or secrets.

### Step 4 — provision prod state (once, via dispatch)

Standing prod up the first time — deploy the code, then run setup — is a single dispatch:

```bash
gh workflow run deploy.yml --ref master -f targets=both      # deploy code, then Lakebase + build_structure
```

`targets` is the control: `code` = `bundle deploy` only · `lakebase` = `scripts/setup.py` only (provision +
build_structure + app↔job wiring) · `both` = deploy then setup. Setup runs **only** through this dispatch —
never on a push — so a merge can't touch live data. After first stand-up, code changes just merge (step 3);
re-run `-f targets=lakebase` only if the schema or app identity changes.

Because deploy is final (post-deploy values are [resolved at runtime](#runtime-resolution), not baked in),
there's no "edit config and redeploy" second pass.

---

## 3. How it works

### One investigation, end to end

```
Tines ──POST /api/investigations {case_id}──▶ Databricks App (FastAPI, runs as the App SP)
                                                │  opens an investigation → INSERT investigations(running); case → investigating
                                                │  returns {investigation_id, status} immediately
                                                ▼
                                         the investigation runs (see modes below)
                                                │  bounded tool-calling loop: the LLM picks which of the 5 governed
                                                │  UC-function tools to call to gather evidence, then decides a verdict
                                                ▼
                                         record_verdict → writes verdict to Lakebase + rolls up the case
                                                │  (+ TODO: push the verdict back to Tines)
                                                ▼
                                         App UI: live board + investigation drill-down
```

The agent core is **pure** — given a case, the tools, and an LLM it returns a verdict and touches no state.
`record_verdict` is the single completion contract. That purity is what lets all three modes share one code
path and differ only in *where tools run* and *how the verdict gets home*.

**The evidence tools.** Five governed Unity Catalog functions, called as `catalog.schema.<tool>(arg)`:
`enrich_indicator` (threat-intel verdict for a URL/IP/hash), `pivot_indicator` (indicator → campaign / actor /
siblings), `blast_radius` (which internal accounts saw the indicator), `get_account_risk`, and
`get_account_actions`. The LLM is reached through the **AI Gateway** — an explicit endpoint URL plus a
bearer token read from AWS Secrets Manager (not FMAPI).

### The components

| Component | Role |
|---|---|
| **Databricks App** (`app/`) | The orchestrator + state owner. FastAPI: the Tines API, the UI, startup reconcile, and (job modes) the journal poll. Always running, always the app SP. |
| **Lakebase** | The operational state: `cases` (the Tines mirror), `investigations` (append-only agent output), and `investigation_events` (the job→app journal). OLTP — point reads for the UI, safe concurrent writes. |
| **Unity Catalog / Delta** | The evidence substrate + the 5 tool functions. Governed and lineage-tracked. AIA's own tables in prod; the demo substrate in evaluation. |
| **`investigate` job** (`src/investigate.py`) | The durable runner used by the two job modes. Runs as the job SP; reports its verdict through the journal. Idle in `in_process`. |
| **SQL warehouse** | Where tools are queried in `in_process` and `job_warehouse` modes. Unused in `job` mode (which uses the job's Spark session). |

### The three modes (where an investigation runs)

The app is always the orchestrator and state owner. What changes is where the investigation *computes* and how
its verdict comes back:

| | **`job_warehouse`** (default) | **`job`** | **`in_process`** |
|---|---|---|---|
| Runs on | the investigate job (**warehouse**, Spark idle) | the investigate job's **Spark session** | the app's background thread |
| Tool-runner identity | the **Job SP** | the **Job SP** | the **App SP** |
| Tools reached via | SQL warehouse | job's own Spark | SQL warehouse |
| Verdict → state | **journal** → app applies it | **journal** → app applies it | **directly** (same process) |
| Recovery | reconcile via `job_run_id` + journal poll | same as `job_warehouse` | startup reconcile re-runs orphans |
| Best when | the default — durable, no per-case Spark spin-up | you want the investigation on Spark | quick loops inside the app's uptime |

All three reach the tools **as a member of the AIA role** — the job SP (both job modes) or the app SP
(`in_process`) — inheriting the role's grants with no token to mint and no secret. The difference between the
two job modes is a **single branch inside the investigate notebook**: `job_warehouse` builds a
`WarehouseSqlRunner` and leaves Spark idle; `job` builds a `SparkSqlRunner` on the job's Spark session. From the
app's side the two are identical — same dispatch, same journal, same reconcile.

`job_warehouse` is the default because it combines the platform-supervised durability of a job with the fast
warehouse tool access of `in_process`, without paying Spark startup per case. Set the mode with the
`agent_mode` config value.

### Durability — a queue plus reconcile

Durability here is a **state** property, not a compute one. Every open investigation is a `status='running'`
row — a durable queue. If the app or a run dies mid-investigation, the row remains, and the app reconciles it:

- **`in_process`** — the orphaned row's worker thread died with the process (the only way an in_process
  investigation is orphaned is a process death, which *is* a restart), so startup reconcile re-runs it.
- **job modes** — the platform guarantees the job reaches a terminal state, and the job writes its verdict to
  the append-only **journal independently of the app**. A continuous poll applies terminal events; startup
  reconcile is the backstop for a `running` row with no event yet — it checks the recorded `job_run_id` against
  the Jobs API and re-fires only if the run is genuinely gone.

This is **at-least-once** (a re-run redoes the whole investigation — fresh LLM + tool cost), so it's safe to
repeat: a fresh run re-queries and re-decides, and the write is an idempotent upsert. An `attempts` cap parks a
crash-looping case as `needs_review` rather than retrying forever.

### The journal (job modes) — a one-directional handoff

In job modes the job never calls the app. It **appends** to the `investigation_events` table — `started`, then
`completed` (with the full verdict) or `failed` — and the app is the sole reader, folding pending events into
`investigations` via the same `record_verdict`. Authenticity rides on `{{job.run_id}}`: the platform injects it
and a job cannot forge it, the app recorded that id when it dispatched the run, and it applies an event only if
the run ids match (enforced in SQL, so a forged or replayed event simply never selects). The job's *entire*
state-store surface is `INSERT` on that one table — no read of case data, no state access, no permission on the
app at all. No token, no shared secret, no callback.

### Tuning the job (retries & timeout)

Two bundle vars bound each job-mode run, covering different failures:

- **`investigate_max_retries`** (default `2`) → the task's `max_retries`. A *transient* failure (an LLM 503, a
  tool timeout) re-runs the same job run; `{{job.run_id}}` is stable across retries, so authenticity and
  reconcile are unaffected. The notebook appends a `failed` event only on the final attempt, so a
  fail-then-succeed run records exactly one `completed`.
- **`investigate_timeout_seconds`** (default `1800`, per attempt) → the task's `timeout_seconds` with
  `retry_on_timeout`. **This is the one that matters:** without it a *wedged* attempt (a hung LLM call) sits
  `RUNNING` forever — never `FAILED`, so retries never fire. With it, the platform times the stuck attempt out
  and retries. The whole-run bound is roughly `(max_retries + 1) × investigate_timeout_seconds`.

These are separate from the app's reconcile cap (`AIA_MAX_ATTEMPTS`, default 3), which bounds how many times an
*orphaned* run is re-fired before the case is parked `needs_review`. Job retries handle "the attempt failed but
compute survived"; reconcile handles "the whole run or the app died."

---

## 4. Identity & permissions

Two rules drive the whole design:

1. **One data-access grant surface.** All evidence and tools are granted to a single **RBAC role**; the
   runners get that access *only* by **membership**. Grants live in one place; every tool query is attributable
   to a role member.
2. **No stored secrets.** Exactly one secret exists (the LLM gateway token). Everything else is injected by a
   runtime, minted short-lived, or attested by the platform.

### The four identities

| Identity | What it is | Can | Cannot |
|---|---|---|---|
| **Deployer** | A regular, non-admin user (staging) or the **CI SP** (prod). Ships the code. | `bundle deploy`; provision Lakebase; create the Lakebase tables (→ owns them); own the bundle files. | Touch the role group or create account principals (that's admin work). |
| **App SP** | Born when the app deploys. **The state owner.** | Sole writer of `cases` + `investigations`; trigger the investigate job; (`in_process` only) run the tools as a role member. | Reach evidence by any path **but role membership** — it has no data grants of its own. In `in_process` it reads the tools/evidence *via the role*; in job modes it isn't a role member and touches no evidence at all. |
| **Job SP** | Pre-allocated by admin; the investigate job's `run_as`. **Minimal privilege.** | Run tools via role membership (warehouse or Spark); `INSERT` on the journal; read its own notebook. | Read/write `cases`/`investigations`; even `SELECT` its own journal events; anything on the app. |
| **AIA role** | An **account group** used as an RBAC role. | Holds `USE CATALOG` / `USE SCHEMA` / `SELECT` / `EXECUTE` on the evidence schema + tools. | It's not a login — grants are **inherited by members**. Warehouse `CAN_USE` goes to the SPs directly, not the role. |

The membership model is deliberate: one place to grant/revoke, symmetry between the modes (both runners are
just members), and no `client_credentials`+assume-role token mint and no secret needed to reach data. The
trade-off — accepted — is that queries audit to the member SP, not the role.

### Who grants what, and when (`job_warehouse`)

| # | Grant | On | Granted by | Kind | When |
|---|---|---|---|---|---|
| 1 | Create the AIA role (account group) + assign to workspace | account SCIM | Platform admin | admin | pre-deploy |
| 2 | Role: `USE CATALOG` / `USE SCHEMA` / `SELECT` / `EXECUTE` | evidence catalog + schema + tools | **Catalog/schema owner (AIA)** | owner | pre-deploy |
| 3 | Create the job SP; add as a **member** of the role | account SCIM | Platform admin | admin | pre-deploy |
| 4 | Job SP: `CAN_USE` on the warehouse (direct, not via the role) | warehouse | Platform admin | admin | pre-deploy |
| 5 | Job SP: `ACCESS` on the LLM service credential | UC credential | Platform admin | admin | pre-deploy |
| 6 | Deployer: `servicePrincipal.user` on the job SP (to set `run_as`) | rule-set | Platform admin | admin | pre-deploy |
| 7 | `bundle deploy` — the job + the app (**app SP born here**) | bundle | Deployer | self | deploy |
| 8 | App SP: RW on `cases`+`investigations`; Job SP: `INSERT`-only on the journal | Lakebase | Deployer (table owner) | owner | setup |
| 9 | App SP: `CAN_MANAGE_RUN` on the investigate job | job ACL | Deployer (job owner) | owner | setup |
| 10 | Job SP: `CAN_READ` on the bundle files dir (to read its notebook) | workspace ACL | Deployer (file owner) | owner | setup |

The clean split: **admin** does the account-level identity work (create the role, group membership, rule-sets,
warehouse grant); the **catalog/schema owner** (AIA) grants the role its data access; and the **deployer**
does everything that only touches resources it created or owns (Lakebase tables, the job's ACL, the bundle
files) — no admin required. Grants 8–10 are additive and survive redeploys; never express them as a bundle
`permissions` block (that block is authoritative and resets ACLs on every deploy).

### No stored secrets

| Credential | How it's obtained | Stored? |
|---|---|---|
| App SP OAuth id/secret | injected by the Databricks Apps runtime | No |
| Lakebase access | OAuth token minted **per connection** (~1h) for the holder | No |
| Job identity | `run_as` + platform-injected `{{job.run_id}}` | No — none at all |
| **LLM gateway token** | **AWS Secrets Manager**, read via a UC **service credential** (fresh STS per call) | **The only one** — and never in Databricks |

The LLM surface: only the **token** is secret. The gateway URL and the credential/ARN names are plain config
vars (`llm_endpoint_url`, `llm_service_credential`, `llm_secret_arn`, …). At runtime `lib/llm.py` exchanges the
service credential for short-lived AWS STS creds and calls `GetSecretValue` — so the tool-runner needs exactly
`ACCESS` on the service credential, nothing more. (For a laptop run, set `llm_endpoint_url` + `AIA_LLM_TOKEN`
and it skips AWS entirely.)

### How the app SP reaches its Lakebase tables

There's no explicit "this app owns that database role" pointer — the link is a **matching name**. The app SP's
`service_principal_client_id` is (1) injected as `DATABRICKS_CLIENT_ID` and used as the Lakebase **username**
(password = a fresh OAuth token per connection), (2) the in-database **name** of the Lakebase role
`build_structure` creates for it, and (3) the target of the `GRANT`. So when the app connects, Lakebase matches
its login name to the role of the same name and it inherits the grants. One value the app knows about itself is
the entire link — which is why the app SP id isn't a config variable.

### Grants by stage (summary)

The [detailed table](#who-grants-what-and-when-job_warehouse) above is organized by grant; this is the same
picture organized by **stage**, and shows the one difference between the modes — *when* the tool-runner gets
into the role:

| Stage | Who | What |
|---|---|---|
| **Pre-deploy** | Platform admin + catalog/schema owner | create the AIA role + grant it the evidence `SELECT`/`EXECUTE`; **[job modes]** create the job SP, add it to the role, warehouse `CAN_USE`, LLM-credential `ACCESS`, and `servicePrincipal.user` to the deployer; put the LLM token in AWS + create the service credential |
| **Deploy** | Deployer / CI SP | `bundle deploy` — the job + the app (the app SP is born here) |
| **Setup** | Deployer (as owner) | Lakebase grants (app SP RW, job SP journal `INSERT`); app SP `CAN_MANAGE_RUN` on the job; job SP `CAN_READ` on the files |
| **Post-deploy** | Platform admin | **[`in_process` only]** add the *app* SP to the role + warehouse `CAN_USE` + LLM-credential `ACCESS` (deferred to here because the app SP doesn't exist until deploy) |

So `job_warehouse`/`job` do all their identity grants **pre-deploy** (the job SP exists ahead of time), while
`in_process` shifts the tool-runner grants to **post-deploy** (the app SP is born at deploy). Everything else is
identical.

---

## 5. Reference

### Config keys

Set under `targets.<env>.variables` in `config.yml` (staging, hand-written from `config.yml.example`) or as
`PROD_*` GitHub variables (prod).

| Key | Choose / look up | What it controls |
|---|---|---|
| `catalog` | look up | UC catalog holding the tool functions + evidence tables; grant target for the role. |
| `schema` | look up | The evidence schema (`catalog.schema` — tools + evidence). It may already exist; if not, the demo seed can create it. The **same name** is reused for the Lakebase schema holding `cases`/`investigations` (which `build_structure` creates). |
| `warehouse_id` | look up | The SQL warehouse tools run on (`in_process` and `job_warehouse`). |
| `lakebase_project` | choose | Lakebase project id (created for you); branch/endpoint default to `production`/`primary`. |
| `pg_database` | choose | Lakebase database name — **use a distinct name per environment** (`aia_stage` vs `aia_prod`). |
| `agent_mode` | choose | `job_warehouse` (default) \| `job` \| `in_process`. |
| `job_sp` | look up | The job SP client id (job modes only) — a member of the role, `INSERT`-only on the journal. |
| `llm_endpoint_url`, `llm_service_credential`, `llm_secret_arn`, `llm_secret_region`, `llm_secret_json_key` | look up | The AI Gateway endpoint + the AWS-Secrets-Manager-via-UC-credential token wiring. |
| `investigate_max_retries` | optional | Job modes: task `max_retries` on a transient failure. Default `2`. See [Tuning the job](#tuning-the-job-retries--timeout). |
| `investigate_timeout_seconds` | optional | Job modes: per-attempt wall-clock bound (`timeout_seconds` + `retry_on_timeout`). Default `1800`. |
| `app_name` | optional | The Databricks App resource name. Default `aia-investigation-app`. |

### Runtime resolution

Three values only exist *after* the first deploy, so instead of baking them in (which would force a
redeploy), the app resolves them lazily at first use from the static names above (`lib/resolve.py`): the **app
SP id** (from the injected `DATABRICKS_CLIENT_ID`), the **Lakebase host + endpoint** (from `lakebase_project`),
and the **investigate job id** (from the job's name). This is why one `bundle deploy` is final and `setup.py`
finishes the environment without touching the app.

### Layout

```
databricks.yml            the bundle: build_structure + investigate jobs, the aia_app, the aia_lib wheel
config.yml.example        the template for config.yml (the single per-env input; config.yml is gitignored)
app/                      the FastAPI app — orchestrator + state owner
  main.py                 routes (Tines API + UI), startup reconcile, journal poll
  investigations.py       in_process runner, job trigger, journal apply, reconcile
  ui.py                   the board + drill-down HTML
lib/                      shared core, delivered to the app from source and to the jobs as the aia_lib wheel
  investigator.py         the AIA agent: prompt, plays, verdict parsing (pure — no state, no Spark)
  tool_wielding_agent.py  the generic, provider-agnostic tool-calling loop
  tools.py                SqlRunner (Warehouse vs Spark) + the UC-function tool adapter
  state_store.py          cases/investigations on Lakebase + record_verdict + reconcile (app only)
  journal.py              the append-only job→app verdict handoff; authenticity via {{job.run_id}}
  pg.py                   Lakebase connection (pg8000 + per-connection OAuth)
  resolve.py              runtime resolution (app SP id, Lakebase host/endpoint, job id)
  llm.py                  the AI Gateway client (token from AWS Secrets Manager)
src/
  build_structure.py      creates the Lakebase tables + grants (the permanent structure layer)
  investigate.py          the job driver (job modes): tools on Spark or the warehouse → journal
scripts/                  readable deploy/setup recipes over the databricks_ops SDK helpers
  deploy.py               bundle deploy + start the app (CI does this in prod)
  setup.py                Lakebase + build_structure + app↔job wiring (once per env, dispatch-only in CI)
  setup_cicd.py           push CI credentials + PROD_* values to GitHub
databricks_ops/           the SDK-typed functions the scripts call (Lakebase, groups, grants, config)
demo/                     the SEPARATE, evaluation-only demo bundle (substrate + tools + 25 cases)
.github/workflows/        CI/CD — code deploy on merge, Lakebase setup via workflow_dispatch
tests/                    unit + integration tests and the end-to-end harness (an executable reference)
```
