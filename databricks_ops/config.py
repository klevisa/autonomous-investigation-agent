"""Read the bundle's per-target variables out of config.yml — the readable-Python replacement for the old
`config-env` CLI verb (which printed `export KEY=VALUE` lines for bash to `eval`). The deploy/setup recipes
now import this and get a plain dict, so there is no shell round-trip and one config source of truth
(config.yml, the same file the DAB reads via `include`).
"""
from __future__ import annotations

import yaml


def load_target_variables(path: str, target: str) -> dict:
    """The raw {var: value} map under targets.<target>.variables in a config.yml."""
    with open(path) as f:
        doc = yaml.safe_load(f)
    return doc["targets"][target]["variables"]


def load_config(path: str, target: str) -> dict:
    """Return {UPPER_KEY: value} for a target. Flattens the two shapes a bundle variable can take —
    a plain scalar (the generated config.yml) or a `{default: ...}` object (databricks.yml style) — so the
    recipes can read cfg["LAKEBASE_PROJECT"] etc. uniformly."""
    out = {}
    for key, val in load_target_variables(path, target).items():
        value = val.get("default", "") if isinstance(val, dict) else (val if val is not None else "")
        out[key.upper()] = value
    return out
