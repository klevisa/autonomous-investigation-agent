"""Runtime resolution of values that only exist AFTER the bundle is deployed.

The bundle is deployed ONCE (by CI) from a handful of static names AIA picks up front — catalog,
schema, warehouse, Lakebase project + database. Three things the app needs, though, do NOT exist until
after that first deploy:

  * the app's OWN service-principal id       (the Postgres user it logs in as),
  * the Lakebase endpoint HOST + endpoint path (created by scripts/setup.py, discovered — not chosen),
  * the `investigate` job id                 (assigned by Databricks when the job is created).

Rather than bake those into deploy-time env vars, the app
resolves them at STARTUP from things it already has: its injected OAuth client id, the Lakebase PROJECT
name, and the deployed job's NAME. Result: deploy once, then run scripts/setup.py — no redeploy.

Everything here is best-effort and fails LOUDLY: if Lakebase or the job isn't there yet (i.e. before
scripts/setup.py has run), resolution raises, and the app surfaces the error instead of silently pointing
at nothing. That's intended — the app should be inert until the infra step completes.
"""
import os


def app_sp_id():
    """The app's own service-principal client id — the Postgres user it authenticates as.

    The Databricks Apps runtime injects the app SP's OAuth client id as DATABRICKS_CLIENT_ID, so we
    never need it passed in as config. (Locally, set AIA_PG_USER to override.)"""
    return (os.environ.get("AIA_PG_USER")
            or os.environ.get("DATABRICKS_CLIENT_ID")
            or "").strip()


def endpoint_path(project, branch, endpoint):
    """The autoscaling endpoint path, assembled from the project + branch + endpoint names. This is what
    token minting + host lookup key off of.

    branch/endpoint are NOT defaulted here on purpose — the default ('production'/'primary', the ones
    Databricks auto-creates) lives once at the config layer (the lakebase_branch/lakebase_endpoint bundle
    variables in databricks.yml), so AIA can override them in one place. Callers pass them in."""
    return f"projects/{project}/branches/{branch}/endpoints/{endpoint}"


def pg_host(workspace, project, branch, endpoint):
    """Look up the Lakebase endpoint HOST for a project via the Postgres REST API.

    Uses api_client.do directly (not the typed workspace.postgres API) so this works on ANY SDK version
    — the serverless/app runtime can ship an older SDK that lacks the typed surface. Mirrors pg.py's
    token-minting approach. Returns the endpoint's host string, or raises if the endpoint isn't found.
    branch/endpoint are required (see endpoint_path re: where the default lives)."""
    parent = f"projects/{project}/branches/{branch}"
    resp = workspace.api_client.do("GET", f"/api/2.0/postgres/{parent}/endpoints")
    for ep in (resp.get("endpoints") or []):
        # match the endpoint by its resource name suffix
        if (ep.get("name") or "").endswith(f"/endpoints/{endpoint}"):
            host = (ep.get("status") or {}).get("hosts", {}).get("host")
            if host:
                return host
    raise RuntimeError(
        f"no host for endpoint '{endpoint}' under {parent} — has scripts/setup.py run for this env?")


def investigate_job_id(workspace, job_name):
    """Resolve the `investigate` job's id by its NAME (assigned at create time, not known pre-deploy).

    The bundle names the job deterministically (see databricks.yml), so the app finds it by name at
    startup rather than needing the numeric id baked in + a redeploy.

    DEV-MODE PREFIX: a DAB deployed with `mode: development` (the stage target) PREPENDS `[dev <user>] ` to
    every job name — but NOT to the app's env var (which is interpolated from ${bundle.target}, so it's the
    UNPREFIXED name). App resource names aren't prefixed, so the app resolves fine; the JOB name won't match
    exactly. `mode: production` (the CI/prod target) adds no prefix, so there an exact match works. To work in
    BOTH: try an exact match first (prod — one cheap server-side filter), then fall back to a SUFFIX match
    over all jobs (dev — the deployed name is `<[dev user] >` + our name). Raises if not found or ambiguous."""
    matches = [j for j in workspace.jobs.list(name=job_name)]   # server-side exact (prod path)
    if not matches:
        # dev-mode fallback: the real name is the configured name with a `[dev <user>] ` prefix.
        matches = [j for j in workspace.jobs.list() if (j.settings and j.settings.name or "").endswith(job_name)]
    if not matches:
        raise RuntimeError(f"no job named '{job_name}' — has the bundle been deployed to this env?")
    if len(matches) > 1:
        raise RuntimeError(f"{len(matches)} jobs named '{job_name}' — job name must be unique per workspace.")
    return matches[0].job_id
