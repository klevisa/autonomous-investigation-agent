"""Tier 2 integration fixtures — a throwaway local Postgres.

Why local Postgres and not a mock: PostgresStateStore + journal are almost entirely SQL, and the point of
these tests is to prove that SQL is valid and behaves (parameterization, the journal authenticity JOIN,
record_verdict idempotency, reconcile queries). pg8000 speaks standard Postgres, so a local instance
exercises the real statements. Lakebase-specific bits (OAuth token mint, autoscaling host) are NOT here —
those stay in the e2e suite.

The instance is ephemeral: initdb into a temp dir, start on a free port with trust auth, drop it at the end.
No Docker, no network, no fixed port. If the postgres binaries aren't on PATH the whole suite is SKIPPED
(so `pytest` stays green on a machine without them); set AIA_TEST_PG_DSN to point at your own instead.

The tables are created with the SAME DDL the product ships in src/build_structure.py — kept in sync by
hand (small + rarely changes). If that DDL changes, update TABLES_DDL below.
"""
import os
import shutil
import socket
import subprocess
import tempfile
import time

import pytest

# The three operational tables, verbatim from src/build_structure.py (Postgres dialect).
TABLES_DDL = [
    """CREATE TABLE IF NOT EXISTS cases (
        case_id TEXT PRIMARY KEY, title TEXT, description TEXT, severity TEXT, status TEXT,
        indicator_value TEXT, indicator_type TEXT, account_id TEXT, source TEXT,
        assessed_severity TEXT, escalate_to_high BOOLEAN, latest_investigation_id TEXT,
        scenario_label TEXT, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS investigations (
        investigation_id TEXT PRIMARY KEY, case_id TEXT REFERENCES cases(case_id), status TEXT,
        assessed_severity TEXT, escalate_to_high BOOLEAN, recommended_play TEXT, confidence DOUBLE PRECISION,
        summary TEXT, rationale TEXT, evidence JSONB, tools_called JSONB, model_endpoint TEXT,
        job_run_id TEXT, attempts INT DEFAULT 0,
        started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, investigated_by TEXT)""",
    """CREATE TABLE IF NOT EXISTS investigation_events (
        event_id BIGSERIAL PRIMARY KEY,
        investigation_id TEXT NOT NULL, case_id TEXT, event_type TEXT NOT NULL,
        job_run_id TEXT, verdict JSONB, detail TEXT, applied_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT now())""",
]
_TABLES = ("investigation_events", "investigations", "cases")   # truncate order (children first)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _EphemeralPg:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="aia_pgtest_")
        self.data = os.path.join(self.dir, "data")
        self.port = _free_port()
        self.user = "postgres"
        self.database = "postgres"

    def start(self):
        subprocess.run(["initdb", "-D", self.data, "-U", self.user, "--auth=trust", "-E", "UTF8"],
                       check=True, capture_output=True)
        subprocess.run(["pg_ctl", "-D", self.data,
                        "-o", f"-p {self.port} -c listen_addresses=127.0.0.1 -c unix_socket_directories=''",
                        "-l", os.path.join(self.dir, "server.log"), "-w", "start"],
                       check=True, capture_output=True)

    def stop(self):
        subprocess.run(["pg_ctl", "-D", self.data, "-w", "-m", "immediate", "stop"],
                       capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def connect_factory(self):
        """A zero-arg callable returning a fresh pg8000 connection — the exact shape PostgresStateStore
        and lib/journal expect from lib.pg.make_pg_connect."""
        import pg8000.dbapi

        def connect():
            return pg8000.dbapi.connect(host="127.0.0.1", port=self.port,
                                        user=self.user, database=self.database)
        return connect


@pytest.fixture(scope="session")
def pg_server():
    if not (shutil.which("initdb") and shutil.which("pg_ctl")):
        pytest.skip("local Postgres (initdb/pg_ctl) not on PATH — set AIA_TEST_PG_DSN or install postgres")
    srv = _EphemeralPg()
    srv.start()
    # wait for readiness, then create schema
    connect = srv.connect_factory()
    for _ in range(50):
        try:
            connect().close()
            break
        except Exception:
            time.sleep(0.1)
    conn = connect()
    cur = conn.cursor()
    for ddl in TABLES_DDL:
        cur.execute(ddl)
    conn.commit()
    conn.close()
    yield srv
    srv.stop()


@pytest.fixture()
def connect(pg_server):
    """Per-test connect factory; truncates all tables first so each test starts clean."""
    factory = pg_server.connect_factory()
    conn = factory()
    cur = conn.cursor()
    cur.execute(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()
    return factory
