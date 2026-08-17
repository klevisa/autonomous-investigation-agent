"""common — generic naming/context helpers for the notebooks. A plain importable module
(`from common import ...`) — no notebook `%run`, nothing runs at import time; every function takes
`spark` as an argument.

This module is DATA-AGNOSTIC on purpose: it holds only Ctx (catalog/schema/identity), no table lists.
The PoC's demo substrate table registry lives in its own module, `demo_substrate`, so AIA can swap the
tables without touching this generic layer. The schema all AIA PoC objects live in is supplied
per-deploy via the `schema` bundle variable — never hardcoded here.
"""


class Ctx:
    """The names every notebook needs: me, catalog, schema. A tiny PURE value object — no I/O in the
    constructor. Identity (`me`) is passed IN by the caller, which derives it from the SDK
    (`WorkspaceClient().current_user.me().user_name`), NOT from Spark: Spark is reserved for the
    SparkSqlRunner tool path, so a notebook's own identity must come from the control plane. `me` is the
    setup identity = the Lakebase Postgres user (same value the app uses)."""

    def __init__(self, me, catalog, schema):
        if not catalog or not schema:
            raise ValueError("catalog and schema are required (set var.catalog / var.schema in config.yml).")
        self.me = me
        self.catalog = catalog
        self.schema = schema

    @property
    def fqschema(self):
        return f"{self.catalog}.{self.schema}"

    def table(self, name):
        return f"{self.catalog}.{self.schema}.{name}"

    def __repr__(self):
        return f"Ctx(me={self.me}, catalog={self.catalog}, schema={self.schema})"


def ctx_for(me, catalog, schema):
    return Ctx(me, catalog, schema)
