"""Tier 1 — lib/llm.py credential resolution (get_llm_config / _from_aws_secrets_manager).

Offline: the Databricks service-credential mint and boto3 Secrets Manager are faked, so the AWS token
chain runs with no cloud. Complements test_llm_retry.py (which covers the gateway POST retry). Covers the
env-override short-circuit, the STS→SecretsManager fetch, raw vs JSON-key secret extraction, and the
required-input guards.
"""
import json
import types

import pytest

import databricks.sdk as sdk
from lib import llm as llmmod

_LLM_ENV = ("AIA_LLM_ENDPOINT_URL", "AIA_LLM_TOKEN", "AIA_LLM_SERVICE_CREDENTIAL",
            "AIA_LLM_SECRET_ARN", "AIA_LLM_SECRET_REGION", "AIA_LLM_SECRET_JSON_KEY")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _LLM_ENV:
        monkeypatch.delenv(k, raising=False)


def _fake_ws(aws_creds=("AK", "SK", "TK"), cred_expect="cred"):
    class _Creds:
        def generate_temporary_service_credential(self, credential_name):
            assert credential_name == cred_expect
            if aws_creds is None:
                return types.SimpleNamespace(aws_temp_credentials=None)
            ak, sk, tk = aws_creds
            return types.SimpleNamespace(aws_temp_credentials=types.SimpleNamespace(
                access_key_id=ak, secret_access_key=sk, session_token=tk))

    class _WS:
        def __init__(self, *a, **k):
            self.credentials = _Creds()
    return _WS


def _patch_boto(monkeypatch, secret_string, captured):
    class _SM:
        def get_secret_value(self, SecretId):
            captured["arn"] = SecretId
            return {"SecretString": secret_string}

    import boto3
    def _client(name, **kw):
        assert name == "secretsmanager"
        captured["client_kwargs"] = kw
        return _SM()
    monkeypatch.setattr(boto3, "client", _client)


def test_env_override_short_circuits_aws(monkeypatch):
    monkeypatch.setenv("AIA_LLM_ENDPOINT_URL", "https://gw/invocations")
    monkeypatch.setenv("AIA_LLM_TOKEN", "literal-token")
    # If AWS were touched, this would explode (no WorkspaceClient patched to succeed):
    monkeypatch.setattr(sdk, "WorkspaceClient", lambda *a, **k: pytest.fail("must not touch AWS"))
    assert llmmod.get_llm_config() == ("https://gw/invocations", "literal-token")


def test_aws_raw_secret(monkeypatch):
    monkeypatch.setenv("AIA_LLM_ENDPOINT_URL", "https://gw/invocations")
    monkeypatch.setenv("AIA_LLM_SERVICE_CREDENTIAL", "cred")
    monkeypatch.setenv("AIA_LLM_SECRET_ARN", "arn:aws:secret:xyz")
    monkeypatch.setenv("AIA_LLM_SECRET_REGION", "us-west-2")
    monkeypatch.setattr(sdk, "WorkspaceClient", _fake_ws())
    captured = {}
    _patch_boto(monkeypatch, "raw-token", captured)

    url, token = llmmod.get_llm_config()
    assert url == "https://gw/invocations" and token == "raw-token"
    assert captured["arn"] == "arn:aws:secret:xyz"
    assert captured["client_kwargs"]["aws_access_key_id"] == "AK"
    assert captured["client_kwargs"]["region_name"] == "us-west-2"


def test_aws_json_key_extraction(monkeypatch):
    monkeypatch.setenv("AIA_LLM_ENDPOINT_URL", "https://gw/invocations")
    monkeypatch.setenv("AIA_LLM_SERVICE_CREDENTIAL", "cred")
    monkeypatch.setenv("AIA_LLM_SECRET_ARN", "arn")
    monkeypatch.setenv("AIA_LLM_SECRET_REGION", "us-west-2")
    monkeypatch.setenv("AIA_LLM_SECRET_JSON_KEY", "token")
    monkeypatch.setattr(sdk, "WorkspaceClient", _fake_ws())
    _patch_boto(monkeypatch, json.dumps({"token": "nested-token", "other": "x"}), {})

    _, token = llmmod.get_llm_config()
    assert token == "nested-token"


def test_missing_url_raises(monkeypatch):
    with pytest.raises(RuntimeError, match="AIA_LLM_ENDPOINT_URL is not set"):
        llmmod.get_llm_config()


def test_missing_aws_inputs_raise(monkeypatch):
    monkeypatch.setenv("AIA_LLM_ENDPOINT_URL", "https://gw/invocations")   # url set, but no cred/arn/region
    with pytest.raises(RuntimeError, match="AIA_LLM_SERVICE_CREDENTIAL"):
        llmmod.get_llm_config()


def test_no_aws_creds_returned_raises(monkeypatch):
    monkeypatch.setenv("AIA_LLM_ENDPOINT_URL", "https://gw/invocations")
    monkeypatch.setenv("AIA_LLM_SERVICE_CREDENTIAL", "cred")
    monkeypatch.setenv("AIA_LLM_SECRET_ARN", "arn")
    monkeypatch.setenv("AIA_LLM_SECRET_REGION", "us-west-2")
    monkeypatch.setattr(sdk, "WorkspaceClient", _fake_ws(aws_creds=None))
    with pytest.raises(RuntimeError, match="returned no AWS creds"):
        llmmod.get_llm_config()
