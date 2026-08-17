"""Tier 1 — the generic tool-calling loop (lib/tool_wielding_agent.py).

Pure, offline: the loop only ever calls the injected `llm` and `tool_fn`, so we drive it with fakes and
assert the request -> (tools) -> repeat -> converge behaviour and the returned shape. No Databricks, no
network, no mlflow (its tracing is a soft-optional no-op).
"""
import json

from lib.tool_wielding_agent import ToolWieldingAgent


# --- fakes -----------------------------------------------------------------------------------------
def _final(text):
    """An LLM response that concludes with a final text answer."""
    return {"choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}]}


def _tool_call(name, args, call_id="c1"):
    """An LLM response that asks to call one tool."""
    return {"choices": [{"finish_reason": "tool_calls",
                         "message": {"role": "assistant", "tool_calls": [
                             {"id": call_id, "function": {"name": name,
                                                          "arguments": json.dumps(args)}}]}}]}


class ScriptedLLM:
    """Returns queued responses in order; records the messages it was called with each turn."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, messages, tools):
        self.calls.append(list(messages))
        return self._responses.pop(0)


# --- tests -----------------------------------------------------------------------------------------
def test_converges_immediately_with_no_tools():
    llm = ScriptedLLM([_final("done")])
    agent = ToolWieldingAgent(llm, tool_specs=[], tool_fn=lambda n, a: [])
    out = agent.run([{"role": "user", "content": "hi"}])
    assert out["converged"] is True
    assert out["content"] == "done"
    assert out["tools_called"] == []
    assert out["evidence"] == {}


def test_runs_one_tool_then_converges():
    llm = ScriptedLLM([_tool_call("enrich_indicator", {"indicator": "x"}), _final("verdict")])
    seen = {}

    def tool_fn(name, args):
        seen["name"], seen["args"] = name, args
        return [{"row": 1}]

    agent = ToolWieldingAgent(llm, tool_specs=[], tool_fn=tool_fn)
    out = agent.run([{"role": "user", "content": "go"}])
    assert out["converged"] is True
    assert out["content"] == "verdict"
    assert out["tools_called"] == ["enrich_indicator"]
    assert out["evidence"] == {"enrich_indicator": [{"row": 1}]}
    assert seen == {"name": "enrich_indicator", "args": {"indicator": "x"}}


def test_tool_exception_is_captured_not_raised():
    llm = ScriptedLLM([_tool_call("blast_radius", {"indicator": "x"}), _final("ok")])

    def boom(name, args):
        raise RuntimeError("warehouse down")

    agent = ToolWieldingAgent(llm, tool_specs=[], tool_fn=boom)
    out = agent.run([{"role": "user", "content": "go"}])
    assert out["converged"] is True
    # the error is fed back as the tool result rather than crashing the loop
    assert out["evidence"]["blast_radius"] == [{"error": "warehouse down"}]


def test_does_not_converge_within_budget():
    # always asks for a tool, never concludes -> exhausts max_tool_turns
    llm = ScriptedLLM([_tool_call("pivot_indicator", {"indicator": "x"}) for _ in range(5)])
    agent = ToolWieldingAgent(llm, tool_specs=[], tool_fn=lambda n, a: [{"r": 1}],
                              max_tool_turns=3)
    out = agent.run([{"role": "user", "content": "go"}])
    assert out["converged"] is False
    assert out["content"] is None
    assert len(out["tools_called"]) == 3   # exactly the budget, no more


def test_multiple_tool_calls_in_one_turn_all_recorded():
    resp = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
        {"id": "a", "function": {"name": "enrich_indicator", "arguments": '{"indicator": "x"}'}},
        {"id": "b", "function": {"name": "enrich_indicator", "arguments": '{"indicator": "y"}'}}]}}]}
    llm = ScriptedLLM([resp, _final("done")])
    agent = ToolWieldingAgent(llm, tool_specs=[], tool_fn=lambda n, a: [{"got": a["indicator"]}])
    out = agent.run([{"role": "user", "content": "go"}])
    assert out["tools_called"] == ["enrich_indicator", "enrich_indicator"]
    assert out["evidence"]["enrich_indicator"] == [{"got": "x"}, {"got": "y"}]
