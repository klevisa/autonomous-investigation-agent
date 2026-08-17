"""Client construction + small shared helpers for databricks_ops.

Every function takes an already
-built WorkspaceClient so the *identity* running each step is explicit and injectable in tests — nothing
here reaches for ambient/default auth on its own.

Account-scoped work (groups, rule-sets, account SCIM) is deliberately done through the WORKSPACE-PROXIED
REST paths (`/api/2.0/account/scim/v2/...`, `/api/2.0/preview/accounts/access-control/rule-sets`) on the
WorkspaceClient — NOT an AccountClient against the account host, which a regular (non-account-admin) deployer
cannot reach. See databricks_ops/groups.py.

Wherever the Databricks SDK exposes a typed method we use it (e.g. `w.warehouses.set_permissions`); we only
drop to raw REST for the few surfaces the SDK lacks (the proxied account paths above and the Lakebase
`postgres` endpoints).
"""
from __future__ import annotations

from databricks.sdk import WorkspaceClient


def workspace(profile: str | None = None) -> WorkspaceClient:
    """A WorkspaceClient for a CLI profile (its identity is who the calls run as). If `profile` is None/empty
    (e.g. in CI, where auth comes from DATABRICKS_HOST/CLIENT_ID/CLIENT_SECRET env vars, not a .databrickscfg
    profile), fall back to the SDK's default env-var auth — `WorkspaceClient()` picks those up."""
    return WorkspaceClient(profile=profile) if profile else WorkspaceClient()
