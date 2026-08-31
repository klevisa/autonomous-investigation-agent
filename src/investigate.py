# Databricks notebook source
# MAGIC %md
# MAGIC # Investigate a case (JOB driver — for investigations that outlast the app)
# MAGIC A **thin driver** that runs as the **job SP**. It owns NO state: its entire database surface is
# MAGIC `INSERT` on the append-only journal (`investigation_events`) — it cannot read that journal and has no
# MAGIC access at all to `cases`/`investigations`. The app remains the sole owner of state.
# MAGIC The flow (see README "Where the agent runs" + lib/journal.py):
# MAGIC   1. the app opens the investigation + fires this job, passing the CASE CONTENT as parameters,
# MAGIC   2. this job appends `started`, investigates (tools on Delta/UC via Spark; LLM via the gateway),
# MAGIC   3. it appends `completed` (with the FULL verdict) or `failed` (with the reason),
# MAGIC   4. the app reconciles those events into `investigations` via the same `record_verdict` in_process
# MAGIC      uses — so there is ONE completion contract, and no HTTP callback anywhere.
# MAGIC
# MAGIC The real work lives in the shared library:
# MAGIC   * `lib/investigator.py`  — the pure tool-calling agent: `Investigator(tool_fn, llm).investigate(case)`,
# MAGIC   * `lib/journal.py`       — the append-only event journal (this job's only write path),
# MAGIC   * `lib/tools.py`         — SqlRunner + the UC-function tool adapter (here `SparkSqlRunner` — the job's own Spark),
# MAGIC   * `lib/llm.py`           — the AI Gateway client (URL + token from secrets; NOT FMAPI).
# MAGIC
# MAGIC **IDENTITY — secret-free:** the job runs as the JOB SP,
# MAGIC which is a **MEMBER of the AIA role group**. So it *inherits* the role's evidence-table SELECT /
# MAGIC tool EXECUTE grants — tool queries run through the job's own Spark session, no token mint, no secret.
# MAGIC Journal writes use the job SP's own short-lived Lakebase OAuth token (minted per connection). Events
# MAGIC are stamped with `{{job.run_id}}`, which the platform injects and the job cannot forge — the app only
# MAGIC applies events whose run id matches the one it dispatched. No stored client secret anywhere.
# MAGIC
# MAGIC **When to use job mode (vs. in_process):** for investigations that could outlast the
# MAGIC app's compute (platform-supervised job durability + reconcile via the journal). job_warehouse (the
# MAGIC default) runs the tools on the warehouse; job runs them on this notebook's own Spark session.

# COMMAND ----------
# MAGIC %pip install -q mlflow

# COMMAND ----------
# MAGIC %restart_python

# COMMAND ----------
import json

import mlflow
from databricks.sdk import WorkspaceClient

# COMMAND ----------
dbutils.widgets.text("catalog", "")   # the DAB passes ${var.catalog}
dbutils.widgets.text("schema", "")    # the DAB passes ${var.schema}
dbutils.widgets.text("agent_mode", "job")   # job (Spark tools) | job_warehouse (warehouse tools, no spark)
dbutils.widgets.text("warehouse_id", "")    # only used when agent_mode=job_warehouse
dbutils.widgets.text("case_id", "")
dbutils.widgets.text("investigation_id", "")   # the app opened this row; our events reference it
dbutils.widgets.text("job_run_id", "")         # {{job.run_id}} — platform-attested; the authenticity binding
# Durable-runner retry accounting: task.execution_count is the 1-based attempt number (first try=1); max_retries
# is the task's configured retry count (same var feeds both). We report a 'failed' journal event ONLY on the
# final attempt, so Databricks' in-run retries of a transient failure don't record a premature failure.
dbutils.widgets.text("task_execution_count", "1")
dbutils.widgets.text("max_retries", "0")
# LLM gateway: plain URL + a token fetched from AWS Secrets Manager via a UC service credential (lib/llm.py).
dbutils.widgets.text("llm_endpoint_url", "")
dbutils.widgets.text("llm_service_credential", "")
dbutils.widgets.text("llm_secret_arn", "")
dbutils.widgets.text("llm_secret_region", "us-west-2")
dbutils.widgets.text("llm_secret_json_key", "token")
# Lakebase coordinates — the job appends events to the JOURNAL (INSERT-only; see lib/journal.py).
dbutils.widgets.text("lakebase_project", "")
dbutils.widgets.text("lakebase_branch", "production")
dbutils.widgets.text("lakebase_endpoint", "primary")
dbutils.widgets.text("pg_database", "")
import os as _os
# lib/llm.py reads these from the environment. The job SP holds ACCESS on the
# service credential, so generate_temporary_service_credential works with no stored AWS key.
for _k in ("llm_endpoint_url", "llm_service_credential", "llm_secret_arn", "llm_secret_region",
           "llm_secret_json_key"):
    _v = dbutils.widgets.get(_k).strip()
    if _v:
        _os.environ["AIA_" + _k.upper()] = _v
