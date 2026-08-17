#!/usr/bin/env python3
"""ADMIN, ONE-TIME: provision the AWS side of the LLM token + the UC service credential,
then rotate a fresh gateway token into the secret. This is the whole AWS-Secrets-Manager design 
: the one-time "create" (secret + IAM role + UC credential) AND the per-round token
ROTATION (step 5) — both here, since both need AWS access. Idempotent — safe to re-run.

    Prereqs: the shell must be authed to AWS (export AWS_PROFILE=<profile>; aws sso login) and ADMIN_PROFILE
             must hold CREATE_SERVICE_CREDENTIAL on the metastore.
    python3 -m tests.e2e.admin_aws_secret

Creates (all idempotent): (1) a Secrets Manager secret, (2) an IAM role whose trust lets the UC master role
(+ itself) assume it gated on the credential external_id, plus a GetSecretValue policy on ONLY that secret,
(3) a UC SERVICE credential, then RECONCILES the trust to the Databricks-generated external_id and VALIDATES.

THE CREATE HANDSHAKE (non-obvious): create-credential must NOT be given unity_catalog_iam_arn (rejected).
Pass only role_arn + skip_validation; Databricks returns a credential-specific external_id + the UC master
arn; only THEN write the AWS trust to that external_id and validate. Wrong order → validation fails.
"""
import json
import os
import subprocess
import sys
import time

from tests.harness import config, dbx, report


def aws(*args: str, region: str, profile: str, check: bool = True) -> str:
    """Run an `aws` CLI command with the setup profile+region; return stdout (stripped).

    On failure (check=True), surface the AWS stderr — the actual error (e.g. MalformedPolicyDocument) — rather
    than letting CalledProcessError print only a bare exit code and the (secret-free) command args.
    """
    env = {**os.environ, "AWS_PROFILE": profile, "AWS_DEFAULT_REGION": region}
    cp = subprocess.run(["aws", *args], env=env, text=True, capture_output=True)
    if check and cp.returncode != 0:
        sys.exit(f"aws {args[0]} {args[1] if len(args) > 1 else ''} failed (exit {cp.returncode}): "
                 f"{cp.stderr.strip() or cp.stdout.strip()}")
    return cp.stdout.strip()


def rotate_llm_token(cfg, admin: str, *, arn: str, region: str, key: str, aws_profile: str) -> None:
    """Store a FRESH short-lived Databricks PAT as the gateway token in the secret — a TEST convenience so
    self-contained runs always have a live token (this is the per-round ROTATION half of the AWS design).
    Non-fatal: a sandbox admin role may lack PutSecretValue, but the existing secret still works at runtime,
    so we warn and continue rather than fail the whole setup."""
    report.step("5) rotate a fresh LLM token (short-lived Databricks PAT) into the secret")
    lifetime = cfg.get("AIA_LLM_TOKEN_LIFETIME_SECONDS", "86400")
    tok = dbx.cli_json(admin, "tokens", "create", "--lifetime-seconds", lifetime,
                       "--comment", f"aia-llm-{cfg.get('TEST_SUFFIX')}")
    token = (tok or {}).get("token_value", "")
    if not token:
        print("  (could not mint a Databricks PAT — check token-usage permission; leaving the existing secret)")
        return
    cp = subprocess.run(["aws", "secretsmanager", "put-secret-value", "--secret-id", arn,
                         "--secret-string", json.dumps({key: token})],
                        env={**os.environ, "AWS_PROFILE": aws_profile, "AWS_DEFAULT_REGION": region},
                        text=True, capture_output=True)
    if cp.returncode == 0:
        print(f"  rotated: fresh short-lived PAT stored in Secrets Manager under key '{key}'.")
    else:
        print(f"  (PutSecretValue failed — {cp.stderr.strip()[:140]}; non-fatal, existing secret still valid)")


