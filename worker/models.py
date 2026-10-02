"""The AI models and effort levels a person may pick on the page. One list, used by the page's server and the worker.

Every model here was checked on 2026-10-02 against OpenRouter's public lists: it takes video as input, supports the
`reasoning` setting, and has at least one zero-retention endpoint (the route raw footage must always use; see
llm.ROUTES["zdr"]). Nothing outside this list can be requested, whatever a browser sends. To add a model: check those
three things again, add it here with its prices, run a real job, and have the result looked at.

Prices are US dollars per million tokens (input, output) on the standard route for prompts under 200k tokens.
"""
from __future__ import annotations

MODELS: dict[str, dict] = {
    "best": {"openrouter": "google/gemini-3.1-pro-preview", "price": (2.00, 12.00), "name": "Best quality",
             "tech": "Gemini 3.1 Pro", "tag": "Recommended",
             "sub": "Best for important ads, when you want the strongest moments picked."},
    "efficient": {"openrouter": "google/gemini-3.8-flash", "price": (0.75, 3.75), "name": "Efficient",
                  "tech": "Gemini 3.8 Flash", "tag": "",
                  "sub": "Faster and cheaper, with very good results. A good everyday choice."},
    "cheapest": {"openrouter": "google/gemini-3.1-flash-lite", "price": (0.25, 1.50), "name": "Cheapest",
                 "tech": "Gemini 3.1 Flash Lite", "tag": "Cheapest",
                 "sub": "Lowest cost. Best for short, clear videos and quick tests."},
}
EFFORTS: dict[str, dict] = {
    "low": {"name": "Quick", "sub": "A fast, light pass. Fine for a simple, clear video."},
    "medium": {"name": "Balanced", "sub": "A good mix of speed and care. Right for most videos."},
    "high": {"name": "Thorough", "sub": "The AI thinks hardest before choosing. Costs the most."},
}
DEFAULT_MODEL, DEFAULT_EFFORT = "best", "medium"

# Rough token use of one planning call and one self-check, to turn the prices into a "about $X" for the page. The thinking
# tokens of each effort level are a guess to be corrected from real spend (jobs.cost_usd) as batches accumulate.
PLAN_INPUT_TOKENS, CHECK_INPUT_TOKENS, CHECK_OUTPUT_TOKENS = 15_000, 5_000, 500
PLAN_OUTPUT_TOKENS = {"low": 1_200, "medium": 2_500, "high": 6_000}


def estimate_cost(model: str, effort: str, ads: int = 3) -> float:
    """About what one batch costs in AI fees: one planning call plus one self-check per ad."""
    pin, pout = MODELS[model]["price"]
    plan = PLAN_INPUT_TOKENS * pin + PLAN_OUTPUT_TOKENS[effort] * pout
    checks = ads * (CHECK_INPUT_TOKENS * pin + CHECK_OUTPUT_TOKENS * pout)
    return round((plan + checks) / 1_000_000, 3)


def resolve(model: object, effort: object) -> tuple[str, str, str]:
    """(page key, OpenRouter model id, effort) for what a job asked for. Anything unknown or missing becomes the default,
    so a bad value can never reach OpenRouter."""
    key = model if isinstance(model, str) and model in MODELS else DEFAULT_MODEL
    level = effort if isinstance(effort, str) and effort in EFFORTS else DEFAULT_EFFORT
    return key, MODELS[key]["openrouter"], level


def catalog() -> dict:
    """What the page needs to draw its two menus: for each AI and effort, about what a batch of 1, 3 or 5 ads costs."""
    return {"models": [{"key": k, "name": m["name"], "tech": m["tech"], "tag": m["tag"], "sub": m["sub"],
                        "costs": {e: {str(n): estimate_cost(k, e, n) for n in (1, 3, 5)} for e in EFFORTS}}
                       for k, m in MODELS.items()],
            "efforts": [{"key": k, "name": e["name"], "sub": e["sub"]} for k, e in EFFORTS.items()],
            "default_model": DEFAULT_MODEL, "default_effort": DEFAULT_EFFORT}
