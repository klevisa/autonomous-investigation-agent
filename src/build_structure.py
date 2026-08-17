# Databricks notebook source
# MAGIC %md
# MAGIC # Build STRUCTURE: the Lakebase operational tables (the ONLY thing setup builds)
# MAGIC Creates the **`cases` + `investigations`** tables in **Lakebase Postgres** — the operational store
# MAGIC the app owns. That's it. This is the one piece of structure that exists in EVERY environment
# MAGIC (prod included), because it's *our* schema, not AIA's data.
# MAGIC
# MAGIC Everything else — the Delta schema, the threat-intel substrate tables, and the 5 UC-function
# MAGIC tools — is **DEMO** and lives in `seed_demo_data`. In production AIA already has their evidence
# MAGIC tables + tools; they just grant the agent SP access to them (the agent's tool list dictates which
# MAGIC tables an investigation needs). So prod runs THIS notebook and skips the seed entirely.
# MAGIC
# MAGIC Runs as the SETUP identity (which created the Lakebase project, so it owns these tables and can
# MAGIC later grant the app SP access — done in this same notebook below). Re-running is safe (IF NOT EXISTS).
# MAGIC The tables go in a Postgres schema named after the Delta `schema` var (not `public`).

# COMMAND ----------
# MAGIC %pip install -q pg8000

# COMMAND ----------
# MAGIC %restart_python

# COMMAND ----------
dbutils.widgets.text("schema", "")    # ${var.schema} — the Postgres schema the tables live in
# Lakebase host + endpoint path are DERIVED from project/branch/endpoint (resolve.py) — not passed.
dbutils.widgets.text("lakebase_project", "")
dbutils.widgets.text("lakebase_branch", "")
dbutils.widgets.text("lakebase_endpoint", "")
dbutils.widgets.text("pg_database", "")
# The APP SP client id — granted read/write on the tables HERE (the owner that just created them grants
# in the same pg8000 session — no psql). setup.sh resolves it from the deployed app and passes it in.
dbutils.widgets.text("app_sp_id", "")
# The JOB SP client id (job mode only) — granted INSERT-ONLY on investigation_events, nothing else.
# Passed by setup.sh from config.yml's job_sp. Empty/"none" in in_process mode, which needs no job grant.
dbutils.widgets.text("job_sp_id", "")

# lib/ is installed as the `aia-lib` WHEEL in the serverless environment (databricks.yml), so it imports as
# a package straight from site-packages. See pyproject.toml.
from lib.pg import make_pg_connect, pg_exec
from lib import resolve

from databricks.sdk import WorkspaceClient

PG_SCHEMA = dbutils.widgets.get("schema").strip()
PROJECT = dbutils.widgets.get("lakebase_project").strip()
BRANCH = dbutils.widgets.get("lakebase_branch").strip()
ENDPOINT = dbutils.widgets.get("lakebase_endpoint").strip()
PG_DATABASE = dbutils.widgets.get("pg_database").strip()
for _n, _v in [("schema", PG_SCHEMA), ("lakebase_project", PROJECT), ("lakebase_branch", BRANCH),
               ("lakebase_endpoint", ENDPOINT), ("pg_database", PG_DATABASE)]:
    if not _v:
        raise ValueError(f"{_n} is required (the DAB passes it).")

_w = WorkspaceClient()
# The setup identity = the Postgres user. Get it from the SCIM API (current_user.me), NOT `spark.sql
# ("SELECT current_user()")` — Spark is reserved for the SparkSqlRunner tool path; the notebook's own
# identity comes from the SDK. Same call the app uses for the PG user (proven to
# match what Lakebase expects).
_me = _w.current_user.me().user_name
_pg_host = resolve.pg_host(_w, PROJECT, BRANCH, ENDPOINT)
_pg_endpoint_path = resolve.endpoint_path(PROJECT, BRANCH, ENDPOINT)
print(f"schema={PG_SCHEMA} pg_host={_pg_host} db={PG_DATABASE} as={_me}")

# COMMAND ----------
# Create the Postgres schema first (bootstrap connection, no search_path), then connect WITH the schema
# as search_path so the unqualified CREATE TABLEs land there.
_bootstrap = make_pg_connect(_w, host=_pg_host, database=PG_DATABASE,
                             user=_me, endpoint_path=_pg_endpoint_path)
pg_exec(_bootstrap, f'CREATE SCHEMA IF NOT EXISTS "{PG_SCHEMA}"')

connect = make_pg_connect(_w, host=_pg_host, database=PG_DATABASE,
                          user=_me, endpoint_path=_pg_endpoint_path, schema=PG_SCHEMA)

pg_exec(connect, """CREATE TABLE IF NOT EXISTS cases (
  case_id TEXT PRIMARY KEY, title TEXT, description TEXT, severity TEXT, status TEXT,
  indicator_value TEXT, indicator_type TEXT, account_id TEXT, source TEXT,
  assessed_severity TEXT, escalate_to_high BOOLEAN, latest_investigation_id TEXT,
  scenario_label TEXT, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)""")