# The case CONTENT, passed by the app (the agent has no Lakebase access, so it never reads the case).
dbutils.widgets.text("case_title", "")
dbutils.widgets.text("case_description", "")
dbutils.widgets.text("case_severity", "")
dbutils.widgets.text("case_indicator_value", "")
dbutils.widgets.text("case_indicator_type", "")
dbutils.widgets.text("case_account_id", "")
dbutils.widgets.text("case_scenario_label", "")

# lib/ is installed as the `aia-lib` WHEEL in the serverless environment (databricks.yml) — imports as a
# package from site-packages. See pyproject.toml.
from lib.llm import GatewayLLM
from lib.tools import SparkSqlRunner, WarehouseSqlRunner, make_tool_fn
from lib.mcp_tools import make_mcp_tool_fn, make_mcp_clients, make_routed_tool_fn
from lib.investigator import Investigator, MAX_TOKENS
from lib import journal
from lib.pg import make_pg_connect
from lib import resolve

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
AGENT_MODE = dbutils.widgets.get("agent_mode").strip() or "job_warehouse"
WAREHOUSE_ID = dbutils.widgets.get("warehouse_id").strip()
CASE_ID = dbutils.widgets.get("case_id").strip()
INV_ID = dbutils.widgets.get("investigation_id").strip()
# {{job.run_id}} — the platform injects this and the job CANNOT forge it. Every event we append carries it,
# and the app only applies an event whose run id matches the one it recorded at jobs.run_now. That's the whole
# authenticity story: no token to mint, no secret to store (see lib/journal.py).
JOB_RUN_ID = dbutils.widgets.get("job_run_id").strip()
# IS_FINAL — is this the last retry attempt? execution_count is 1-based (first try=1), max_retries is the
# configured retry count, so the final attempt is execution_count == max_retries+1, i.e. execution_count >
# max_retries. We append a 'failed' journal event only when IS_FINAL, so a transient failure that Databricks
# retries (and that later succeeds) never records a premature terminal failure. Defaults (1, 0) → IS_FINAL
# true, matching the no-retry behaviour when the task has max_retries=0.
try:
    _EXEC_COUNT = int(dbutils.widgets.get("task_execution_count").strip() or "1")
    _MAX_RETRIES = int(dbutils.widgets.get("max_retries").strip() or "0")
except ValueError:
    _EXEC_COUNT, _MAX_RETRIES = 1, 0
IS_FINAL = _EXEC_COUNT > _MAX_RETRIES
PROJECT = dbutils.widgets.get("lakebase_project").strip()
BRANCH = dbutils.widgets.get("lakebase_branch").strip() or "production"
ENDPOINT = dbutils.widgets.get("lakebase_endpoint").strip() or "primary"
PG_DATABASE = dbutils.widgets.get("pg_database").strip()
for _n, _v in [("catalog", CATALOG), ("schema", SCHEMA), ("case_id", CASE_ID),
               ("investigation_id", INV_ID), ("job_run_id", JOB_RUN_ID),
               ("lakebase_project", PROJECT), ("pg_database", PG_DATABASE)]:
    if not _v:
        raise ValueError(f"{_n} is required — the app passes it when it triggers this job.")

# Reassemble the case dict from the params the app passed (shape matches Investigator.investigate()).
case = {
    "case_id": CASE_ID,
    "title": dbutils.widgets.get("case_title").strip(),
    "description": dbutils.widgets.get("case_description").strip(),
    "severity": dbutils.widgets.get("case_severity").strip(),
    "indicator_value": dbutils.widgets.get("case_indicator_value").strip(),
    "indicator_type": dbutils.widgets.get("case_indicator_type").strip(),
    "account_id": dbutils.widgets.get("case_account_id").strip(),
    "scenario_label": dbutils.widgets.get("case_scenario_label").strip(),
}

# Identity from the control plane (SCIM), NOT spark.sql("SELECT current_user()") — Spark is reserved for the
# SparkSqlRunner tool path (make_tool_fn below). `me` is the job SP (a member of the AIA role group) and the
# Lakebase Postgres user for the journal connection.
_w = WorkspaceClient()
me = _w.current_user.me().user_name
mlflow.set_experiment(f"/Users/{me}/aia-investigations")
print(f"catalog={CATALOG} schema={SCHEMA} case={CASE_ID} inv={INV_ID} run={JOB_RUN_ID} run_as={me}")

# COMMAND ----------
# The JOURNAL connection — the job's ONLY database surface. It holds INSERT on investigation_events and
# nothing else: it cannot read the journal, and has no access whatsoever to cases/investigations. Auth is the
# job SP's own short-lived Lakebase OAuth token (minted per connection by pg.make_pg_connect) — no secret.
# (_w created above for the identity lookup; reused here.)
_journal = make_pg_connect(
    _w, host=resolve.pg_host(_w, PROJECT, BRANCH, ENDPOINT), database=PG_DATABASE,
    user=me, endpoint_path=resolve.endpoint_path(PROJECT, BRANCH, ENDPOINT), schema=SCHEMA)

