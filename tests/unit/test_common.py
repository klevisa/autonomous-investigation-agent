"""Tier 1 — lib/common.py Ctx value object (pure, no I/O)."""
import pytest

from lib.common import Ctx, ctx_for


def test_fqschema_and_table():
    c = Ctx("me@x", "cat", "sch")
    assert c.fqschema == "cat.sch"
    assert c.table("cases") == "cat.sch.cases"
    assert c.me == "me@x"


def test_ctx_for_factory():
    assert ctx_for("me", "c", "s").table("t") == "c.s.t"


@pytest.mark.parametrize("catalog,schema", [("", "s"), ("c", ""), (None, "s"), ("c", None)])
def test_missing_catalog_or_schema_raises(catalog, schema):
    with pytest.raises(ValueError, match="catalog and schema are required"):
        Ctx("me", catalog, schema)