pg_exec(connect, """CREATE TABLE IF NOT EXISTS investigations (
  investigation_id TEXT PRIMARY KEY, case_id TEXT REFERENCES cases(case_id), status TEXT,
  assessed_severity TEXT, escalate_to_high BOOLEAN, recommended_play TEXT, confidence DOUBLE PRECISION,
  summary TEXT, rationale TEXT, evidence JSONB, tools_called JSONB, model_endpoint TEXT,
  job_run_id TEXT, attempts INT DEFAULT 0,
  started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, investigated_by TEXT)""")
# `attempts` bounds startup-reconcile re-runs (see app/backend.py): each (re)run increments it, and the
# reconcile gives up on a row once it hits the cap so a crash-loop can't re-investigate forever. ADD it
# on pre-existing tables too — CREATE TABLE IF NOT EXISTS won't add a column to a table that already exists.
pg_exec(connect, "ALTER TABLE investigations ADD COLUMN IF NOT EXISTS attempts INT DEFAULT 0")
pg_exec(connect, "CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status)")
pg_exec(connect, "CREATE INDEX IF NOT EXISTS idx_inv_case ON investigations(case_id)")

# investigation_events — the APPEND-ONLY JOURNAL the investigate JOB writes to (job mode). The job gets
# INSERT here and NOTHING else: no read, no update, no access to cases/investigations. It appends
# 'started' then 'completed'|'failed'; the APP reconciles those into `investigations` (the state table it
# alone owns) via record_verdict. So the job never touches state, and a buggy/hostile job can only append
# rows — it cannot alter or read case data.
#
# job_run_id is the AUTHENTICITY binding: the job stamps each event with {{job.run_id}}, which the platform
# injects and the job cannot forge, and the app already recorded that run id at jobs.run_now. The app
# applies an event ONLY IF its job_run_id matches the one it dispatched for that investigation_id — so one
# job SP can't append a verdict for another investigation. No minted token, no stored secret.
pg_exec(connect, """CREATE TABLE IF NOT EXISTS investigation_events (
  event_id BIGSERIAL PRIMARY KEY,
  investigation_id TEXT NOT NULL,
  case_id TEXT,
  event_type TEXT NOT NULL,          -- dispatched | started | completed | failed
  job_run_id TEXT,                   -- {{job.run_id}} — platform-attested; must match investigations.job_run_id
  verdict JSONB,                     -- the full verdict on 'completed' (evidence + tools trail preserved)
  detail TEXT,                       -- error/reason on 'failed'
  applied_at TIMESTAMPTZ,            -- set by the APP once reconciled into investigations (NULL = pending)
  created_at TIMESTAMPTZ DEFAULT now())""")
# Partial index: the app's poll asks "any unapplied terminal events?" — keep that read tiny.
pg_exec(connect, """CREATE INDEX IF NOT EXISTS idx_events_unapplied ON investigation_events (investigation_id)
                    WHERE applied_at IS NULL""")
print("cases + investigations + investigation_events tables ready in Lakebase Postgres.")

# COMMAND ----------
# MAGIC %md
# MAGIC ## Grant the app SP read/write on the tables (no psql — done here as the owner)
# MAGIC The app connects to Postgres as its OWN SP. Two steps: (1) map that SP to a Postgres ROLE (a
# MAGIC Databricks control-plane call, via REST so it works on any SDK), then (2) `GRANT` table privileges
# MAGIC to that role — as the table OWNER, which is exactly the identity running this notebook (it just
# MAGIC created them). Skipped if app_sp_id isn't passed (e.g. a first run before the app exists).

# COMMAND ----------
def _grant_with_role_retry(conn, statements, role_name, attempts=30, delay=2):
    """Run the GRANT statements, retrying the whole (idempotent) set while Postgres still reports the freshly
    created role as absent. A role created via the control-plane REST call isn't usable in-DB immediately, so
    the first GRANT can fail SQLSTATE 42704 ('role "<id>" does not exist'). We retry the GRANTs themselves
    rather than polling pg_roles — a connection may not even see other roles in that view, which makes a
    poll-based wait time out falsely. Any error that is NOT 'role does not exist' is raised immediately."""
    import time as _time
    for _i in range(attempts):
        try:
            for _stmt in statements:
                pg_exec(conn, _stmt)
            return
        except Exception as e:
            msg = str(e)
            if "does not exist" in msg and role_name in msg:
                _time.sleep(delay)
                continue
            raise
    raise RuntimeError(f"role '{role_name}' never became usable for GRANT after {attempts * delay}s")