def main() -> None:
    cfg = config.load()
    admin = cfg.require("ADMIN_PROFILE")
    aws_profile = cfg.get("AWS_PROFILE_FOR_SETUP")
    if not aws_profile:
        print("  (AWS_PROFILE_FOR_SETUP unset — skipping AWS provisioning. Set it in the config file to enable.)")
        return
    cred = cfg.require("AIA_LLM_SERVICE_CREDENTIAL")
    secret_name = cfg.require("AIA_LLM_SECRET_NAME")
    role_name = cfg.require("AIA_LLM_IAM_ROLE")
    region = cfg.get("AIA_LLM_SECRET_REGION", "us-west-2")
    key = cfg.get("AIA_LLM_SECRET_JSON_KEY", "token")

    # ── Idempotency gate — skip ONLY when AWS is UNREACHABLE ───────────────────────────────────────────
    # The friction we remove: an expired `aws sso login` shouldn't HARD-FAIL a re-run that needs no new AWS
    # resource. But we must NOT skip provisioning merely because the resources exist: step 2 RE-ASSERTS the
    # shared `aia-llm-secrets-access` role's inline policy for THIS config's secret, and stage vs prod use
    # DIFFERENT secret names on the SAME role — so if a different config provisioned last, skipping leaves the
    # policy pointing at the wrong secret and the app gets AccessDenied on GetSecretValue. So: probe AWS; if
    # reachable, always run the full (idempotent) provisioning below; only if UNREACHABLE do we skip (and only
    # when already provisioned), accepting the caveat that a cross-config stale policy/token won't be fixed.
    acct = aws("sts", "get-caller-identity", "--query", "Account", "--output", "text",
               region=region, profile=aws_profile, check=False)
    if not acct:
        existing_cred = dbx.cli_json(admin, "credentials", "get-credential", cred)
        existing_arn = cfg.get("AIA_LLM_SECRET_ARN")
        if existing_cred and existing_arn:
            cfg.set("AIA_LLM_SECRET_ARN", existing_arn)
            report.step(f"AWS not authed, but UC credential '{cred}' + secret already exist — skipping (no SSO needed)")
            life = cfg.get("AIA_LLM_TOKEN_LIFETIME_SECONDS", "86400")
            print(f"  NOTE: token NOT rotated (valid until its {life}s lifetime) and the shared IAM policy NOT")
            print(f"  re-asserted for this config's secret. If a DIFFERENT config provisioned more recently, the")
            print(f"  app can hit AccessDenied on GetSecretValue — `aws sso login --profile {aws_profile}` + re-run to fix.")
            print("\nAWS + UC credential already in place. Next: 00_admin_prereqs.py")
            return
        sys.exit(f"could not reach AWS — is the shell authed? (export AWS_PROFILE={aws_profile}; aws sso login)")

    # ── AWS reachable → full IDEMPOTENT provisioning (reuses existing resources; critically re-asserts the
    #    shared role's inline policy for THIS config's secret in step 2). ──
    role_arn = f"arn:aws:iam::{acct}:role/{role_name}"
    print(f"AWS account={acct} region={region}  role={role_name}  credential={cred}")

    # ── 1. Secrets Manager secret (placeholder; step 5 below rotates the real PAT in) ──
    report.step(f"1) ensure the Secrets Manager secret '{secret_name}' exists")
    arn = aws("secretsmanager", "describe-secret", "--secret-id", secret_name,
              "--query", "ARN", "--output", "text", region=region, profile=aws_profile, check=False)
    if arn:
        print(f"  reusing existing secret: {arn}")
    else:
        arn = aws("secretsmanager", "create-secret", "--name", secret_name,
                  "--description", "AIA LLM gateway token (fetched by app/job via UC service credential)",
                  "--secret-string", json.dumps({key: "PLACEHOLDER-step5-rotates-this"}),
                  "--query", "ARN", "--output", "text", region=region, profile=aws_profile)
        print(f"  created secret: {arn}")
    cfg.set("AIA_LLM_SECRET_ARN", arn)

    # ── 2. IAM role: placeholder trust (reconciled in step 3) + GetSecretValue policy ──
    report.step(f"2) ensure the IAM role '{role_name}' exists (trust reconciled in step 3)")
    # Placeholder principal = the ACCOUNT ROOT, not the role's own ARN. On a FIRST run the role doesn't exist
    # yet, so a self-referential trust ({"AWS": role_arn}) is rejected at create time ("MalformedPolicyDocument:
    # Invalid principal"). The account root always resolves and is a valid placeholder; step 3 OVERWRITES this
    # trust entirely with the real UC-master + external_id, so nothing actually relies on it.
    init_trust = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{acct}:root"}, "Action": "sts:AssumeRole"}]}
    exists = subprocess.run(["aws", "iam", "get-role", "--role-name", role_name],
                            env={**os.environ, "AWS_PROFILE": aws_profile, "AWS_DEFAULT_REGION": region},
                            capture_output=True, text=True).returncode == 0
    if exists:
        print(f"  reusing existing role: {role_arn}")
    else:
        aws("iam", "create-role", "--role-name", role_name,
            "--assume-role-policy-document", json.dumps(init_trust),
            "--description", "AIA: UC service credential reads the LLM token from Secrets Manager",
            region=region, profile=aws_profile)
        print(f"  created role: {role_arn}")
    policy = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
        "Resource": f"arn:aws:secretsmanager:{region}:{acct}:secret:{secret_name}-*"}]}
    aws("iam", "put-role-policy", "--role-name", role_name, "--policy-name", "aia-read-llm-secret",
        "--policy-document", json.dumps(policy), region=region, profile=aws_profile)
    print(f"  inline policy set (GetSecretValue on {secret_name} only)")

    # ── 3. UC service credential + reconcile trust ──
    report.step(f"3) ensure the UC service credential '{cred}' + reconcile the trust")
    cinfo = dbx.cli_json(admin, "credentials", "get-credential", cred)
    if not cinfo:
        cinfo = dbx.cli_json(admin, "credentials", "create-credential", "--json", json.dumps({
            "name": cred, "purpose": "SERVICE",
            "comment": "AIA: read LLM gateway token from AWS Secrets Manager",
            "aws_iam_role": {"role_arn": role_arn}, "skip_validation": True}))
        print("  created credential")
    else:
        print("  reusing existing credential")
    extid = (cinfo or {}).get("aws_iam_role", {}).get("external_id", "")
    uc_master = (cinfo or {}).get("aws_iam_role", {}).get("unity_catalog_iam_arn", "")
    if not extid or not uc_master:
        sys.exit("credential returned no external_id / unity_catalog_iam_arn")
    print(f"  external_id={extid}")
    print(f"  uc_master={uc_master}")
    # The final trust lists BOTH the UC master role AND this role itself (self-assume — a documented UC
    # requirement). On a FIRST run the role was just created, and IAM is eventually
    # consistent: for a few seconds AWS's policy validator doesn't yet resolve the new role ARN as a valid
    # principal, so update-assume-role-policy fails "MalformedPolicyDocument: Invalid principal". Retry with
    # backoff until propagation catches up (verified: succeeds within ~1 min of role creation).
    final_trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": [uc_master, role_arn]}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"sts:ExternalId": extid}}}]}
    for i in range(1, 13):
        r = subprocess.run(["aws", "iam", "update-assume-role-policy", "--role-name", role_name,
                            "--policy-document", json.dumps(final_trust)],
                           env={**os.environ, "AWS_PROFILE": aws_profile, "AWS_DEFAULT_REGION": region},
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(f"  trust reconciled to external_id (attempt {i})")
            break
        if "MalformedPolicyDocument" not in r.stderr:
            sys.exit(f"update-assume-role-policy failed (exit {r.returncode}): {r.stderr.strip()}")
        print(f"  attempt {i}: role ARN not yet resolvable (IAM propagation); retrying…")
        time.sleep(5)
    else:
        sys.exit("trust reconcile never succeeded — the new role ARN did not propagate in time")

    # ── 4. validate (retries — IAM trust propagation lag) ──
    # A FRESH role's STS AssumeRole trust can take 60-120s to propagate — longer than the trust-policy WRITE
    # above. Validation FAILs (PERMISSION_DENIED, "failed to get credentials") until it catches up, then PASSes
    # with no code change. So retry generously (~2.5 min). Success = at least one PASS and NO remaining FAILs;
    # a persistent failure is a HARD error (exit non-zero), not a warning — a broken credential that slips
    # through here surfaces later as a cryptic runtime "no ACCESS on Credential" during an investigation.
    report.step("4) validate the credential (retries — a fresh IAM role's STS trust takes 1-2 min to propagate)")
    last = ""
    for i in range(1, 26):
        res = dbx.cli_json(admin, "credentials", "validate-credential", "--json",
                           json.dumps({"credential_name": cred, "purpose": "SERVICE"}))
        results = (res or {}).get("results", []) if isinstance(res, dict) else []
        verdicts = [x.get("result") for x in results]
        if verdicts and "FAIL" not in verdicts:   # all checks PASS/SKIP, at least one present
            print(f"  VALIDATED (attempt {i})")
            break
        last = "; ".join(f"{x.get('result')}: {x.get('message', '')[:80]}" for x in results) or str(res)
        print(f"  attempt {i}: not yet (IAM propagation); retrying…")
        time.sleep(6)
    else:
        sys.exit(f"credential validation never PASSed after retries — last: {last}")

    # ── 5. rotate a fresh gateway token into the secret (was 00_admin_prereqs step 4) ──
    rotate_llm_token(cfg, admin, arn=arn, region=region, key=key, aws_profile=aws_profile)

    print("\nAWS + UC credential ready, with a fresh token rotated into the secret. Next: 00_admin_prereqs.py")
    print("creates the role/SPs (and, in job mode, grants the job SP ACCESS on this credential up front);")
    print("in_process, the app SP gets that ACCESS post-deploy in 02_admin_postdeploy.py once the app exists.")


if __name__ == "__main__":
    main()
