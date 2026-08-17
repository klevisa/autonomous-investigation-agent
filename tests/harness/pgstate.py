"""Read AIA's Lakebase state (cases / investigations / journal) as the deployer.

Connects exactly the way the app does: via the repo's own `lib.pg` (pg8000 + a minted OAuth token), as the
DEPLOYER identity (which must be granted on the tables), with the AIA schema on the search_path. So this
imports `lib` (product code) rather than re-implementing a Postgres client.
"""
from __future__ import annotations

import time

from databricks.sdk import WorkspaceClient
from lib import resolve
from lib.pg import make_pg_connect, pg_exec, pg_query


def _retry(fn, *, tries: int = 5, delay: float = 6.0):
    """Retry a Lakebase op on transient cold-start failures. `lib.pg` connects fresh per call (new host
    resolve + OAuth mint + pg8000 connect), so against a scale-to-zero endpoint the FIRST ops after idle can
    raise a control-plane DeadlineExceeded / connection error while the DB spins up. These reads/writes are
    idempotent (a failed connect commits nothing), so retrying with a warm-up backoff is safe."""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — SDK DeadlineExceeded / pg8000 InterfaceError are transient here
            last = e
            if i < tries - 1:
                time.sleep(delay)
    raise last


class PgState:
    """A deployer-scoped reader for the AIA operational tables.

    Build it from the Config (it needs the deployer profile + the Lakebase coordinates), then call `query`
    for arbitrary reads or the scalar convenience readers the scenarios use.
    """

    def __init__(self, profile: str, *, schema: str, project: str, pg_database: str,
                 branch: str = "production", endpoint: str = "primary"):
        self.profile = profile
        self._schema = schema
        self._project = project
        self._pg_database = pg_database
        self._branch = branch
        self._endpoint = endpoint
        self._connect = None

    @classmethod
    def from_config(cls, cfg) -> "PgState":
        return cls(
            cfg.require("DEPLOYER_PROFILE"),
            schema=cfg.require("SCHEMA"),
            project=cfg.require("LAKEBASE_PROJECT"),
            pg_database=cfg.require("PG_DATABASE"),
            branch=cfg.get("LAKEBASE_BRANCH", "production"),
            endpoint=cfg.get("LAKEBASE_ENDPOINT", "primary"),
        )

    def _conn(self):
        if self._connect is None:
            w = WorkspaceClient(profile=self.profile)
            me = w.current_user.me().user_name   # the deployer is the Postgres user
            self._connect = make_pg_connect(
                w,
                host=resolve.pg_host(w, self._project, self._branch, self._endpoint),
                database=self._pg_database,
                user=me,
                endpoint_path=resolve.endpoint_path(self._project, self._branch, self._endpoint),
                schema=self._schema,
            )
        return self._connect

    def query(self, sql: str) -> list[dict]:
        """Run a read and return rows as a list of dicts."""
        return _retry(lambda: pg_query(self._conn(), sql))

    def execute(self, sql: str, params=()) -> None:
        """Run a write (INSERT/UPDATE/DDL) — the scenarios inject orphan rows / reset case state with these."""
        _retry(lambda: pg_exec(self._conn(), sql, params))

    def _scalar(self, sql: str, col: str):
        rows = self.query(sql)
        return rows[0][col] if rows else None

    def inv_field(self, inv_id: str, col: str):
        """Read one column from an investigations row (generic form of the inv_* readers)."""
        return self._scalar(f"SELECT {col} FROM investigations WHERE investigation_id='{inv_id}'", col)

    # scalar convenience readers
    def inv_status(self, inv_id: str):
        return self._scalar(f"SELECT status FROM investigations WHERE investigation_id='{inv_id}'", "status")

    def inv_attempts(self, inv_id: str):
        return self._scalar(f"SELECT attempts FROM investigations WHERE investigation_id='{inv_id}'", "attempts")

    def case_status(self, case_id: str):
        return self._scalar(f"SELECT status FROM cases WHERE case_id='{case_id}'", "status")

    def latest_inv_for(self, case_id: str):
        return self._scalar(
            f"SELECT investigation_id FROM investigations WHERE case_id='{case_id}' "
            f"ORDER BY started_at DESC LIMIT 1", "investigation_id")

    def case_count(self) -> int:
        rows = self.query("SELECT COUNT(*) AS n FROM cases")
        return int(rows[0]["n"]) if rows else 0
