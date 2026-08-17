"""databricks_ops — readable Python for the AIA deploy/setup orchestration + role grants.

These are generic Databricks control-plane operations (Lakebase provisioning, account SCIM, rule-set grants,
UC/warehouse grants), so they're named for what they operate on, not the team that uses them. They're plain
importable functions — there is no CLI. The multi-step recipes that call them live in readable Python
(scripts/deploy.py, scripts/setup.py) and in the CI workflow, so the *sequence* is visible in the caller
rather than hidden behind argparse dispatch.

Uses the Databricks SDK's typed APIs wherever they exist; drops to raw REST only for the autoscaling-Lakebase
`postgres` endpoints (no typed surface) and the workspace-proxied account paths (SCIM + rule-sets, reachable
by a regular deployer). Functions take an injected WorkspaceClient so the identity running each step is
explicit — see dbx.workspace().
"""