# 'started' — proves this run actually got compute. The app uses the presence/absence of this event to tell
# "dispatched but never started" (re-trigger, and DON'T burn an attempt — no work was done) apart from
# "started then died" (re-trigger and DO count it — that's the crash-loop the attempts cap exists for).
journal.append_event(_journal, INV_ID, journal.STARTED, JOB_RUN_ID, case_id=CASE_ID)
print(f"journal: {journal.STARTED} appended for {INV_ID}")

# COMMAND ----------
# TOOLS run as the job SP, which is a MEMBER of the AIA role group, so it INHERITS the role's evidence
# SELECT / tool EXECUTE grants — no token mint, no client secret.
# WHERE they run depends on the mode:
#   job            → the job's OWN Spark session (SparkSqlRunner).
#   job_warehouse  → the warehouse via the SQL Statement Execution API (WarehouseSqlRunner), delegating the
#                    query off the (idle) job compute. The `spark` session is NOT used at all in this mode;
#                    the runner uses the ambient WorkspaceClient (_w = the job SP), which needs the role's
#                    warehouse CAN_USE (granted to the group in admin_prereqs, inherited by membership).
if AGENT_MODE == "job_warehouse":
    if not WAREHOUSE_ID:
        raise ValueError("warehouse_id is required in job_warehouse mode (tools run on the warehouse).")
    sql_tool_fn = make_tool_fn(WarehouseSqlRunner(_w, WAREHOUSE_ID), CATALOG, SCHEMA)
    print(f"tools: WarehouseSqlRunner on {WAREHOUSE_ID} (job_warehouse — spark session unused)")
else:
    sql_tool_fn = make_tool_fn(SparkSqlRunner(spark), CATALOG, SCHEMA)
    print("tools: SparkSqlRunner on the job's own Spark session (job)")
# pivot_indicator (Databricks managed MCP) and enrich_indicator (the custom MCP server behind a UC
# Connection + MCP Service) — both authenticate as the job SP, the SAME ambient `_w` used above; this
# never touches a warehouse (Databricks-managed serverless compute), so it's free in plain `job` mode.
mcp_tool_fn = make_mcp_tool_fn(make_mcp_clients(_w, CATALOG, SCHEMA))
tool_fn = make_routed_tool_fn(sql_tool_fn, mcp_tool_fn)
print("tools: pivot_indicator via managed MCP, enrich_indicator via custom MCP (UC connection)")
llm = GatewayLLM()                                   # URL from env; token via AWS Secrets Manager (lib/llm.py)
# no temperature: reasoning models (Claude Opus 5) reject it; the gateway defaults are fine.
llm_fn = lambda messages, tools: llm.chat(messages, tools=tools, max_tokens=MAX_TOKENS)


@mlflow.trace(span_type="CHAIN")
def main():
    mlflow.update_current_trace(tags={"case_id": CASE_ID, "investigation_id": INV_ID})
    # The Investigator just returns the verdict and we append it to the
    # journal below — the app folds it into `investigations` on its next sweep.
    return Investigator(tool_fn, llm_fn).investigate(case)


# Terminal-event reporting. On success the FULL verdict goes into the journal (evidence + tool trail
# preserved). On failure we append a 'failed' event with WHY — but ONLY on the FINAL attempt: if Databricks
# will retry this task (not final), we append NOTHING and just re-raise, so the platform re-runs the same job
# run and a later attempt can succeed (or the final one reports failure). This keeps the journal's "one
# terminal event per run" contract: without the IS_FINAL gate, a non-final 'failed' could be applied by the
# app before the retry succeeds, locking in a failure the run later recovered from. Always re-raise so
# Databricks counts the attempt and triggers the retry.
try:
    verdict = main()
except Exception as _e:
    import traceback
    traceback.print_exc()
    if IS_FINAL:
        try:
            journal.append_event(_journal, INV_ID, journal.FAILED, JOB_RUN_ID, case_id=CASE_ID,
                                 detail=f"{type(_e).__name__}: {_e}")
            print(f"journal: {journal.FAILED} appended for {INV_ID} (final attempt {_EXEC_COUNT})")
        except Exception:
            traceback.print_exc()   # journal write failed too → the row stays 'running'; reconcile handles it
    else:
        print(f"journal: attempt {_EXEC_COUNT}/{_MAX_RETRIES + 1} failed — NOT final, no terminal event; "
              f"letting Databricks retry the task")
    raise

journal.append_event(_journal, INV_ID, journal.COMPLETED, JOB_RUN_ID, case_id=CASE_ID, verdict=verdict)
print(f"journal: {journal.COMPLETED} appended for {INV_ID}")
print(json.dumps(verdict, indent=2, default=str))
dbutils.notebook.exit(json.dumps({"investigation_id": INV_ID, "case_id": CASE_ID,
                                  "assessed_severity": verdict.get("assessed_severity"),
                                  "escalate_to_high": bool(verdict.get("escalate_to_high"))}))
