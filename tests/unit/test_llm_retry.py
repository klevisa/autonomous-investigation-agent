"""Tier 1 — lib/llm.py gateway retry (transient-aware).

Offline: monkeypatch urllib's urlopen with a scripted sequence, so no network. Asserts we retry the transient
signals (5xx, timeout/URLError, 429 honoring Retry-After) but fail loud + immediately on a deterministic 4xx.
"""
import io
import json
import urllib.error

import pytest

from lib import llm as llmmod
from lib.llm import GatewayLLM


class _Resp:
    """A urlopen success context-manager returning `payload` as JSON."""
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, retry_after=None, body=b"err"):
    hdrs = {}
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("http://x", code, "msg", hdrs, io.BytesIO(body))


def _seq_urlopen(seq):
    """A fake urlopen that yields the scripted items in order (Exception → raise, else return)."""
    it = iter(seq)

    def _fn(req, timeout=None):
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item

    return _fn


def _llm():
    return GatewayLLM("http://x", "tok")   # explicit url+token → no AWS / env


def _msgs():
    return [{"role": "user", "content": "hi"}]


def test_retries_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(llmmod.urllib.request, "urlopen", _seq_urlopen([_http_error(503), _Resp({"ok": 1})]))
    monkeypatch.setattr(llmmod.time, "sleep", lambda s: None)
    assert _llm().chat(_msgs()) == {"ok": 1}


def test_retries_timeout_then_succeeds(monkeypatch):
    monkeypatch.setattr(llmmod.urllib.request, "urlopen",
                        _seq_urlopen([urllib.error.URLError("timed out"), _Resp({"ok": 2})]))
    monkeypatch.setattr(llmmod.time, "sleep", lambda s: None)
    assert _llm().chat(_msgs()) == {"ok": 2}


def test_4xx_fails_loud_and_does_not_retry(monkeypatch):
    calls = {"n": 0}

    def _fn(req, timeout=None):
        calls["n"] += 1
        raise _http_error(400, body=b"bad request")

    monkeypatch.setattr(llmmod.urllib.request, "urlopen", _fn)
    monkeypatch.setattr(llmmod.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        _llm().chat(_msgs())
    assert calls["n"] == 1   # no retry on a deterministic client error


def test_exhausts_retries_then_raises_last(monkeypatch):
    def _fn(req, timeout=None):
        raise _http_error(503)

    monkeypatch.setattr(llmmod.urllib.request, "urlopen", _fn)
    monkeypatch.setattr(llmmod.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        _llm().chat(_msgs())


def test_honors_retry_after_header(monkeypatch):
    sleeps = []
    monkeypatch.setattr(llmmod.urllib.request, "urlopen",
                        _seq_urlopen([_http_error(429, retry_after=7), _Resp({"ok": 3})]))
    monkeypatch.setattr(llmmod.time, "sleep", lambda s: sleeps.append(s))
    assert _llm().chat(_msgs()) == {"ok": 3}
    assert sleeps and sleeps[0] == 7   # first backoff came from Retry-After, not the exponential default
