"""Tier 1 — databricks_ops/config.py bundle-variable reader.

Offline: writes a small config.yml to tmp_path and asserts load_config flattens BOTH variable shapes a
bundle uses — a plain scalar (generated config.yml) and a `{default: ...}` object (databricks.yml style) —
uppercases keys, and coerces None to "".
"""
import textwrap

import pytest

from databricks_ops import config as cfgmod


def _write(tmp_path, text):
    p = tmp_path / "config.yml"
    p.write_text(textwrap.dedent(text))
    return str(p)


def test_load_config_flattens_scalars_and_defaults(tmp_path):
    path = _write(tmp_path, """
        targets:
          prod:
            variables:
              catalog: main
              schema: {default: aia}
              warehouse_id:
              lakebase_project: proj
        """)
    cfg = cfgmod.load_config(path, "prod")
    assert cfg["CATALOG"] == "main"           # plain scalar
    assert cfg["SCHEMA"] == "aia"             # {default: ...} object → its default
    assert cfg["WAREHOUSE_ID"] == ""          # null/empty → ""
    assert cfg["LAKEBASE_PROJECT"] == "proj"


def test_load_config_uppercases_keys(tmp_path):
    path = _write(tmp_path, """
        targets:
          stage:
            variables:
              agent_mode: job_warehouse
        """)
    cfg = cfgmod.load_config(path, "stage")
    assert "AGENT_MODE" in cfg and "agent_mode" not in cfg
    assert cfg["AGENT_MODE"] == "job_warehouse"


def test_load_config_default_missing_in_dict_is_empty(tmp_path):
    # a dict variable without a `default` key → "" (val.get("default", ""))
    path = _write(tmp_path, """
        targets:
          prod:
            variables:
              weird: {description: no-default-here}
        """)
    assert cfgmod.load_config(path, "prod")["WEIRD"] == ""


def test_load_target_variables_raw(tmp_path):
    path = _write(tmp_path, """
        targets:
          prod:
            variables:
              a: 1
              b: {default: 2}
        """)
    raw = cfgmod.load_target_variables(path, "prod")
    assert raw == {"a": 1, "b": {"default": 2}}


def test_missing_target_raises(tmp_path):
    path = _write(tmp_path, """
        targets:
          prod:
            variables: {a: 1}
        """)
    with pytest.raises(KeyError):
        cfgmod.load_target_variables(path, "does-not-exist")
