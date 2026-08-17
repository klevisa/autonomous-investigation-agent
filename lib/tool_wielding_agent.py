"""ToolWieldingAgent — a generic, provider-agnostic bounded tool-calling loop.

This is the reusable engine, extracted from the AIA investigator: it knows nothing about cases,
severities, or containment plays. Give it an LLM callable, a set of tool specs, and a way to run a
tool, and it drives the request → (maybe call tools) → repeat cycle until the model returns a final
text answer or the turn budget is exhausted. Any domain agent (AIA's `Investigator`, or a future
one) builds its prompt, calls `run()`, and interprets the returned text itself.

    llm(messages, tools) -> dict            # OpenAI-style chat completion (response["choices"][0]...)
    tool_specs: list[dict]                  # OpenAI function-tool specs the LLM chooses from
    tool_fn(name: str, args: dict) -> rows  # run ONE tool given its parsed arguments; returns anything
                                            #   JSON-serializable (the loop feeds it back as the result)

WHY A HAND-WRITTEN LOOP (and not a framework): this loop speaks plain OpenAI-style chat, so it works
against ANY OpenAI-compatible endpoint — including AIA's own endpoint reached by explicit URL + token
(see lib/llm.py), which is the core requirement. Framework loops don't fit that: the Anthropic SDK
**Tool Runner** (`client.beta.messages.tool_runner`) drives the loop for you but targets the Anthropic
API, not an arbitrary endpoint URL; **Managed Agents** runs the loop server-side; and Mosaic AI
**`ResponsesAgent`** is a serving *contract*, not a loop engine (the workshop's agent still hand-wrote
this same loop inside `predict()`). If you later route the LLM through FMAPI or the Anthropic SDK, this
class can be swapped for Tool Runner without changing any domain agent that uses it.

MLflow tracing is *soft-optional*: if mlflow is importable, each LLM turn and tool call opens a span
(so a job/endpoint gets a per-run trace for free); otherwise the loop runs untraced (so tests / plain
Python work with no mlflow dependency).
"""
import json

# MLflow tracing is OPTIONAL — keeps this module pure and importable anywhere.
#   * In a Databricks job/endpoint, mlflow IS installed → `_span` opens a real span and the run shows
#     up as a trace (LLM turns + tool calls) in the experiment.
#   * In plain Python (unit tests, or an app importing this) mlflow may be absent → the `except` defines
#     a no-op context manager with the same shape, so the identical loop runs untraced instead of
#     failing on import.
try:
    import mlflow

    def _span(name, kind):
        return mlflow.start_span(name=name, span_type=kind)
except Exception:  # pragma: no cover — mlflow absent; tracing becomes a no-op
    import contextlib

    class _Noop:
        def set_inputs(self, *_a, **_k): pass
        def set_outputs(self, *_a, **_k): pass

    @contextlib.contextmanager
    def _span(name, kind):
        yield _Noop()


class ToolWieldingAgent:
    """The bounded tool-calling loop. Construct with the LLM + tools, then `run(messages)`.

    max_tool_turns bounds the loop (default 6) so a model that keeps calling tools without concluding
    can't run forever. `run()` returns a dict:
        {"content": <final assistant text or None if it never concluded>,
         "evidence": {tool_name: [rows, ...]},   # every tool result, grouped by tool
         "tools_called": [tool_name, ...],        # in call order (may repeat)
         "converged": bool}                       # True if the model produced a final text answer
    Pure — no side effects beyond calling the injected `llm` / `tool_fn`.
    """

    def __init__(self, llm, tool_specs, tool_fn, max_tool_turns=6):
        self._llm = llm
        self._tool_specs = tool_specs
        self._tool_fn = tool_fn
        self._max_turns = max_tool_turns

    def _run_tool(self, name, args):
        with _span(name, "TOOL") as span:
            span.set_inputs({"args": args})
            try:
                rows = self._tool_fn(name, args)
            except Exception as e:
                rows = [{"error": str(e)}]
            span.set_outputs({"rows": rows})
            return rows

    def run(self, messages):
        """Drive the loop over a starting `messages` list (system + user, etc.). See class docstring
        for the return shape. `messages` is not mutated by the caller's reference beyond append."""
        evidence, tools_called = {}, []
        for turn in range(self._max_turns):
            with _span("llm", "LLM") as span:
                span.set_inputs({"turn": turn, "messages": messages})
                resp = self._llm(messages, self._tool_specs)
                choice = resp["choices"][0]
                message = choice.get("message", {})
                span.set_outputs({"finish_reason": choice.get("finish_reason"), "message": message})
            if choice.get("finish_reason") == "tool_calls":
                messages.append(message)
                for call in message.get("tool_calls", []):
                    name = call["function"]["name"]
                    args = json.loads(call["function"].get("arguments") or "{}")
                    rows = self._run_tool(name, args)
                    tools_called.append(name)
                    evidence.setdefault(name, []).extend(rows)
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": json.dumps(rows, default=str)})
            else:
                return {"content": message.get("content", ""), "evidence": evidence,
                        "tools_called": tools_called, "converged": True}
        return {"content": None, "evidence": evidence,
                "tools_called": tools_called, "converged": False}
