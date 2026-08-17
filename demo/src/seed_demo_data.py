# Databricks notebook source
# MAGIC %md
# MAGIC # DEMO tier: the Delta substrate, the tools, and the seed data (throwaway)
# MAGIC Everything AIA already has in production lives here — so in prod this notebook is **NOT run**:
# MAGIC
# MAGIC 1. creates the **Delta schema** + the threat-intel **substrate tables** (empty),
# MAGIC 2. creates the **5 UC-function tools** the agent calls, and **grants the agent SP** EXECUTE on
# MAGIC    them + SELECT on the substrate (in prod, AIA grants the agent SP access to THEIR tables/tools),
# MAGIC 3. loads the substrate CSVs (`./seed`) into the Delta tables, and
# MAGIC 4. seeds **~25 medium cases** into Lakebase — a balanced mix (some with known-bad malware IOCs
# MAGIC    that should escalate, a benign control, others) so the demo shows the agent discriminating.
# MAGIC
# MAGIC **In production none of this runs.** AIA's own detection/SIEM data + tools already exist; they
# MAGIC just grant the agent SP access to them (the agent's tool list dictates which tables it needs). Real
# MAGIC Tines cases (fetched live) replace the seeded rows. `build_structure` (the Lakebase tables) is
# MAGIC the ONLY structure prod builds. Deleting this notebook + `seed/` is the whole "remove the demo".
# MAGIC
# MAGIC Re-running is safe: schema/tables/functions are IF NOT EXISTS / CREATE OR REPLACE, substrate tables
# MAGIC are overwritten from the CSVs, and the demo cases are cleared + reinserted (so it doubles as a reset).

# COMMAND ----------
# MAGIC %pip install -q pg8000

# COMMAND ----------
# MAGIC %restart_python

# COMMAND ----------
dbutils.widgets.text("catalog", "")   # ${var.catalog}
dbutils.widgets.text("schema", "")    # ${var.schema}
dbutils.widgets.text("seed_src", "")  # ${workspace.file_path}/seed
# The AIA ROLE (account group) is the single tool-running identity — both modes ASSUME it, so it (not any
# SP) gets the demo evidence SELECT + tool EXECUTE grants. In prod AIA grants the equivalent to this role
# on THEIR tables/tools. A static name, passed straight from the bundle var.
dbutils.widgets.text("role_group", "")
# Host + endpoint path are derived from the project/branch/endpoint names (resolve.py), not passed.
dbutils.widgets.text("lakebase_project", "")
dbutils.widgets.text("lakebase_branch", "")
dbutils.widgets.text("lakebase_endpoint", "")
dbutils.widgets.text("pg_database", "")

# lib/ is installed as the `aia-lib` WHEEL in the serverless environment (databricks.yml) — imports as a
# package from site-packages; no lib_dir widget, no local staging, no /Workspace FUSE read. See pyproject.toml.
from lib.common import ctx_for
from demo_substrate import SUBSTRATE_TABLES, spark_schema   # DEMO tables (in this demo bundle's wheel)
from lib.pg import make_pg_connect, pg_exec, pg_query
from lib import resolve
from databricks.sdk import WorkspaceClient

# Identity from the control plane (SCIM), NOT spark.sql("SELECT current_user()") — Spark is reserved for the
# SparkSqlRunner tool path. `me` = the Lakebase Postgres user (matches the app).
# _w is reused later (seed-CSV control-plane download + the Lakebase connection).
_w = WorkspaceClient()
ctx = ctx_for(_w.current_user.me().user_name, catalog=dbutils.widgets.get("catalog").strip(),
              schema=dbutils.widgets.get("schema").strip())
SEED_SRC = dbutils.widgets.get("seed_src").strip().rstrip("/")
if not SEED_SRC:
    raise ValueError("seed_src is empty — deploy via the DAB (it passes ${workspace.file_path}/seed).")
