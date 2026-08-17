"""The AIA role (an account group) — resolve its id + wait for SP propagation, for MEMBERSHIP grants.

The app SP and the job SP are made MEMBERS of this role (membership → inherits the evidence SELECT / tool
EXECUTE grants); the membership PATCH itself lives in tests/harness/identities.add_group_member. This module
provides the two reads that membership needs. Facts, all verified empirically:
  * A REGULAR (non-account-admin) user reaches account SCIM **only through the WORKSPACE-PROXIED REST paths**
    (`/api/2.0/account/scim/v2/...`) — NOT through the AccountClient (account host), which 404s for a
    workspace identity. So everything here goes through the WorkspaceClient's api_client, which carries the
    workspace token the proxy accepts.
  * A freshly-created account group / service principal takes ~1-3 min to propagate to the account directory
    that account-plane grants (group membership) validate against — hence the poll/wait helpers below.
"""
from __future__ import annotations

import time
import urllib.parse

from databricks.sdk import WorkspaceClient


def wait_for_sp_in_account_dir(w: WorkspaceClient, sp_application_id: str,
                               tries: int = 6, delay: float = 10.0) -> None:
    """Block until a service principal is visible in the ACCOUNT SCIM directory (by applicationId).

    A newly-created SP (born at app deploy, or just created for job mode) exists in the WORKSPACE plane
    immediately but takes ~1-3 min to propagate to the account directory that account-plane grants
    (rule-sets, group membership) validate against — until then those writes fail 'ServicePrincipal ...
    not found' (verified 2026-08-04). This is the ONE named precondition every "created an SP, now grant
    against it in the account directory" site should call FIRST, so the grant itself is a single
    deterministic write with no retry loop swallowing real errors.

    Bounded poll (tries × delay, default ~60s). Raises if the SP never appears — a genuine setup error we
    want loud, not an infinite wait."""
    q = urllib.parse.quote(f'applicationId eq "{sp_application_id}"')
    path = f"/api/2.0/account/scim/v2/ServicePrincipals?filter={q}"
    for attempt in range(tries):
        try:
            if (w.api_client.do("GET", path).get("Resources") or []):
                return   # visible — safe to grant against it
        except Exception:
            pass         # transient read error — treat as not-yet-visible and keep polling
        if attempt < tries - 1:
            time.sleep(delay)
    raise RuntimeError(
        f"service principal {sp_application_id} not visible in the account directory after "
        f"{tries} tries — was it created? (account-SCIM propagation usually completes in ~1-3 min)")


def resolve_account_group_id(w: WorkspaceClient, role_group_name: str, tries: int = 30, delay: int = 6) -> str:
    """The account-group id for a role's display name (what membership grants target). Uses the
    workspace-proxied account SCIM (a regular user can reach it).

    A freshly-created account group is NOT immediately visible via account SCIM — propagation typically takes
    1-3 min. Since this is normally called shortly after the role group is created (e.g. adding a member SP
    right after prereqs create it), POLL for it rather than raising on the first empty result: an immediate raise
    makes the flow flaky (it passed or failed depending on how fast SCIM caught up). A genuinely-missing group
    (or an ambiguous name) is still a loud error after the poll window — we do not return a silent empty id."""
    q = urllib.parse.quote(f'displayName eq "{role_group_name}"')
    for attempt in range(tries):
        try:
            resp = w.api_client.do("GET", f"/api/2.0/account/scim/v2/Groups?filter={q}")
            matches = resp.get("Resources") or []
        except Exception:
            matches = []          # transient read error — treat as not-yet-visible and keep polling
        if len(matches) > 1:
            raise RuntimeError(f"{len(matches)} account groups named {role_group_name!r} — name must be unique")
        if matches:
            return matches[0]["id"]
        if attempt < tries - 1:
            time.sleep(delay)
    raise RuntimeError(f"no account group named {role_group_name!r} after {tries} tries "
                       f"(~{tries * delay}s) — was the AIA role created? (account-SCIM propagation lag)")
