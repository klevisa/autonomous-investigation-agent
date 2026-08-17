"""Provision the autoscaling Lakebase project + its cases/investigations Postgres database.

NOTE on the SDK: the typed `w.database` service covers
*provisioned* Database Instances, NOT the *autoscaling* `postgres` project/endpoint model this PoC uses
(create-project / create-database / list-endpoints). Those have no typed SDK surface, so we call them
through the SDK's own HTTP client (`w.api_client.do`) — same endpoints the CLI wraps, but in-process and
without shelling out. That's the one place here we can't use a typed wrapper.

NO Unity Catalog registration. The app reaches Postgres DIRECTLY via pg8000 + an OAuth token (its SP mapped
to a Postgres role — see src/build_structure.py + lib/pg.py). A UC catalog would only surface the tables
for Genie/SQL browsing, which the app never uses — and registering one needs CREATE CATALOG on the metastore
(a metastore-admin grant a regular deployer won't have). So we create the Postgres database natively and skip
UC entirely.
"""
from __future__ import annotations

import time

from databricks.sdk import WorkspaceClient


def _do(w: WorkspaceClient, method: str, path: str, body: dict | None = None, query: dict | None = None):
    return w.api_client.do(method, path, body=body, query=query)


def provision(w: WorkspaceClient, project: str, pg_database: str, *,
              branch: str = "production", endpoint: str = "primary",
              min_cu: float = 0.5, max_cu: float = 2.0) -> dict:
    """Create the project (if absent), wait for its endpoint ACTIVE, set scale-to-zero, and create the
    Postgres database (if absent). Idempotent-ish (reuses an existing project/database). Returns a summary.

    The endpoint host/path are NOT returned — every consumer derives them from the project name at runtime
    (lib/resolve.py), so there's nothing to capture or let go stale.
    """
    branch_path = f"projects/{project}/branches/{branch}"
    endpoint_path = f"{branch_path}/endpoints/{endpoint}"

    # 1. project. Reuse a LIVE project; but a SOFT-DELETED one (has `delete_time`, kept until `purge_time`)
    #    is NOT usable — its endpoints never come back ACTIVE — yet GET still returns it 200. So if we find a
    #    soft-deleted project under this name we PURGE it first (?purge=true frees the reserved name
    #    immediately) and then recreate. Without this, a teardown→rerun within the 7-day purge window hangs
    #    forever polling a dead endpoint.
    live = False
    try:
        proj = _do(w, "GET", f"/api/2.0/postgres/projects/{project}") or {}
        if proj.get("delete_time"):
            print(f"  project {project} is soft-deleted — purging to free the name before recreating")
            try:
                _do(w, "DELETE", f"/api/2.0/postgres/projects/{project}", query={"purge": "true"})
            except Exception as e:
                print(f"  (purge best-effort: {e})")
        else:
            live = True
    except Exception:
        live = False
    if not live:
        _do(w, "POST", "/api/2.0/postgres/projects",
            body={"spec": {"display_name": "AIA cases + investigations"}},
            query={"project_id": project})

    # 2. wait for the primary endpoint to be ACTIVE. A JUST-created project doesn't materialize its
    #    branch/endpoints subresources instantly — GET .../endpoints can 404 ("Project ... not found") for a
    #    short window right after the create POST. That's a transient not-ready, not a real error, so we
    #    tolerate any exception here and keep polling; only a genuine timeout (never reaching ACTIVE) fails.
    state = None
    for _ in range(30):
        try:
            eps = (_do(w, "GET", f"/api/2.0/postgres/{branch_path}/endpoints") or {}).get("endpoints", [])
            state = (eps[0].get("status", {}).get("current_state") if eps else None)
        except Exception:
            state = None   # project/endpoint not queryable yet — keep waiting
        if state == "ACTIVE":
            break
        time.sleep(10)
    if state != "ACTIVE":
        raise RuntimeError(f"Lakebase endpoint not ACTIVE (last state: {state})")

    # 3. scale-to-zero autoscaling limits
    _do(w, "PATCH", f"/api/2.0/postgres/{endpoint_path}",
        body={"spec": {"autoscaling_limit_min_cu": min_cu, "autoscaling_limit_max_cu": max_cu}},
        query={"update_mask": "spec.autoscaling_limit_min_cu,spec.autoscaling_limit_max_cu"})

    # 4. create the Postgres DATABASE (the app connects to it via pg8000). No UC catalog — see the module
    #    docstring. A new database must be OWNED by a Postgres role; the branch auto-creates a role for the
    #    provisioning identity (sp-<app-id> for an SP), so we own it with that. Idempotent: reuse if present.
    created = _ensure_pg_database(w, branch_path, pg_database)
    return {"project": project, "pg_database": pg_database, "endpoint_state": state, "database": created}


def _ensure_pg_database(w: WorkspaceClient, branch_path: str, pg_database: str) -> str:
    """Create the named Postgres database on the branch if it doesn't exist; return 'created' or 'reused'.
    The database's owning role must be the provisioning identity's branch role (`.../roles/<role_id>`)."""
    dbs = (_do(w, "GET", f"/api/2.0/postgres/{branch_path}/databases") or {}).get("databases", [])
    # the postgres name lives under status (spec is often empty on read); check both to be safe
    def _pgname(d): return d.get("status", {}).get("postgres_database") or d.get("spec", {}).get("postgres_database")
    if any(_pgname(d) == pg_database for d in dbs):
        return "reused"
    role = _own_branch_role(w, branch_path)
    _do(w, "POST", f"/api/2.0/postgres/{branch_path}/databases",
        body={"spec": {"postgres_database": pg_database, "role": role}})
    return "created"


def _own_branch_role(w: WorkspaceClient, branch_path: str) -> str:
    """The full path of the provisioning identity's Postgres role on the branch (auto-created when the
    project is provisioned). Prefers the caller's own role (sp-<app-id> / the user's role); falls back to
    the first role on the branch. Raises if the branch has no roles yet."""
    roles = (_do(w, "GET", f"/api/2.0/postgres/{branch_path}/roles") or {}).get("roles", [])
    if not roles:
        raise RuntimeError(f"no Postgres roles on {branch_path} yet — cannot own the new database.")
    try:
        me = w.current_user.me()
        ident = (getattr(me, "user_name", None) or "").lower()
        # an SP's branch role is sp-<application_id>; a user's role is their user name
        for r in roles:
            rid = r.get("role_id", "")
            if ident and ident in rid.lower():
                return f"{branch_path}/roles/{rid}"
    except Exception:
        pass
    return f"{branch_path}/roles/{roles[0].get('role_id')}"
