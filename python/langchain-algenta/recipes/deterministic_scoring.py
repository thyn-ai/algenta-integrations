"""Deterministic scoring: rank candidate LLM outputs through the engine's `score` tool.

LLM eval is the pattern every serious LangChain deployment converges on: generate several
candidate answers, score them, ship the winner -- and *gate* on a threshold so a bad batch
fails loudly instead of silently reaching users. This recipe does that with the engine's
real `score` tool (the registry's recommendations category): each candidate gets a
deterministic composite score blending normalized expected value with one minus the
probability of loss, candidates are ranked, and the eval decision itself is persisted via
`log_decision` so the selection is auditable later.

Determinism is the point: the same candidates always produce the same ranking, so this
runs as a CI gate. `score` sits outside the four contract profiles' named sets, so -- as
on a real engine -- it's reachable only via the opt-in `full` profile, which is exactly
right for an eval harness (admin/ops code, not a model-facing default).

(Algenta's accelerated `sacrebleu_mojo` scoring kernel is not published to PyPI, so it
can't power a public recipe; the composite `score` tool is the governed scoring surface
that *is* public.)

Run it (from `algenta-integrations/python/langchain-algenta`):

    uv run python -m recipes.deterministic_scoring
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_algenta import create_algenta_tools

from ._stub import serve_recipe_stub
from ._support import first_text_json, tool_by_name

#: Below this composite score the batch is gated (escalated) rather than shipped.
PASS_THRESHOLD = 0.25

#: The recipe's fixed candidate set. In a real eval these come from your chain (e.g.
#: `chain.abatch(...)` over n variants); fixed here so the ranking is reproducible.
CANDIDATES: list[dict[str, str]] = [
    {"candidate": "terse", "text": "Refunds take 30 days."},
    {"candidate": "helpful", "text": "We issue refunds within 30 days of receiving your return."},
    {"candidate": "verbose", "text": "Our refund policy, which applies to all orders, is that refunds are issued within 30 days."},
]


async def run_deterministic_scoring(
    base_url: str, candidates: list[dict[str, str]], *, pass_threshold: float = PASS_THRESHOLD
) -> dict[str, Any]:
    """Score, rank, and gate the candidates; persist the eval decision; return the ranking."""
    if not candidates:
        raise ValueError("deterministic scoring needs at least one candidate.")

    # An eval harness is application/ops code, so it uses the opt-in `full` profile --
    # which, exactly like on a real engine, is the only profile exposing `score`.
    tools = await create_algenta_tools(base_url=base_url, profile="full")
    score = tool_by_name(tools, "score")
    log_decision = tool_by_name(tools, "log_decision")

    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        payload = first_text_json(await score.ainvoke({"request": candidate}))
        scored.append(
            {
                "candidate": candidate["candidate"],
                "score": payload["score"],
                "expected_value": payload["expected_value"],
                "probability_of_loss": payload["probability_of_loss"],
                "score_breakdown": payload["score_breakdown"],
            }
        )
    scored.sort(key=lambda entry: (-entry["score"], entry["candidate"]))

    winner = scored[0]
    gated = winner["score"] < pass_threshold
    logged = first_text_json(await log_decision.ainvoke({"chosen_action": f"eval-select:{winner['candidate']}"}))

    return {
        "ranking": scored,
        "winner": winner["candidate"],
        "winner_score": winner["score"],
        "gated": gated,
        "decision_id": logged["decision_id"],
    }


async def main() -> None:
    async with serve_recipe_stub(score=True) as base_url:
        print(f"Stub Algenta MCP server listening at {base_url}")
        result = await run_deterministic_scoring(base_url, CANDIDATES)
        for entry in result["ranking"]:
            print(f"{entry['candidate']:>10}: score={entry['score']:.4f} (ev={entry['expected_value']}, pol={entry['probability_of_loss']})")
        verdict = "GATED -- escalate, do not ship" if result["gated"] else "PASS"
        print(f"Winner: {result['winner']} ({result['winner_score']:.4f}) -- {verdict}")
        print(f"Eval decision logged: {result['decision_id']}")


if __name__ == "__main__":
    asyncio.run(main())
