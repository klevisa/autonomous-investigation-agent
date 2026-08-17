"""LLM client for the AIA investigation agent.

The agent does NOT use the Databricks Foundation Model API (FMAPI) directly. AIA accesses the LLM the
way any internal endpoint is accessed: an **explicit endpoint URL + a bearer token**. The URL is not
sensitive (a plain env var, set by the bundle); the **token** is fetched at runtime from **AWS Secrets
Manager**, reached via a Databricks **Service Credential** that wraps an AWS IAM role — the standard
"AWS Secrets in Databricks" pattern. No long-lived AWS key is stored in Databricks:
the service credential is exchanged for short-lived STS creds per call.

Two token sources, in order:
  1. ENV OVERRIDE — if AIA_LLM_ENDPOINT_URL and AIA_LLM_TOKEN are both set, use them and skip AWS.
     For local dev / tests. (This is the ONLY way to run without AWS.)
  2. AWS SECRETS MANAGER — otherwise: mint STS creds from the service credential, GetSecretValue the token.
"""
import os
import json
import random
import time
import urllib.request
import urllib.error

# Transient-fault retry for the gateway call. An investigation is a MULTI-TURN agent loop (5-15 gateway
# calls), so without this a single transient blip on one turn fails the WHOLE investigation → the job re-runs
# from turn 1 and burns a slot against the reconcile attempts cap. This inner retry recovers a single call in
# place, preserving the investigation's progress; it complements (does not duplicate) the outer domain retry.
# An LLM completion is a SAFE-to-retry POST — its only side effect is token generation/billing, no state we
# must not double-create — so retrying a timed-out/5xx call is sound. We retry ONLY transient signals and let
# deterministic client errors (400/401/403/404/422 — bad request, auth, unsupported param) fail loud.
_LLM_RETRIES = 4
_LLM_BACKOFF = 2.0                            # base seconds, exponential + jitter
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RETRY_AFTER_CAP = 30                         # never honor a Retry-After longer than this


def _from_aws_secrets_manager():
    """Fetch (endpoint_url, token) using a Databricks Service Credential → AWS Secrets Manager.

    The endpoint URL is a plain env var (not sensitive). The token is read from Secrets Manager with
    STS creds minted from the service credential — the caller (app SP / job SP) must hold ACCESS on that
    credential (metastore privilege).
    """
    url = os.environ.get("AIA_LLM_ENDPOINT_URL")
    if not url:
        raise RuntimeError(
            "AIA_LLM_ENDPOINT_URL is not set — required (the gateway URL is a plain, non-secret env var).")

    cred_name = os.environ.get("AIA_LLM_SERVICE_CREDENTIAL")
    secret_arn = os.environ.get("AIA_LLM_SECRET_ARN")
    region = os.environ.get("AIA_LLM_SECRET_REGION")
    if not (cred_name and secret_arn and region):
        raise RuntimeError(
            "AWS token fetch needs AIA_LLM_SERVICE_CREDENTIAL, AIA_LLM_SECRET_ARN, AIA_LLM_SECRET_REGION "
            "(or set AIA_LLM_ENDPOINT_URL + AIA_LLM_TOKEN to override with a literal token).")

    # 1) exchange the SERVICE credential for short-lived AWS STS creds (verified SDK contract — AWS needs no
    #    provider options; creds arrive on .aws_temp_credentials). Minted per call, so expiry is a non-issue.
    from databricks.sdk import WorkspaceClient
    tc = WorkspaceClient().credentials.generate_temporary_service_credential(credential_name=cred_name)
    aws = tc.aws_temp_credentials
    if not aws:
        raise RuntimeError(
            f"service credential '{cred_name}' returned no AWS creds — is it an AWS credential, and does the "
            "caller hold ACCESS on it?")

    # 2) read the token from Secrets Manager with those creds.
    import boto3   # lazy — only the AWS token path needs it
    sm = boto3.client("secretsmanager", region_name=region,
                      aws_access_key_id=aws.access_key_id,
                      aws_secret_access_key=aws.secret_access_key,
                      aws_session_token=aws.session_token)
    secret = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
    # The secret may be the raw token, or JSON with the token under a key (AIA_LLM_SECRET_JSON_KEY).
    json_key = os.environ.get("AIA_LLM_SECRET_JSON_KEY", "").strip()
    if json_key:
        secret = json.loads(secret)[json_key]
    return url, secret


def get_llm_config():
    """Return (endpoint_url, token). Env override (URL+TOKEN) wins; otherwise AWS Secrets Manager."""
    url = os.environ.get("AIA_LLM_ENDPOINT_URL")
    token = os.environ.get("AIA_LLM_TOKEN")
    if url and token:
        return url, token
    return _from_aws_secrets_manager()


def _backoff(attempt):
    """Exponential backoff with jitter (spreads retries so concurrent investigations don't thundering-herd)."""
    return _LLM_BACKOFF * (2 ** attempt) + random.uniform(0, 1)


def _retry_after(e):
    """Seconds to wait from a Retry-After header, if present and an integer (the delta-seconds form the gateway
    sends on 429/503); capped. Returns None if absent or an HTTP-date, so the caller falls back to backoff."""
    raw = (e.headers.get("Retry-After") if getattr(e, "headers", None) else None)
    try:
        return min(int(raw), _RETRY_AFTER_CAP)
    except (TypeError, ValueError):
        return None


class GatewayLLM:
    """A tiny OpenAI-compatible chat client that POSTs to the AIA gateway URL with the bearer token.

    Mirrors the shape the workshop agent expects from `get_deploy_client('databricks').predict(...)`:
    `chat(messages, tools=...)` returns the raw response dict, so `response["choices"][0]["message"]`
    works the same. Uses only stdlib (urllib) for the POST so the request path has no extra deps.
    """

    def __init__(self, endpoint_url=None, token=None):
        if endpoint_url and token:
            self.url, self.token = endpoint_url, token
        else:
            self.url, self.token = get_llm_config()

    def chat(self, messages, tools=None, max_tokens=1024, temperature=None):
        # temperature is OPT-IN: reasoning models (e.g. Claude Opus 5) REJECT it with a 400
        # ("does not support the temperature parameter"). Only send it when a caller explicitly asks.
        payload = {"messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
        data = json.dumps(payload).encode()

        last = None
        for attempt in range(_LLM_RETRIES):
            req = urllib.request.Request(
                self.url, data=data, method="POST",
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                # Deterministic client errors (bad request, auth, unsupported param) will just recur — fail loud.
                if e.code not in _RETRYABLE_STATUS:
                    raise RuntimeError(f"LLM gateway HTTP {e.code}: {body[:500]}") from e
                last = RuntimeError(f"LLM gateway HTTP {e.code}: {body[:500]}")
                delay = _retry_after(e)
                delay = delay if delay is not None else _backoff(attempt)
            except (urllib.error.URLError, TimeoutError) as e:
                # timeout / connection reset / DNS blip — transient, retry.
                last = RuntimeError(f"LLM gateway request failed: {e}")
                delay = _backoff(attempt)
            if attempt < _LLM_RETRIES - 1:
                time.sleep(delay)
        raise last