APP_SP_ID = dbutils.widgets.get("app_sp_id").strip()
if APP_SP_ID:
    BRANCH_PATH = f"projects/{PROJECT}/branches/{BRANCH}"
    # 1. Map the app SP to a Postgres role (its in-DB role name == the SP client id). Idempotent.
    try:
        _w.api_client.do("POST", f"/api/2.0/postgres/{BRANCH_PATH}/roles",
                         query={"role_id": "appsp"},
                         body={"spec": {"identity_type": "SERVICE_PRINCIPAL",
                                        "auth_method": "LAKEBASE_OAUTH_V1", "postgres_role": APP_SP_ID}})
    except Exception as e:
        print(f"  (create pg role: {str(e)[:120]} — continuing; role may already exist)")
    # 2. GRANT to that role by its in-DB name (== the SP client id). Identifiers can't be bound params;
    #    APP_SP_ID is a UUID from config (not user input), so f-string interpolation is safe here.
    #    The role-create POST returns before the role is usable in-DB (async propagation), so on a FRESH app
    #    SP the FIRST grant can fail "role does not exist" (SQLSTATE 42704). Retry the grants until they take,
    #    rather than polling pg_roles (a connection may not even see other roles in the pg_roles view, which
    #    made a poll-based wait give false timeouts). Each pg_exec is its own statement, so re-running the
    #    whole idempotent GRANT set on a transient failure is safe.
    _grants = (f'GRANT USAGE ON SCHEMA "{PG_SCHEMA}" TO "{APP_SP_ID}"',
               f'GRANT SELECT, INSERT, UPDATE ON "{PG_SCHEMA}".cases TO "{APP_SP_ID}"',
               f'GRANT SELECT, INSERT, UPDATE ON "{PG_SCHEMA}".investigations TO "{APP_SP_ID}"',
               # the app is the journal's READER + reconciler: it appends 'dispatched', reads pending
               # events, and stamps applied_at once folded into `investigations`.
               f'GRANT SELECT, INSERT, UPDATE ON "{PG_SCHEMA}".investigation_events TO "{APP_SP_ID}"',
               f'GRANT USAGE, SELECT ON SEQUENCE "{PG_SCHEMA}".investigation_events_event_id_seq TO "{APP_SP_ID}"')
    _grant_with_role_retry(connect, _grants, APP_SP_ID)
    print(f"app SP {APP_SP_ID} granted SELECT/INSERT/UPDATE on cases + investigations + investigation_events.")
else:
    print("no app_sp_id passed — skipped the app-SP Postgres grant (run again once the app exists).")

# COMMAND ----------
# MAGIC %md
# MAGIC ## Grant the JOB SP **INSERT-ONLY** on the journal (job mode)
# MAGIC Least privilege, deliberately: the investigate job appends `started` / `completed` / `failed` rows to
# MAGIC `investigation_events` and can do **nothing else** — no SELECT on the journal, no access at all to
# MAGIC `cases` / `investigations`. It cannot read case data, cannot alter state, cannot even see its own
# MAGIC events. The app owns state and reconciles the journal into it.

# COMMAND ----------
JOB_SP_ID = dbutils.widgets.get("job_sp_id").strip()
if JOB_SP_ID and JOB_SP_ID.lower() != "none":
    BRANCH_PATH = f"projects/{PROJECT}/branches/{BRANCH}"
    try:
        _w.api_client.do("POST", f"/api/2.0/postgres/{BRANCH_PATH}/roles",
                         query={"role_id": "jobsp"},
                         body={"spec": {"identity_type": "SERVICE_PRINCIPAL",
                                        "auth_method": "LAKEBASE_OAUTH_V1", "postgres_role": JOB_SP_ID}})
    except Exception as e:
        print(f"  (create pg role: {str(e)[:120]} — continuing; role may already exist)")
    # USAGE on the schema is needed to reference the table at all; then INSERT on the journal ONLY.
    # BIGSERIAL needs the sequence too, or the INSERT fails on nextval permission. Retry until the freshly
    # created role is usable (same async-propagation handling as the app SP above).
    _job_grants = (f'GRANT USAGE ON SCHEMA "{PG_SCHEMA}" TO "{JOB_SP_ID}"',
                   f'GRANT INSERT ON "{PG_SCHEMA}".investigation_events TO "{JOB_SP_ID}"',
                   f'GRANT USAGE, SELECT ON SEQUENCE "{PG_SCHEMA}".investigation_events_event_id_seq TO "{JOB_SP_ID}"')
    _grant_with_role_retry(connect, _job_grants, JOB_SP_ID)
    print(f"job SP {JOB_SP_ID} granted INSERT-ONLY on {PG_SCHEMA}.investigation_events (no state access).")
else:
    print("no job_sp_id passed — skipped the job-SP journal grant (in_process mode needs none).")

# COMMAND ----------
dbutils.notebook.exit(f"structure ready: Lakebase {PG_SCHEMA}.cases + {PG_SCHEMA}.investigations")
