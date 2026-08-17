"""The AIA investigation agent — the domain layer on top of the generic ToolWieldingAgent.

`Investigator.investigate(case)` returns a verdict dict. Everything AIA-specific lives here — the
system prompt, the containment plays, how the 5 UC-function tools take their argument, and how the
final JSON verdict is parsed and validated. The provider-agnostic tool-calling loop lives in
`tool_wielding_agent.ToolWieldingAgent`; this class just supplies the domain and interprets the result.

It stays PURE (no Spark, dbutils, table writes, or secrets) and is wired via two callables:

    tool_fn(name: str, value: str) -> list[dict]     # run one UC-function tool, return rows
    llm(messages, tools) -> dict                     # OpenAI-style chat completion response

Because it's pure, it is unit-testable with fakes and identical across every deployment target. All
the I/O (SQL, secrets, persistence, Tines) lives in the orchestrator + drivers.
"""
import json

from lib.tool_wielding_agent import ToolWieldingAgent

# The containment plays the agent may recommend (must match the DSL / downstream contract).
PLAYS = ["account_suspended", "rate_limited", "forced_password_reset", "mfa_enforced",
         "manual_review", "external_sharing_disabled", "session_revoked", "cleared_no_action"]

# tool name -> the argument name it takes (indicator-centric vs account-centric).
TOOL_ARG = {
    "enrich_indicator": "indicator", "pivot_indicator": "indicator", "blast_radius": "indicator",
    "get_account_risk": "account_id", "get_account_actions": "account_id",
}
TOOL_DESCRIPTIONS = {
    "enrich_indicator": "URLhaus verdict for a URL/IP/domain/hash (query_status ok=known-bad, threat, url_status, tags, family).",
    "pivot_indicator": "Pivot an indicator to its campaign, threat actor, sibling indicators, and family/threat/tags.",
    "blast_radius": "Which internal accounts have this indicator in their detection telemetry, with each account's risk band.",
    "get_account_risk": "Latest risk score, band, and top contributing signal for an account.",
    "get_account_actions": "Protective actions already taken on an account and why.",
}

SYSTEM_PROMPT = (
    "You are AIA's autonomous SOC investigator. You are handed ONE security case that arrived at "
    "severity MEDIUM. Investigate it with the tools — YOU decide which to call and in what order — to "
    "determine whether it is truly medium or is actually a HIGH-severity threat that must be escalated. "
    "Gather enough evidence (enrich the indicator, pivot for attribution, check blast radius, check the "
    "account's risk and prior actions), then decide. Escalate to HIGH when the evidence shows a "
    "known-bad indicator tied to an active campaign/actor, a meaningful blast radius, or a high-risk "
    "account. Recommend exactly ONE containment play from: " + ", ".join(PLAYS) + ".\n\n"
    'Reply with ONLY a JSON object: {"assessed_severity": "low|medium|high", '
    '"escalate_to_high": true|false, "recommended_play": "<one play>", "confidence": 0.0-1.0, '
    '"summary": "<2-3 sentence investigation summary>", "rationale": "<one sentence why>"}.')

# Bounded loop budget: ~5 tools (enrich + pivot + blast_radius + account risk/actions) + a conclusion.
MAX_TOOL_TURNS = 6
# Per-turn LLM output cap. Each turn emits either a small tool call or the final verdict JSON (a few
# hundred tokens), so this is generous headroom / a runaway-response ceiling, not a tuned value.
MAX_TOKENS = 1024


def _tool_specs():
    """OpenAI function-tool specs for the 5 UC-function tools (each takes one string argument)."""
    specs = []
    for name, arg in TOOL_ARG.items():
        desc = ("an account id like ACC-000888" if arg == "account_id"
                else "an indicator value (URL/IP/domain/hash)")
        specs.append({"type": "function", "function": {
            "name": name, "description": TOOL_DESCRIPTIONS[name],
            "parameters": {"type": "object", "required": [arg],
                           "properties": {arg: {"type": "string", "description": desc}}}}})
    return specs


TOOL_SPECS = _tool_specs()


def _extract_json(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    start, end = t.find("{"), t.rfind("}")
    return json.loads(t[start:end + 1])


class Investigator:
    """AIA's investigation agent. Construct with the two callables, then investigate(case).

    tool_fn(name, value) -> list[dict]:  runs one UC-function tool and returns rows.
    llm(messages, tools) -> dict:        an OpenAI-style chat completion (response["choices"][0]...).
    """

    def __init__(self, tool_fn, llm, max_tool_turns=MAX_TOOL_TURNS):
        self._tool_fn = tool_fn
        self._llm = llm
        self._max_turns = max_tool_turns

    def _decision(self, text):
        """Parse + validate the model's final JSON verdict; None if it isn't parseable."""
        try:
            d = _extract_json(text)
        except Exception:
            return None
        if d.get("recommended_play") not in PLAYS:
            d["recommended_play"] = "manual_review"
        if d.get("assessed_severity") not in ("low", "medium", "high"):
            d["assessed_severity"] = "medium"
        d["escalate_to_high"] = bool(d.get("escalate_to_high"))
        try:
            d["confidence"] = float(d.get("confidence") or 0.0)
        except (TypeError, ValueError):
            d["confidence"] = 0.0
        return d

    def investigate(self, case):
        """Investigate one case dict and return a verdict dict:
           {assessed_severity, escalate_to_high, recommended_play, confidence, summary, rationale,
            evidence, tools_called}. Pure — no side effects."""
        # The generic loop calls tools with a parsed args dict; map it to each UC function's single
        # positional string argument (indicator- vs account-centric) — the AIA-specific glue.
        def tool_fn(name, args):
            if name not in TOOL_ARG:
                return [{"error": f"unknown tool {name}"}]
            return self._tool_fn(name, args.get(TOOL_ARG[name]))

        agent = ToolWieldingAgent(self._llm, TOOL_SPECS, tool_fn, max_tool_turns=self._max_turns)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content":
                f"Case {case['case_id']} on account {case.get('account_id')}. "
                f"Indicator {case.get('indicator_value')} ({case.get('indicator_type')}). "
                f"Arrived at severity {case.get('severity')}. Narrative: {case.get('description')}"}]
        result = agent.run(messages)

        decision = self._decision(result["content"]) if result["converged"] else None
        if decision:
            decision["evidence"] = result["evidence"]
            decision["tools_called"] = result["tools_called"]
            return decision
        # Didn't converge, or the final message wasn't valid JSON — route to manual review.
        return {"assessed_severity": "medium", "escalate_to_high": False,
                "recommended_play": "manual_review", "confidence": 0.3,
                "summary": "Agent did not converge within the tool budget; routing to manual review.",
                "rationale": "max tool turns reached or unparseable verdict",
                "evidence": result["evidence"], "tools_called": result["tools_called"]}
