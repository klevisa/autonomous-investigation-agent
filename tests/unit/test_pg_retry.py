"""Tier 1 — lib/pg.py connection resilience (connect timeout + bounded retry).

Offline: we drive make_pg_connect with a fake WorkspaceClient (token mint) and monkeypatch pg8000's connect,
so no real Lakebase/network. Asserts a cold endpoint that fails a couple of times then recovers, that the
connect timeout is applied, and that a persistent failure re-raises loudly (doesn't loop forever).
"""
import pg8000.dbapi
import pytest

from lib import pg as pgmod
from lib.pg import make_pg_connect


class _FakeWS:
    """Minimal WorkspaceClient stand-in — only the token-mint path is exercised."""
    class _AC:
        def do(self, *a, **k):
            return {"token": "tok"}
    api_client = _AC()


def _factory():
    # All coords passed explicitly (no env reads); schema="" so connect() skips the search_path cursor.
    return make_pg_connect(_FakeWS(), host="h", database="d", user="u", endpoint_path="p", schema="")


def test_connect_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sentinel = object()

    def fake_connect(**kwargs):
        calls["n"] += 1
        assert kwargs["timeout"] == pgmod._CONNECT_TIMEOUT   # the connect timeout is applied (no infinite hang)
        if calls["n"] < 3:
            raise ConnectionError("cold endpoint resuming")
        return sentinel

    monkeypatch.setattr(pg8000.dbapi, "connect", fake_connect)
    monkeypatch.setattr(pgmod.time, "sleep", lambda s: None)   # no real backoff sleeping

    conn = _factory()()
    assert conn is sentinel
    assert calls["n"] == 3


def test_connect_reraises_after_exhausting(monkeypatch):
    def fake_connect(**kwargs):
        raise ConnectionError("still cold")

    monkeypatch.setattr(pg8000.dbapi, "connect", fake_connect)
    monkeypatch.setattr(pgmod.time, "sleep", lambda s: None)

    with pytest.raises(ConnectionError, match="still cold"):
        _factory()()
