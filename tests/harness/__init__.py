"""AIA test harness (Python) — the readable core the e2e test flow builds on.

Design:
  * SHARED product logic is IMPORTED from `databricks_ops` / `lib` (WorkspaceClient factory, Lakebase provision,
    the SCIM-wait gate, the job/dir grants) — never re-implemented here.
  * TEST-ONLY and ADMIN-ONLY concerns live here: the PASS/FAIL tally, the app REST client, Lakebase-state
    reads, config.env load + write-back, poll waiters, and the account-admin identity primitives that
    `databricks_ops` deliberately excludes (create the role group, mint the deployer/CI SP + its secret, write
    account rule-sets for group.manager / servicePrincipal.user / group.assumer).

Identity is always explicit: every operation carries the CLI profile it runs as (admin vs deployer) —
there is no ambient auth.
"""
