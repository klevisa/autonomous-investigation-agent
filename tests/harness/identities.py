"""Account-admin identity primitives the harness needs but `databricks_ops` deliberately EXCLUDES.

`databricks_ops` is scoped to regular-user, post-deploy verbs — its docstring is explicit that it never creates
account principals or mints/stores secrets; that admin work is out-of-band. The test harness IS that
out-of-band admin, so these live here (test code), not in the product package. Where a mechanism already
exists in `databricks_ops`, we import it (e.g. `wait_for_sp_in_account_dir`) rather than duplicate.

All account-plane work goes through the WorkspaceClient's api_client on the WORKSPACE-PROXIED paths
(`/api/2.0/account/scim/...`, the rule-sets preview path) — the same access model databricks_ops/groups.py uses.

Rule-set writes: we provide a general etag-guarded read-modify-write here — one for group-target rule-sets
(e.g. group.manager) and one for sp-target rule-sets (servicePrincipal.user, which the deployer needs on the
job SP to set it as run_as). Both share `_rmw_rule_set`. (databricks_ops stays scoped to non-admin
post-deploy verbs, so this account-admin work lives in the harness.)
"""
from __future__ import annotations

import os
import re
import time
import urllib.parse

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import ResourceConflict

_RULESETS = "/api/2.0/preview/accounts/access-control/rule-sets"


# ── generic account rule-set read-modify-write (etag-guarded, idempotent add) ────────────────────
def _rmw_rule_set(w: WorkspaceClient, name: str, role: str, principal: str) -> None:
    """Add `principal` under `role` in the rule-set named `name`, preserving existing grants + etag.

    `name` is the fully-qualified rule-set name, e.g.
      accounts/<acct>/groups/<gid>/ruleSets/default          (group-target)
      accounts/<acct>/servicePrincipals/<spid>/ruleSets/default   (sp-target)
    Idempotent: if the principal already holds the role, it's a no-op.
    """
    current = w.api_client.do("GET", f"{_RULESETS}?name={urllib.parse.quote(name)}&etag=")
    rules = list(current.get("grant_rules") or [])
    for r in rules:
        if r.get("role") == role:
            if principal in (r.get("principals") or []):
                return   # already granted
            r["principals"] = (r.get("principals") or []) + [principal]
            break
    else:
        rules.append({"role": role, "principals": [principal]})
    body = {"name": name, "rule_set": {"name": name, "etag": current.get("etag", ""), "grant_rules": rules}}
    w.api_client.do("PUT", _RULESETS, body=body)


def grant_role_on_group(w: WorkspaceClient, account_id: str, group_id: str, role: str, principal: str) -> None:
    """Grant `principal` a role (e.g. roles/group.manager, roles/group.assumer) on a group's rule-set."""
    name = f"accounts/{account_id}/groups/{group_id}/ruleSets/default"
    _rmw_rule_set(w, name, role, principal)


def grant_role_on_sp(w: WorkspaceClient, account_id: str, sp_id: str, role: str, principal: str) -> None:
    """Grant `principal` a role (e.g. roles/servicePrincipal.user) on a service principal's rule-set."""
    name = f"accounts/{account_id}/servicePrincipals/{sp_id}/ruleSets/default"
    _rmw_rule_set(w, name, role, principal)


# ── service principals (find-or-create, entitle, secret) ─────────────────────────────────────────
# IMPORTANT: SPs are created + looked up in the WORKSPACE plane (w.service_principals), NOT via account
# SCIM. It matters: the OAuth token
# endpoint + the secret-proxy are workspace-scoped, so a secret minted for a WORKSPACE SP authenticates,
# while one minted for an account-SCIM-only SP fails `invalid_client`. Account-directory presence (needed
# for rule-set grants) then follows via wait_for_sp_in_account_dir.
def find_sp(w: WorkspaceClient, *, display_name: str = "", application_id: str = "") -> dict | None:
    """Find a WORKSPACE SP by displayName or applicationId. Returns {id, applicationId} or None."""
    for sp in w.service_principals.list():
        if (display_name and sp.display_name == display_name) or \
           (application_id and sp.application_id == application_id):
            return {"id": sp.id, "applicationId": sp.application_id}
    return None


def find_or_create_sp(w: WorkspaceClient, display_name: str) -> dict:
    """Return the WORKSPACE SP with this displayName, creating it if absent. Result has id + applicationId."""
    existing = find_sp(w, display_name=display_name)
    if existing:
        return existing
    sp = w.service_principals.create(display_name=display_name)
    return {"id": sp.id, "applicationId": sp.application_id}


def add_entitlements(w: WorkspaceClient, sp_id: str, entitlements: list[str]) -> None:
    """Add SCIM entitlements (e.g. workspace-access, databricks-sql-access) to an SP (account SCIM PATCH).

    Best-effort: some directories reject/ignore entitlement ops on an SP that
    already has them, or don't model them on account groups/SPs the same way — a failure here is not fatal
    to the setup, so we swallow it rather than abort."""
    try:
        w.api_client.do("PATCH", f"/api/2.0/account/scim/v2/ServicePrincipals/{sp_id}",
                        body={"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                              "Operations": [{"op": "add", "path": "entitlements",
                                              "value": [{"value": e} for e in entitlements]}]})
    except Exception:  # noqa: BLE001
        pass


