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
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.8-flash"),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "openai/gpt-oss-120b:free"),
    "ollama": (None, "http://localhost:11434/v1", "llama3.1:8b"),
}
_cooldown: dict[str, float] = {}   # provider -> time it can be used again (after errors)
last_error = ""
judged_by = None    # which model answered the last question


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


def available() -> list[str]:
    """The chosen provider first, then any others you have keys for (used as backups)."""
    first = provider_name()
    if not first:
        return []
    out = [first]
    if config.ANTHROPIC_API_KEY and "anthropic" not in out:
        out.append("anthropic")
    for name, (key_var, _, _) in PROVIDERS.items():
        if key_var and config.key(key_var) and name not in out:
            out.append(name)
    return out


def model_name(p=None) -> str | None:
    p = p or provider_name()
    if not p:
        return None
    if config.LLM_MODEL_OVERRIDE and p == provider_name():
        return config.LLM_MODEL_OVERRIDE  # the override applies to your main provider only
    return config.LLM_MODEL if p == "anthropic" else PROVIDERS[p][2]


def daily_limit() -> int | None:
    """Free tiers have daily caps: stay under them. None = no cap."""
    if config.AI_MAX_CALLS_PER_DAY is not None:
        return config.AI_MAX_CALLS_PER_DAY
    return None if provider_name() == "anthropic" else 300

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
    global last_error
    key_var, base, _ = PROVIDERS[p]
    headers = {"Content-Type": "application/json"}
    if key_var:
        headers["Authorization"] = f"Bearer {config.key(key_var)}"
    if p == "openrouter":
        headers["X-Title"] = "polybot"
    body = {"model": model_name(p), "temperature": 0, "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}]}
    r = requests.post(f"{base}/chat/completions", headers=headers, json=body, timeout=30)
    if r.status_code == 429:
        _cooldown[p] = time.time() + 300
        last_error = f"{p} rate limit reached - pausing AI for 5 minutes"
        print(f"[ai] {last_error}")
        return None
    if r.status_code >= 400:
        try:  # pull out the provider's own message, e.g. "model X is no longer available"
            msg = r.json()
            msg = (msg[0] if isinstance(msg, list) else msg).get("error", {}).get("message", "")
        except Exception:
            msg = r.text
        last_error = f"{p} error {r.status_code}: {str(msg)[:160]}"
        print(f"[ai] {last_error}")
        if r.status_code in (401, 403, 404):  # bad key or model: retrying won't help, so stop
            _cooldown[p] = time.time() + 3600        # for an hour instead of burning the daily allowance
        elif r.status_code >= 500:                   # provider overloaded: give it a minute
            _cooldown[p] = time.time() + 60
        return None
    choice = (r.json().get("choices") or [{}])[0]
    return (choice.get("message") or {}).get("content") or ""


def _ask(prompt) -> str | None:
    """Ask the main AI; if it's busy, down or rate-limited, try the backups in turn."""
    global last_error, judged_by
    for p in available():
        if time.time() < _cooldown.get(p, 0):
            continue
        try:
            if p == "anthropic":
                msg = _get_client().messages.create(
                    model=model_name(p), max_tokens=4000,  # room for the model to think before it answers
                    messages=[{"role": "user", "content": prompt}])
                # The reply can contain "thinking" blocks before the answer: only read the text blocks.
                raw = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
            else:
                raw = _ask_openai_compatible(p, prompt)
        except Exception as e:  # bad key, no credit, network, timeout... don't crash the bot
            last_error = f"{p} error: {str(e)[:160]}"
            print(f"[ai] {last_error}")
            _cooldown[p] = time.time() + 60
            raw = None
        if raw:
            judged_by = model_name(p)
            return raw
    return None


def _parse(raw, default_p) -> tuple[dict, Judgement] | None:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return d, Judgement(
            relevant=bool(d.get("relevant", d.get("pick") is not None)),
            prob_yes=min(max(float(d.get("prob_yes") if d.get("prob_yes") is not None else default_p), 0.0), 1.0),
            confidence=min(max(float(d.get("confidence") or 0), 0.0), 1.0),
            already_priced_in=bool(d.get("already_priced_in")),
            reasoning=str(d.get("reasoning", ""))[:500],
        )
    except (ValueError, TypeError):
        return None


def judge(news, market, age_min: float) -> Judgement | None:
    """One story, one market (kept for tools and tests; the bot uses judge_story)."""
    raw = _ask(PROMPT.format(
        question=market.question, rules=market.description or "(none given)",
        end=market.end_date, price=market.yes_price, age=round(age_min),
        source=news.source, news=news.text))
    r = _parse(raw, market.yes_price)
    return r[1] if r else None


STORY_PROMPT = """You are a careful prediction-market analyst.

TODAY (UTC): {today}

NEW INFORMATION (published {age} minutes ago, source: {source}):
{news}

CANDIDATE MARKETS (picked by keyword, so most are probably NOT affected):
{markets}

Tasks:
1. Which ONE market does this news most directly change, under its exact rules and time frame?
   A story about one game barely moves a season-long championship market; a story about a
   person rarely changes a market about a different event. If none is clearly affected, pick null.
2. For the market you pick, give your probability that it resolves YES given this news.
3. Be sceptical: rumours, opinion pieces, unverified single-source claims and news the market has
   probably already seen should not move you much. If the price likely already reflects it, say so.
   If a market's price has already moved since the news came out, the market has probably reacted:
   only disagree with it if you have a clear reason.
4. Mind the deadline: something "likely eventually" can still be unlikely by the close date.
5. Only go above 0.97 or below 0.03 if the outcome is effectively settled.

Reply with ONLY a JSON object:
{{"pick": <market number or null>, "prob_yes": 0.0-1.0, "confidence": 0.0-1.0,
  "already_priced_in": true/false, "reasoning": "one or two sentences"}}"""


def judge_story(news, markets, age_min: float, prices_then: dict | None = None):
    """Show the AI the story plus several candidate markets in ONE call; it picks the affected one.
    prices_then = {market id: YES price around when the news came out} (shows if a market already reacted).
    Returns (index of the picked market or None, Judgement), or (None, None) if the AI didn't answer."""
    now, prices_then = time.time(), prices_then or {}
    lines = []
    for i, mk in enumerate(markets, 1):
        rules = " ".join((mk.description or "").split())[:260]
        left = f" (~{max(mk.end_ts - now, 0) / 86400:.1f} days left)" if mk.end_ts else ""
        then = prices_then.get(mk.id)
        moved = f" (was {then:.3f} when the news came out, {(mk.mid - then) * 100:+.1f} pts)" if then is not None else ""
        lines.append(f"[{i}] {mk.question}\n    closes {mk.end_date or '?'}{left} | YES price {mk.mid:.3f}{moved}"
                     + (f"\n    rules: {rules}" if rules else ""))
    raw = _ask(STORY_PROMPT.format(today=time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)),
                                   age=round(age_min), source=news.source, news=news.text,
                                   markets="\n".join(lines)))
    r = _parse(raw, 0.5)
    if not r:
        return None, None
    d, j = r
    try:
        pick = int(d.get("pick")) - 1 if d.get("pick") is not None else None
    except (TypeError, ValueError):
        pick = None
    if pick is None or not (0 <= pick < len(markets)):
        j.relevant = False
        return None, j
    if d.get("prob_yes") is None:
        j.prob_yes = markets[pick].mid
    j.relevant = True
    return pick, j
