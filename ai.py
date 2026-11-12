"""Second-pass judgement: does this news actually move this market, and to what?

Uses Claude via the Anthropic API if ANTHROPIC_API_KEY is set.
Without a key, returns None and the bot just logs alerts for you to judge by hand.
"""
import json
import re
from dataclasses import dataclass

import config

PROMPT = """You are a careful prediction-market analyst.

MARKET QUESTION: {question}
RESOLUTION RULES (these decide YES/NO, read them closely):
{rules}
MARKET CLOSES: {end}
CURRENT MARKET PRICE OF YES: {price:.3f}

NEW INFORMATION (published {age} minutes ago, source: {source}):
{news}

Tasks:
1. Is this news genuinely relevant to how THIS market resolves under its exact rules?
2. If relevant, give your probability that the market resolves YES given this news.
3. Be sceptical: rumours, opinion pieces, unverified single-source claims and
   news the market has probably already seen should not move you much.
   If the price likely already reflects it, say so and stay near the market price.
4. Near the extremes, be careful: only go above 0.97 or below 0.03 if the outcome is
   effectively settled (officially announced, already happened, or mathematically
   decided). "Very likely" is not the same as "certain".

Reply with ONLY a JSON object:
{{"relevant": true/false, "prob_yes": 0.0-1.0, "confidence": 0.0-1.0,
  "already_priced_in": true/false, "reasoning": "one or two sentences"}}"""


@dataclass
class Judgement:
    relevant: bool
    prob_yes: float
    confidence: float
    already_priced_in: bool
    reasoning: str


_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def judge(news, market, age_min: float) -> Judgement | None:
    if not config.ANTHROPIC_API_KEY:
        return None
    try:
        msg = _get_client().messages.create(
            model=config.LLM_MODEL,
            max_tokens=4000,  # room for the model to think before it answers
            messages=[{"role": "user", "content": PROMPT.format(
                question=market.question, rules=market.description or "(none given)",
                end=market.end_date, price=market.yes_price, age=round(age_min),
                source=news.source, news=news.text,
            )}],
        )
    except Exception as e:  # bad key, no credit, network... don't crash the bot
        print(f"[ai] Claude API error: {e}")
        return None
    # The reply can contain "thinking" blocks before the answer: only read the text blocks.
    raw = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return Judgement(
            relevant=bool(d.get("relevant")),
            prob_yes=min(max(float(d.get("prob_yes", market.yes_price)), 0.0), 1.0),
            confidence=min(max(float(d.get("confidence", 0)), 0.0), 1.0),
            already_priced_in=bool(d.get("already_priced_in")),
            reasoning=str(d.get("reasoning", ""))[:500],
        )
    except (ValueError, TypeError):
        return None