def mint_oauth_secret(w: WorkspaceClient, sp_id: str) -> str:
    """Mint ONE OAuth M2M secret for an SP, first deleting any existing (the hard cap is 5). Returns it.

    Uses the SDK's typed WORKSPACE-level `service_principal_secrets_proxy` (the account-host secrets path is
    unreachable from a workspace profile). `sp_id` is the
    SP's numeric id (the proxy API takes the numeric id, NOT the applicationId).
    """
    proxy = w.service_principal_secrets_proxy
    for s in proxy.list(sp_id):
        proxy.delete(sp_id, s.id)
    return proxy.create(sp_id).secret or ""


def mint_additional_oauth_secret(w: WorkspaceClient, sp_id: str) -> str:
    """Mint an ADDITIONAL OAuth M2M secret for an SP WITHOUT deleting existing ones. Returns it.

    Use this when the SP already has a secret in use elsewhere that must stay valid — e.g. the CI SP's
    secret is set as a GitHub Actions secret and drives the CI deploy, so the harness can't wipe it just to
    get its own copy. Adding a secret leaves existing ones valid (the SP's cap is 5). `sp_id` is the numeric id.
    """
    return w.service_principal_secrets_proxy.create(sp_id).secret or ""


# ── account groups (find-or-create, workspace-assign, membership) ────────────────────────────────
def find_or_create_group(w: WorkspaceClient, display_name: str) -> dict:
    """Return the account group with this displayName, creating it if absent.

    A re-run right after a prior run created this group (e.g. a crash mid-phase) can find the GET empty —
    account SCIM is eventually consistent, same lag documented in databricks_ops/groups.py — then hit 409
    CONFLICT on create. That 409 itself proves the group exists, so on conflict we poll the GET instead of
    surfacing a create error for an object that's already there."""
    q = urllib.parse.quote(f'displayName eq "{display_name}"')

    def _find():
        resp = w.api_client.do("GET", f"/api/2.0/account/scim/v2/Groups?filter={q}")
        matches = resp.get("Resources") or []
        return matches[0] if matches else None

    found = _find()
    if found:
        return found
    try:
        return w.api_client.do("POST", "/api/2.0/account/scim/v2/Groups",
                               body={"displayName": display_name,
                                     "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"]})
    except ResourceConflict:
        for _ in range(6):
            found = _find()
            if found:
                return found
            time.sleep(5)
        raise RuntimeError(f"group {display_name!r}: create hit 409 (already exists) but never became "
                           f"visible via SCIM filter after polling — account-SCIM propagation lag exceeded")


def add_group_member(w: WorkspaceClient, group_id: str, member_id: str) -> None:
    """Add a member (by SCIM id) to an account group (idempotent-ish; SCIM tolerates re-add)."""
    w.api_client.do("PATCH", f"/api/2.0/account/scim/v2/Groups/{group_id}",
                    body={"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                          "Operations": [{"op": "add", "path": "members",
                                          "value": [{"value": member_id}]}]})


def add_group_entitlements(w: WorkspaceClient, group_id: str, entitlements: list[str]) -> None:
    """Add SCIM entitlements to an account group (workspace-access, databricks-sql-access).

    Best-effort: account groups may reject an `entitlements` PATCH outright
    ("Core attribute entitlements is undefined for schema ...Group") — the group still works via its
    workspace assignment + rule-set grants, so this is not fatal."""
    try:
        w.api_client.do("PATCH", f"/api/2.0/account/scim/v2/Groups/{group_id}",
                        body={"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                              "Operations": [{"op": "add", "path": "entitlements",
                                              "value": [{"value": e} for e in entitlements]}]})
    except Exception:  # noqa: BLE001
        pass


def assign_to_workspace(w: WorkspaceClient, account_id: str, workspace_id: str, principal_id: str) -> None:
    """Assign a principal (group or SP, by SCIM id) to the workspace as USER. Best-effort (idempotent)."""
    try:
        w.api_client.do(
            "PUT",
            f"/api/2.0/accounts/{account_id}/workspaces/{workspace_id}/permissionassignments/principals/{principal_id}",
            body={"permissions": ["USER"]})
    except Exception:  # noqa: BLE001 — fall back to the preview path
        try:
            w.api_client.do("PUT", f"/api/2.0/preview/permissionassignments/principals/{principal_id}",
                            body={"permissions": ["USER"]})
        except Exception:  # noqa: BLE001 — already assigned / not permitted → leave as-is
            pass


