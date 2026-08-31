"""Tier 1 — lib/tools.py WarehouseSqlRunner (SQL Statement Execution API adapter).

Offline: a fake WorkspaceClient.statement_execution returns scripted responses. Covers the
success→rows-as-dicts mapping (manifest columns zipped to data), the non-SUCCEEDED→raise behavior (a failed
tool must surface, not masquerade as empty), and the empty-result case. make_tool_fn itself is covered by
test_make_tool_fn.py.
"""
import types

import pytest

from lib.tools import WarehouseSqlRunner


def _resp(state="SUCCEEDED", cols=None, rows=None, err_msg=None):
    status = types.SimpleNamespace(
        state=types.SimpleNamespace(value=state),
        error=(types.SimpleNamespace(message=err_msg) if err_msg else None))
    manifest = (types.SimpleNamespace(schema=types.SimpleNamespace(
        columns=[types.SimpleNamespace(name=c) for c in cols])) if cols is not None else None)
    result = types.SimpleNamespace(data_array=rows)
    return types.SimpleNamespace(status=status, manifest=manifest, result=result)


class _FakeWS:
    def __init__(self, resp):
        self.captured = {}
        outer = self

        class _SE:
            def execute_statement(self, warehouse_id, statement, wait_timeout):
                outer.captured.update(warehouse_id=warehouse_id, statement=statement,
                                      wait_timeout=wait_timeout)
                return resp
        self.statement_execution = _SE()


def test_query_maps_columns_to_dicts():
    ws = _FakeWS(_resp("SUCCEEDED", cols=["ip", "score"], rows=[["1.2.3.4", 9], ["5.6.7.8", 1]]))
    runner = WarehouseSqlRunner(ws, "wh-42", wait_timeout="30s")
    out = runner.query("SELECT * FROM t")
    assert out == [{"ip": "1.2.3.4", "score": 9}, {"ip": "5.6.7.8", "score": 1}]
    assert ws.captured["warehouse_id"] == "wh-42"
    assert ws.captured["wait_timeout"] == "30s"


def test_query_empty_result_is_empty_list():
    ws = _FakeWS(_resp("SUCCEEDED", cols=None, rows=None))
    assert WarehouseSqlRunner(ws, "wh").query("SELECT 1") == []


def test_non_succeeded_raises_with_message():
    ws = _FakeWS(_resp("FAILED", err_msg="table not found"))
    with pytest.raises(RuntimeError, match="SQL FAILED: table not found"):
        WarehouseSqlRunner(ws, "wh").query("SELECT * FROM missing")


def test_exec_succeeds_silently():
    ws = _FakeWS(_resp("SUCCEEDED"))
    WarehouseSqlRunner(ws, "wh").exec("INSERT INTO t VALUES (1)")   # no raise, no return


def test_exec_raises_on_failure():
    ws = _FakeWS(_resp("CANCELED"))
    with pytest.raises(RuntimeError, match="SQL CANCELED"):
        WarehouseSqlRunner(ws, "wh").exec("INSERT INTO t VALUES (1)")
