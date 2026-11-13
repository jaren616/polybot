"""Second-pass judgement: does this news actually move this market, and to what?

Works with whichever AI you have a key for (first one found wins, or set LLM_PROVIDER):
  ANTHROPIC_API_KEY   Claude (paid, console.anthropic.com)
  GEMINI_API_KEY      Google Gemini (free tier, aistudio.google.com)
  GROQ_API_KEY        Groq (free tier, console.groq.com)
  OPENROUTER_API_KEY  OpenRouter free models (openrouter.ai)
  LLM_PROVIDER=ollama a model running on your own computer (ollama.com), no key
Without any of these, returns None and the bot just logs alerts for you to judge by hand.
"""
import json
import re
import time
from dataclasses import dataclass

import requests

import config

# provider: (key variable, OpenAI-compatible base URL, default model)
PROVIDERS = {
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "openai/gpt-oss-120b:free"),
    "ollama": (None, "http://localhost:11434/v1", "llama3.1:8b"),
}
_cooldown_until = 0.0   # after a rate-limit error, pause AI calls for a few minutes
last_error = ""


def provider_name() -> str | None:
    """Which AI will be used, or None if there's no key for any."""
    want = (config.LLM_PROVIDER or "auto").lower()
    if want == "anthropic":
        return "anthropic" if config.ANTHROPIC_API_KEY else None
    if want in PROVIDERS:
        key_var = PROVIDERS[want][0]
        return want if key_var is None or config.key(key_var) else None
    if config.ANTHROPIC_API_KEY:
        return "anthropic"
    for name, (key_var, _, _) in PROVIDERS.items():
        if key_var and config.key(key_var):
            return name
    return None


def model_name() -> str | None:
    p = provider_name()
    if not p:
        return None
    if config.LLM_MODEL_OVERRIDE:
        return config.LLM_MODEL_OVERRIDE
    return config.LLM_MODEL if p == "anthropic" else PROVIDERS[p][2]


def daily_limit() -> int | None:
    """Free tiers have daily caps: stay under them. None = no cap."""
    if config.AI_MAX_CALLS_PER_DAY is not None:
        return config.AI_MAX_CALLS_PER_DAY
    return None if provider_name() == "anthropic" else 150

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


def _ask_openai_compatible(p, prompt) -> str | None:
    """Gemini, Groq, OpenRouter and Ollama all accept the same OpenAI-style request."""
    global _cooldown_until, last_error
    key_var, base, _ = PROVIDERS[p]
    headers = {"Content-Type": "application/json"}
    if key_var:
        headers["Authorization"] = f"Bearer {config.key(key_var)}"
    if p == "openrouter":
        headers["X-Title"] = "polybot"
    body = {"model": model_name(), "temperature": 0, "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}]}
    r = requests.post(f"{base}/chat/completions", headers=headers, json=body, timeout=60)
    if r.status_code == 429:
        _cooldown_until = time.time() + 300
        last_error = f"{p} rate limit reached - pausing AI for 5 minutes"
        print(f"[ai] {last_error}")
        return None
    if r.status_code >= 400:
        last_error = f"{p} error {r.status_code}: {r.text[:200]}"
        print(f"[ai] {last_error}")
        return None
    choice = (r.json().get("choices") or [{}])[0]
    return (choice.get("message") or {}).get("content") or ""


def judge(news, market, age_min: float) -> Judgement | None:
    global last_error
    p = provider_name()
    if not p or time.time() < _cooldown_until:
        return None
    prompt = PROMPT.format(
        question=market.question, rules=market.description or "(none given)",
        end=market.end_date, price=market.yes_price, age=round(age_min),
        source=news.source, news=news.text)
    try:
        if p == "anthropic":
            msg = _get_client().messages.create(
                model=model_name(), max_tokens=4000,  # room for the model to think before it answers
                messages=[{"role": "user", "content": prompt}])
            # The reply can contain "thinking" blocks before the answer: only read the text blocks.
            raw = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        else:
            raw = _ask_openai_compatible(p, prompt)
    except Exception as e:  # bad key, no credit, network... don't crash the bot
        last_error = f"{p} error: {e}"
        print(f"[ai] {last_error}")
        return None
    if not raw:
        return None
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
