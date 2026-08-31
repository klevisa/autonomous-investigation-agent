"""Tools — how an investigation runs a UC-function tool, and the SQL backend it runs on.

Two concerns, co-located because they're the two halves of "call a tool":

  * SqlRunner  — the ONE thing that differs between deployment targets. A job/notebook has a live Spark
    session (SparkSqlRunner); an endpoint/app/laptop has no Spark, so it goes through the SQL Statement
    Execution API on a warehouse (WarehouseSqlRunner). Both expose the same tiny contract:
        runner.query(statement) -> list[dict]   # rows as dicts
        runner.exec(statement)  -> None          # a write, no rows expected
    Pick a runner in the driver; everything above this line is target-agnostic.

  * make_tool_fn — the adapter the Investigator calls to run ONE of AIA's 5 UC-function tools over a
    SqlRunner. Keeping the UC-function tools on Unity Catalog (not reimplementing them in Python) is what
    keeps governance / lineage / Genie over the analytic substrate unchanged.
"""
from lib.investigator import TOOL_ARG


class SparkSqlRunner:
    """Runs SQL on the ambient Spark session (job / notebook). No warehouse needed."""

    def __init__(self, spark):
        self._spark = spark

    def query(self, statement):
        return [r.asDict() for r in self._spark.sql(statement).collect()]

    def exec(self, statement):
        self._spark.sql(statement)


class WarehouseSqlRunner:
    """Runs SQL through the SQL Statement Execution API on a warehouse (endpoint / app / laptop).

    Uses a provided WorkspaceClient (so the caller controls identity — e.g. the endpoint's SP, or an
    OBO token). Raises on non-SUCCEEDED so a failed tool/write surfaces instead of masquerading as an
    empty result.
    """

    def __init__(self, workspace, warehouse_id, wait_timeout="50s"):
        self._w = workspace
        self._wid = warehouse_id
        self._wait = wait_timeout

    def _run(self, statement):
        r = self._w.statement_execution.execute_statement(
            warehouse_id=self._wid, statement=statement, wait_timeout=self._wait)
        state = r.status.state.value if r.status and r.status.state else "UNKNOWN"
        if state != "SUCCEEDED":
            err = getattr(r.status, "error", None)
            raise RuntimeError(f"SQL {state}: {getattr(err, 'message', '') if err else ''}")
        return r

    def query(self, statement):
        r = self._run(statement)
        cols = [c.name for c in r.manifest.schema.columns] if (
            r.manifest and r.manifest.schema and r.manifest.schema.columns) else []
        rows = r.result.data_array if (r.result and r.result.data_array) else []
        return [dict(zip(cols, row)) for row in rows]

    def exec(self, statement):
        self._run(statement)


def make_sql_runner(agent_mode, *, spark=None, workspace=None, warehouse_id=None):
    """Pick the SqlRunner for a job driver's agent_mode: `job_warehouse` runs on the SQL warehouse
    (WarehouseSqlRunner); anything else (i.e. plain `job`) runs on the ambient Spark session
    (SparkSqlRunner) — mirrors src/investigate.py's own if/else exactly, extracted here because that file
    is a Databricks notebook (dbutils/spark at module scope) and can't otherwise be unit tested."""
    if agent_mode == "job_warehouse":
        if not warehouse_id:
            raise ValueError("warehouse_id is required in job_warehouse mode (tools run on the warehouse).")
        return WarehouseSqlRunner(workspace, warehouse_id)
    return SparkSqlRunner(spark)


def make_tool_fn(sql, catalog, schema):
    """A tool_fn(name, value) the Investigator calls — runs the matching UC function via `sql`
    (a SqlRunner over Delta/UC: Spark in the job, warehouse in the endpoint)."""
    def _lit(v):
        return "'" + str(v).replace("'", "''") + "'"

    def tool_fn(name, value):
        if name not in TOOL_ARG:
            return [{"error": f"unknown tool {name}"}]
        return sql.query(f"SELECT * FROM {catalog}.{schema}.{name}({_lit(value)})")
    return tool_fn
