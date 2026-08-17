"""Tier 1 — the UC-function tool adapter (lib/tools.make_tool_fn).

Pins the two things that matter: the SQL statement it builds (catalog.schema.<tool>(<literal>)) and the
single-quote escaping of the argument — indicators are URLs full of quotes/`?`/`%`, so a broken escape is
both a bug and an injection risk. The `sql` runner is a fake that just records the statement.
"""
from lib.tools import make_tool_fn


class FakeSql:
    def __init__(self):
        self.statements = []

    def query(self, statement):
        self.statements.append(statement)
        return [{"echo": statement}]


def test_builds_qualified_call_with_quoted_literal():
    sql = FakeSql()
    fn = make_tool_fn(sql, "cat", "sch")
    fn("enrich_indicator", "http://bad.example/x")
    assert sql.statements == ["SELECT * FROM cat.sch.enrich_indicator('http://bad.example/x')"]


def test_single_quotes_in_value_are_doubled():
    sql = FakeSql()
    fn = make_tool_fn(sql, "cat", "sch")
    fn("get_account_actions", "O'Brien")
    assert sql.statements[0] == "SELECT * FROM cat.sch.get_account_actions('O''Brien')"


def test_url_with_query_string_is_passed_intact():
    sql = FakeSql()
    fn = make_tool_fn(sql, "c", "s")
    fn("enrich_indicator", "http://x/y?a=1&b='2'")
    assert sql.statements[0] == "SELECT * FROM c.s.enrich_indicator('http://x/y?a=1&b=''2''')"


def test_unknown_tool_returns_error_without_touching_sql():
    sql = FakeSql()
    fn = make_tool_fn(sql, "c", "s")
    out = fn("drop_tables", "x")
    assert out == [{"error": "unknown tool drop_tables"}]
    assert sql.statements == []