# ── Unity Catalog grants (via the workspace-proxied UC permissions PATCH) ────────────────────────
def _uc_patch_with_principal_retry(w: WorkspaceClient, path: str, principal: str, privileges: list[str],
                                   tries: int = 30, delay: int = 6) -> None:
    """PATCH a UC permissions endpoint, retrying while the error is specifically that the PRINCIPAL isn't
    found yet. A just-created account group is not immediately resolvable as a UC grantee — the grant fails
    'Could not find principal with name <group>' for 1-3 min after creation (verified: the create-role →
    grant-UC sequence in prereqs). Poll through that window; re-raise any OTHER error immediately (a real
    misconfig — wrong catalog, missing privilege name — must stay loud, not get swallowed by the retry)."""
    for attempt in range(tries):
        try:
            w.api_client.do("PATCH", path, body={"changes": [{"principal": principal, "add": privileges}]})
            return
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "find principal" not in msg and "not found" not in msg.lower():
                raise
            if attempt == tries - 1:
                raise RuntimeError(f"UC grant to principal {principal!r} still failing after {tries} tries "
                                   f"(~{tries * delay}s) — propagation lag or a genuinely-missing principal: {msg}")
            time.sleep(delay)


def uc_grant_catalog(w: WorkspaceClient, catalog: str, principal: str, privileges: list[str]) -> None:
    _uc_patch_with_principal_retry(w, f"/api/2.1/unity-catalog/permissions/catalog/{catalog}",
                                   principal, privileges)


def uc_grant_schema(w: WorkspaceClient, catalog: str, schema: str, principal: str, privileges: list[str]) -> None:
    _uc_patch_with_principal_retry(w, f"/api/2.1/unity-catalog/permissions/schema/{catalog}.{schema}",
                                   principal, privileges)


def _grant_warehouse_can_use(w: WorkspaceClient, warehouse_id: str, acl_entry: dict, label: str,
                             tries: int = 30, delay: int = 6) -> None:
    """PATCH a warehouse CAN_USE grant, retrying while the principal isn't resolvable yet — a just-created
    account group OR service principal takes 1-3 min to become a usable grantee here, and this runs right
    after the principal is created in prereqs. Other errors re-raise immediately."""
    for attempt in range(tries):
        try:
            w.api_client.do("PATCH", f"/api/2.0/permissions/warehouses/{warehouse_id}",
                            body={"access_control_list": [{**acl_entry, "permission_level": "CAN_USE"}]})
            return
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "find principal" not in msg and "not found" not in msg.lower() and "does not exist" not in msg.lower():
                raise
            if attempt == tries - 1:
                raise RuntimeError(f"warehouse CAN_USE grant to {label} still failing after "
                                   f"{tries} tries (~{tries * delay}s): {msg}")
            time.sleep(delay)


def grant_warehouse_can_use(w: WorkspaceClient, warehouse_id: str, group_name: str,
                            tries: int = 30, delay: int = 6) -> None:
    """Grant an account GROUP CAN_USE on a SQL warehouse."""
    _grant_warehouse_can_use(w, warehouse_id, {"group_name": group_name}, f"group {group_name!r}", tries, delay)


def grant_warehouse_can_use_sp(w: WorkspaceClient, warehouse_id: str, sp_app: str,
                               tries: int = 30, delay: int = 6) -> None:
    """Grant a SERVICE PRINCIPAL (by applicationId) CAN_USE on a SQL warehouse. Used for the app SP
    (in_process) and the job SP (job_warehouse) — both hit the warehouse as themselves, not via a group."""
    _grant_warehouse_can_use(w, warehouse_id, {"service_principal_name": sp_app}, f"SP {sp_app}", tries, delay)


def write_cli_profile(profile: str, *, host: str, client_id: str, client_secret: str, account_id: str) -> None:
    """Create-or-replace an OAuth M2M (client_credentials) profile section in ~/.databrickscfg.

    Two things this must get right (both cost a live debugging round):
      * set `auth_type = oauth-m2m` EXPLICITLY. If ~/.databrickscfg has a `[DEFAULT]` section with
        `auth_type = databricks-cli` (common on a dev box), every profile INHERITS it and the CLI tries
        interactive OAuth instead of M2M → "cannot configure default credentials". An explicit auth_type
        on the section overrides the inherited default.
      * edit ONLY this section textually — do NOT round-trip the whole file through configparser, which
        would inline `[DEFAULT]` into every other section (corrupting them).
    """
    path = os.path.expanduser("~/.databrickscfg")
    section = (
        f"[{profile}]\n"
        f"host = {host}\n"
        f"client_id = {client_id}\n"
        f"client_secret = {client_secret}\n"
        f"account_id = {account_id}\n"
        f"auth_type = oauth-m2m\n"
    )
    existing = open(path).read() if os.path.exists(path) else ""
    # drop any prior [profile] section (from header to the next section header or EOF), then append fresh.
    pat = re.compile(rf"(?ms)^\[{re.escape(profile)}\]\n.*?(?=^\[|\Z)")
    cleaned = pat.sub("", existing)
    if cleaned and not cleaned.endswith("\n"):
        cleaned += "\n"
    with open(path, "w") as f:
        f.write(cleaned + ("\n" if cleaned and not cleaned.endswith("\n\n") else "") + section)
