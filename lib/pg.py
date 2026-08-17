"""Postgres connection factory for autoscaling Lakebase.

Autoscaling Lakebase projects are NOT bindable as an app `database` resource (that binding only
resolves provisioned Database Instances). So the app/job/endpoint connects the way any client does:
resolve the endpoint host, mint a short-lived OAuth token, and open a Postgres connection.

Driver choice: **pg8000** — a pure-Python Postgres driver, so it installs on Databricks **serverless**
(which can't build C-extension packages like psycopg2). Verified connecting to the autoscaling
endpoint with an OAuth token.

OAuth tokens expire (~1h), so `make_pg_connect` mints a FRESH token per connection. The StateStore
opens a connection per investigation (seconds long), so token expiry inside one run is a non-issue.

Config comes from env (set by the app resource env / job params):
  AIA_PG_HOST           the endpoint host (ep-....database.<region>.cloud.databricks.com)
  AIA_PG_DATABASE       the Postgres database name (AIA-chosen; created in setup)
  AIA_PG_USER           the Postgres user (the caller identity — user email or the app SP id)
  AIA_PG_ENDPOINT_PATH  projects/<p>/branches/production/endpoints/primary  (for token minting)
  AIA_PG_SCHEMA         the Postgres schema holding cases/investigations (= the Delta schema name).
                         Set as the connection search_path, so every query in this repo can name the
                         tables unqualified (cases / investigations) and still resolve to AIA's schema
                         rather than public. Optional — if unset, connections default to public.
"""
import os
import ssl
import time

# Connection resilience for an autoscaling (scale-to-zero) Lakebase endpoint. Without a timeout, a cold or
# briefly-unreachable endpoint makes pg8000's connect BLOCK FOREVER (observed: a ~50-min hang in provisioning).
# So we bound each connect AND retry a few times so a cold endpoint can warm up across attempts instead of
# either hanging or failing the first request after idle.
#   _CONNECT_TIMEOUT — pg8000 socket timeout. It also bounds query time, which is fine here: every AIA query
#                      is a sub-second cases/investigations read/write (the agent's heavy work runs on the
#                      warehouse, not through pg8000), so 30s can only ever trip on a genuinely stuck socket.
#   retries/backoff  — a bounded, linear backoff (~30s of sleeps + up to N connect timeouts, so worst case is
#                      minutes, never infinite). A fresh OAuth token is minted on each attempt.
_CONNECT_TIMEOUT = 30
_CONNECT_RETRIES = 5
_CONNECT_BACKOFF = 3.0


def make_pg_connect(workspace, host=None, database=None, user=None, endpoint_path=None, schema=None):
    """Return connect() -> a fresh pg8000 connection, minting a new OAuth token each call.

    `workspace` is a databricks.sdk.WorkspaceClient (its identity is who the token is minted for, and
    must match `user`). Any arg left None is read from the AIA_PG_* env vars. `schema`, if given (or
    AIA_PG_SCHEMA), is applied as the session search_path so unqualified table names resolve to it.
    """
    host = host or os.environ["AIA_PG_HOST"]
    database = database or os.environ["AIA_PG_DATABASE"]
    user = user or os.environ["AIA_PG_USER"]
    endpoint_path = endpoint_path or os.environ["AIA_PG_ENDPOINT_PATH"]
    schema = schema or os.environ.get("AIA_PG_SCHEMA", "").strip()
    ssl_ctx = ssl.create_default_context()

    def _mint_token():
        # Mint a Lakebase OAuth token. Use the REST endpoint directly via api_client.do so this works
        # on ANY SDK version — the typed `workspace.postgres` API only exists in newer SDKs (serverless
        # runtimes can ship an older one that lacks it). Falls back to the typed API if present.
        try:
            resp = workspace.api_client.do(
                "POST", "/api/2.0/postgres/credentials", body={"endpoint": endpoint_path})
            return resp["token"]
        except Exception:
            return workspace.postgres.generate_database_credential(endpoint_path).token

    def _open_once():
        import pg8000.dbapi   # imported lazily so pg_exec/pg_query + this module stay importable
        conn = pg8000.dbapi.connect(host=host, port=5432, database=database,
                                    user=user, password=_mint_token(), ssl_context=ssl_ctx,
                                    timeout=_CONNECT_TIMEOUT)
        if schema:
            # Resolve unqualified table names to AIA's schema (then public). Identifier can't be bound
            # as a parameter; quote it so mixed-case/reserved names are safe. schema comes from config,
            # not user input.
            cur = conn.cursor()
            cur.execute(f'SET search_path TO "{schema}", public')
            conn.commit()
        return conn

    def connect():
        # Retry the whole open (token mint + pg8000 connect + search_path) on any failure: a scale-to-zero
        # endpoint resuming from cold, or a transient control-plane blip while minting the token, clears on a
        # later attempt. Bounded — re-raise the last error once attempts are exhausted so a real misconfig
        # (bad user/database/credential) still surfaces loudly rather than looping.
        last = None
        for i in range(_CONNECT_RETRIES):
            try:
                return _open_once()
            except Exception as e:  # noqa: BLE001 — pg8000/SDK errors on a cold endpoint are transient here
                last = e
                if i < _CONNECT_RETRIES - 1:
                    time.sleep(_CONNECT_BACKOFF * (i + 1))
        raise last

    return connect


# Small run-one-statement helpers over a `connect` callable — shared by the build/seed notebooks and by
# PostgresStateStore, so the open→run→commit→close pattern lives in one place. Each opens a short-lived
# connection (autoscaling Lakebase resumes fast; a fresh OAuth token is minted per connect).
def pg_exec(connect, sql, params=()):
    """Run a write (INSERT/UPDATE/DDL); no rows expected."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def pg_query(connect, sql, params=()):
    """Run a statement and return any result rows as a list of dicts. A statement with no result set
    (INSERT/UPDATE/DDL) returns [] — `cur.description` is None there, and calling fetchall() would raise
    ("attempting to use unexecuted cursor"). The app only SELECTs through this; the test harness (PgState)
    also uses it to inject/adjust rows, so tolerating non-SELECT keeps one code path for both."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        if cur.description is None:          # no result set (INSERT/UPDATE/DDL)
            conn.commit()
            return []
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        conn.commit()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()