SEED_VOLUME = f"/Volumes/{ctx.catalog}/{ctx.schema}/seed"
ROLE_GROUP = dbutils.widgets.get("role_group").strip()
print(ctx)
print(f"seed_src={SEED_SRC}  seed_volume={SEED_VOLUME}  role_group={ROLE_GROUP or '(none)'}")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 1. Delta schema + empty substrate tables (DEMO — in prod these are AIA's own tables)
# MAGIC The table names + explicit schemas come from `demo_substrate.SUBSTRATE_TABLES` (shared with the
# MAGIC CSV load below, so the columns can't drift). Created empty here; loaded in step 3.

# COMMAND ----------
# The schema is PRE-CREATED by the platform admin (pre-setup), NOT here — that's the production parity point:
# AIA's evidence schema already exists in prod, and a regular deployer only creates the tables/tools INSIDE
# a schema it was granted (USE_SCHEMA + CREATE on the schema), never the schema itself. So the deployer needs
# no catalog CREATE_SCHEMA/MANAGE. We assume the schema exists; if it doesn't,
# saveAsTable below fails loudly, which is the correct signal that pre-setup didn't run.
for _csv, table, cols in SUBSTRATE_TABLES:
    empty = spark.createDataFrame([], spark_schema(spark, cols))
    empty.write.mode("ignore").saveAsTable(ctx.table(table))   # ignore = don't clobber existing data
    print(f"  table ready: {ctx.table(table)}")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 2. UC-function tools + grant the agent SP (invoker rights — callable from SQL, Genie, an agent)
# MAGIC `enrich_indicator` is table-backed by `indicator_intel` (in prod it would call the real URLhaus
# MAGIC API); `blast_radius` scans `telemetry`. In prod these tools + their backing tables are AIA's; they
# MAGIC just grant the agent SP EXECUTE/SELECT on them — the agent's tool list dictates which tables an
# MAGIC investigation touches.

# COMMAND ----------
spark.sql(f"""CREATE OR REPLACE FUNCTION {ctx.table('get_account_risk')}(
  acct STRING COMMENT 'Account id, e.g. ACC-000888')
RETURNS TABLE (account_id STRING, customer_name STRING, segment STRING,
               risk_score INT, risk_band STRING, top_signal STRING)
COMMENT 'Latest risk score, band, and top contributing signal for an account.'
RETURN
  SELECT a.account_id, a.customer_name, a.segment, latest.risk_score, latest.risk_band, latest.top_signal
  FROM (SELECT account_id, risk_score, risk_band, top_signal,
               row_number() OVER (PARTITION BY account_id ORDER BY score_date DESC) AS rn
        FROM {ctx.table('account_risk_scores')}) latest
  JOIN {ctx.table('accounts')} a ON a.account_id = latest.account_id
  WHERE latest.rn = 1 AND a.account_id = acct""")

spark.sql(f"""CREATE OR REPLACE FUNCTION {ctx.table('get_account_actions')}(
  acct STRING COMMENT 'Account id')
RETURNS TABLE (action_type STRING, reason_summary STRING, taken_by STRING,
               taken_at TIMESTAMP, related_investigation_id STRING)
COMMENT 'Protective actions already taken on an account and why.'
RETURN
  SELECT action_type, reason_summary, taken_by, taken_at, related_investigation_id
  FROM {ctx.table('account_actions')} WHERE account_id = acct ORDER BY taken_at DESC""")

spark.sql(f"""CREATE OR REPLACE FUNCTION {ctx.table('pivot_indicator')}(
  ind STRING COMMENT 'Indicator value (URL/IP/domain/hash) or IOC-id')
RETURNS TABLE (indicator_value STRING, indicator_type STRING, campaign_id STRING,
               campaign_name STRING, campaign_severity STRING, actor_id STRING,
               actor_name STRING, actor_aliases STRING, family STRING, threat STRING,
               tags STRING, sibling_count BIGINT, sibling_indicators ARRAY<STRING>)
COMMENT 'Pivot an indicator to its campaign, threat actor, sibling indicators, and URLhaus family/threat/tags.'
RETURN
  WITH hit AS (
    SELECT i.indicator_id, i.indicator_value, i.indicator_type, i.campaign_id,
           c.campaign_name, c.severity AS campaign_severity, c.actor_id,
           ta.actor_name, ta.aliases AS actor_aliases, ii.family, ii.threat, ii.tags
    FROM {ctx.table('indicators')} i
    LEFT JOIN {ctx.table('campaigns')} c ON i.campaign_id = c.campaign_id
    LEFT JOIN {ctx.table('threat_actors')} ta ON c.actor_id = ta.actor_id
    LEFT JOIN {ctx.table('indicator_intel')} ii ON i.indicator_id = ii.indicator_id
    WHERE i.indicator_value = ind OR i.indicator_id = ind
    LIMIT 1)
  SELECT h.indicator_value, h.indicator_type, h.campaign_id, h.campaign_name, h.campaign_severity,
         h.actor_id, h.actor_name, h.actor_aliases, h.family, h.threat, h.tags,
         (SELECT count(*) FROM {ctx.table('indicators')} s
            WHERE s.campaign_id = h.campaign_id AND s.indicator_value <> h.indicator_value) AS sibling_count,
         (SELECT slice(collect_list(s.indicator_value), 1, 8) FROM {ctx.table('indicators')} s
            WHERE s.campaign_id = h.campaign_id AND s.indicator_value <> h.indicator_value) AS sibling_indicators
  FROM hit h""")

spark.sql(f"""CREATE OR REPLACE FUNCTION {ctx.table('blast_radius')}(
  ind STRING COMMENT 'Indicator value (URL/IP/domain/hash)')
RETURNS TABLE (account_id STRING, segment STRING, risk_band STRING, hits BIGINT, last_seen TIMESTAMP)
COMMENT 'Which internal accounts have this indicator in their detection telemetry, with each account latest risk band.'
RETURN
  SELECT t.account_id, a.segment, sc.risk_band, count(*) AS hits, max(t.created_at) AS last_seen
  FROM {ctx.table('telemetry')} t
  LEFT JOIN {ctx.table('accounts')} a ON a.account_id = t.account_id
  LEFT JOIN (SELECT account_id, risk_band,
                    row_number() OVER (PARTITION BY account_id ORDER BY score_date DESC) AS rn
             FROM {ctx.table('account_risk_scores')}) sc ON sc.account_id = t.account_id AND sc.rn = 1
  WHERE t.indicator_value = ind
  GROUP BY t.account_id, a.segment, sc.risk_band ORDER BY hits DESC""")

spark.sql(f"""CREATE OR REPLACE FUNCTION {ctx.table('enrich_indicator')}(
  ind STRING COMMENT 'The artifact to enrich: a URL, IP, domain, md5, or sha256')
RETURNS TABLE (indicator STRING, query_status STRING, threat STRING, url_status STRING, tags STRING, family STRING)
COMMENT 'Enrich an indicator against the URLhaus threat feed (table-backed). query_status ok=known-bad / no_results=unknown.'
RETURN
  WITH hit AS (
    SELECT indicator_value, url_status, threat, tags, family
    FROM {ctx.table('indicator_intel')}
    WHERE indicator_value = ind OR payload_md5 = ind OR payload_sha256 = ind
    LIMIT 1)
  SELECT ind AS indicator,
         CASE WHEN h.indicator_value IS NOT NULL THEN 'ok' ELSE 'no_results' END AS query_status,
         h.threat, h.url_status, h.tags, h.family
  FROM (SELECT 1 AS x) d LEFT JOIN hit h ON TRUE""")

_TOOLS = ["get_account_risk", "get_account_actions", "pivot_indicator", "blast_radius", "enrich_indicator"]
# The AIA ROLE is granted what an investigation needs — USE CATALOG + USE SCHEMA + SELECT + EXECUTE — by the
# ADMIN at the SCHEMA level BEFORE this seed runs (the admin prereqs). Schema-level grants cover
# objects created LATER (verified 2026-08-07: a function created after a schema-level EXECUTE grant is covered),
# so these tools are automatically callable by the role with no per-tool grant here. This is the production parity
# split: the schema owner (platform admin) grants the role read/exec on the evidence schema; the deployer only
# CREATES the tables/tools inside a schema it was granted USE + CREATE on — it does NOT own the schema and
# therefore CANNOT (and need not) grant the role. So the seed issues no grants.
print(f"5 UC-function tools created in {ctx.fqschema} (role grants are admin/schema-level — see prereqs).")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 3. Load the substrate CSVs into the Delta tables
# MAGIC The CSVs ride in this bundle (`./seed`); we copy them to a UC volume, then overwrite each table
# MAGIC from its CSV using the explicit schema in `demo_substrate.SUBSTRATE_TABLES` (shared with
# MAGIC `build_structure`, so the columns can't drift). An inferred read would make every column STRING
# MAGIC and break the tools.

# COMMAND ----------
import time

# _w (WorkspaceClient) was created at the top for the identity lookup; reused here for the control-plane
# seed-CSV download below and the Lakebase connection.
spark.sql(f"CREATE VOLUME IF NOT EXISTS {ctx.fqschema}.seed COMMENT 'CSV seed copied from the bundle.'")


def _copy_seed_csv(src_ws_path, dst_volume_path, retries=6):
    """Copy one seed CSV from the bundle's /Workspace seed dir to the UC volume. Reads the source via the SDK
    Workspace **download API** (control plane) rather than the /Workspace FUSE mount — the mount read
    intermittently fails with `OSError [Errno 5]` (blob 403 SSE-C) on serverless (verified 2026-08-04)."""
    for attempt in range(retries):
        try:
            data = _w.workspace.download(src_ws_path).read()   # control-plane read (bypasses FUSE/blob)
            with open(dst_volume_path, "wb") as f:              # the /Volumes dest is a normal writable FS
                f.write(data)
            return
        except Exception as e:
            if attempt == retries - 1:
                raise RuntimeError(f"could not copy {src_ws_path} after {retries} tries: {e}")
            time.sleep(1.5 * (attempt + 1))


for csv_name, _table, _cols in SUBSTRATE_TABLES:
    _copy_seed_csv(f"{SEED_SRC}/{csv_name}.csv", f"{SEED_VOLUME}/{csv_name}.csv")

for csv_name, table, cols in SUBSTRATE_TABLES:
    df = (spark.read.format("csv").option("header", "true").option("mode", "PERMISSIVE")
          .schema(spark_schema(spark, cols)).load(f"{SEED_VOLUME}/{csv_name}.csv"))
    df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(ctx.table(table))
    print(f"  loaded {ctx.table(table):52s} {df.count():6d} rows  <- {csv_name}.csv")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 4. Seed ~25 MEDIUM demo cases into Lakebase
# MAGIC Drawn from `telemetry` (Delta) so every case has a real IOC + account the tools can investigate.
# MAGIC Balanced so the demo shows both outcomes: ~10 with a KNOWN-BAD feed IOC (strong escalate-to-high
# MAGIC candidates), the rest a spread incl. a benign control (should mostly NOT escalate). Inserted into
# MAGIC Postgres (parameterized — indicators are URLs full of ? and % that inline SQL would mangle).

# COMMAND ----------
# schema=ctx.schema sets the search_path so the unqualified cases/investigations below resolve to AIA's
# schema (where build_structure created them), not public. Host/endpoint derived from the project.
# (_w was created above for the seed-CSV download; reused here.)
_project = dbutils.widgets.get("lakebase_project").strip()
_branch = dbutils.widgets.get("lakebase_branch").strip()
_endpoint = dbutils.widgets.get("lakebase_endpoint").strip()
connect = make_pg_connect(_w, host=resolve.pg_host(_w, _project, _branch, _endpoint),
                          database=dbutils.widgets.get("pg_database").strip(),
                          user=ctx.me, endpoint_path=resolve.endpoint_path(_project, _branch, _endpoint),
                          schema=ctx.schema)

# Idempotent reseed (also a demo reset): clear prior demo rows, children first for the FK.
pg_exec(connect, "DELETE FROM investigations")
pg_exec(connect, "DELETE FROM cases WHERE source = 'tines'")

seed = spark.sql(f"""
  WITH latest_risk AS (
    SELECT account_id, risk_band,
           row_number() OVER (PARTITION BY account_id ORDER BY score_date DESC) AS rn
    FROM {ctx.table('account_risk_scores')}),
  known_bad AS (SELECT DISTINCT indicator_value FROM {ctx.table('indicator_intel')}),
  enriched AS (
    SELECT t.incident_id, t.created_at, t.narrative, t.indicator_value, t.indicator_type,
           t.account_id, t.scenario_label, lr.risk_band,
           CASE WHEN kb.indicator_value IS NOT NULL THEN 1 ELSE 0 END AS is_known_bad,
           row_number() OVER (
             PARTITION BY CASE WHEN kb.indicator_value IS NOT NULL THEN 1 ELSE 0 END
             ORDER BY t.created_at) AS rn
    FROM {ctx.table('telemetry')} t
    LEFT JOIN latest_risk lr ON lr.account_id = t.account_id AND lr.rn = 1
    LEFT JOIN known_bad kb ON kb.indicator_value = t.indicator_value
    WHERE t.indicator_value IS NOT NULL)
  SELECT incident_id, created_at, narrative, indicator_value, indicator_type,
         account_id, scenario_label, risk_band
  FROM enriched
  WHERE (is_known_bad = 1 AND rn <= 10) OR (is_known_bad = 0 AND rn <= 15)
  ORDER BY is_known_bad DESC, created_at""")

import re as _re
for i, r in enumerate(seed.collect(), start=1):
    case_id = f"CASE-{i:04d}"
    title = f"Medium alert on {r['account_id']} — {(r['scenario_label'] or 'review').replace('_', ' ')}"
    desc = _re.sub(r"Severity \w+\.", "Severity medium.", r["narrative"] or "")
    pg_exec(connect,
        """INSERT INTO cases (case_id, title, description, severity, status, indicator_value,
             indicator_type, account_id, source, scenario_label, created_at, updated_at)
           VALUES (%s,%s,%s,'medium','new',%s,%s,%s,'tines',%s,%s, now())
           ON CONFLICT (case_id) DO NOTHING""",
        (case_id, title, desc, r["indicator_value"], r["indicator_type"], r["account_id"],
         r["scenario_label"], r["created_at"]))

n = pg_query(connect, "SELECT count(*) AS c FROM cases")[0]["c"]
print(f"seeded {n} medium demo cases into Lakebase")

# COMMAND ----------
dbutils.notebook.exit(f"demo data seeded: substrate loaded into {ctx.fqschema}; {n} cases in Lakebase.")
