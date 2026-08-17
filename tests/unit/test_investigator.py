"""Tier 1 — the AIA domain agent (lib/investigator.py).

Focus: verdict extraction + validation and the non-convergence fallback. We converge the LLM immediately
(no tool calls) so these stay pure verdict-parsing tests; the tool loop itself is covered separately.
"""
import json

from lib.investigator import Investigator, _extract_json, PLAYS


CASE = {"case_id": "CASE-1", "account_id": "ACC-000888", "indicator_value": "http://bad.example/x",
        "indicator_type": "url", "severity": "medium", "description": "suspicious"}


def _llm_returning(text):
    """An LLM that concludes on the first turn with `text` (no tool calls)."""
    def llm(messages, tools):
        return {"choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}]}
    return llm


def _no_tools(name, value):
    raise AssertionError("tool_fn should not be called when the model concludes immediately")


# --- _extract_json ---------------------------------------------------------------------------------
def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_prose_around_it():
    assert _extract_json('Here is my answer: {"a": 1} thanks') == {"a": 1}


def test_extract_json_with_code_fence():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


# --- verdict validation ----------------------------------------------------------------------------
def _run(verdict_dict):
    agent = Investigator(_no_tools, _llm_returning(json.dumps(verdict_dict)))
    return agent.investigate(CASE)


def test_valid_verdict_passes_through():
    v = _run({"assessed_severity": "high", "escalate_to_high": True,
              "recommended_play": "account_suspended", "confidence": 0.9,
              "summary": "s", "rationale": "r"})
    assert v["assessed_severity"] == "high"
    assert v["escalate_to_high"] is True
    assert v["recommended_play"] == "account_suspended"
    assert v["confidence"] == 0.9
    assert "evidence" in v and "tools_called" in v


def test_unknown_play_coerced_to_manual_review():
    v = _run({"assessed_severity": "medium", "recommended_play": "delete_everything",
              "confidence": 0.5})
    assert v["recommended_play"] == "manual_review"


def test_bad_severity_coerced_to_medium():
    v = _run({"assessed_severity": "SEVERE", "recommended_play": "manual_review", "confidence": 0.5})
    assert v["assessed_severity"] == "medium"


def test_nonnumeric_confidence_becomes_zero():
    v = _run({"assessed_severity": "low", "recommended_play": "cleared_no_action",
              "confidence": "high"})
    assert v["confidence"] == 0.0


def test_escalate_coerced_to_bool():
    v = _run({"assessed_severity": "high", "recommended_play": "mfa_enforced",
              "confidence": 0.5, "escalate_to_high": "yes"})
    assert v["escalate_to_high"] is True


def test_all_declared_plays_are_accepted():
    for play in PLAYS:
        v = _run({"assessed_severity": "medium", "recommended_play": play, "confidence": 0.5})
        assert v["recommended_play"] == play


# --- fallbacks -------------------------------------------------------------------------------------
def test_unparseable_verdict_routes_to_manual_review():
    agent = Investigator(_no_tools, _llm_returning("I could not decide."))
    v = agent.investigate(CASE)
    assert v["recommended_play"] == "manual_review"
    assert v["assessed_severity"] == "medium"
    assert v["escalate_to_high"] is False


def test_non_convergence_routes_to_manual_review():
    # An LLM that always asks for a tool never concludes -> the budget is exhausted.
    def always_tool(messages, tools):
        return {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": "c", "function": {"name": "enrich_indicator", "arguments": '{"indicator": "x"}'}}]}}]}

    agent = Investigator(lambda name, value: [{"ok": 1}], always_tool, max_tool_turns=2)
    v = agent.investigate(CASE)
    assert v["recommended_play"] == "manual_review"
    assert "did not converge" in v["summary"].lower()
