"""Crypto value: price Polymarket's crypto markets from the live coin price and its volatility.

Polymarket lists three kinds of crypto price markets (all settle on Binance prices):
  above   "Will the price of Bitcoin be above $66,000 on November 15?"   (price at noon ET)
  range   "Bitcoin price on November 15?"  -> "<66,000", "66,000-68,000", ">84,000"
  touch   "Will Bitcoin reach $100,000 in November?" / "dip to $60,000"   (any time in the period)

Fair chance = standard option maths (log-normal price moves):
  * coin price now from Coinbase (works from GitHub's US servers; Binance often doesn't)
  * volatility from the last 7 days of hourly Coinbase prices
  * "touch" markets also need the highest/lowest price since the period began: if the level was
    already hit, the market is decided and we leave it alone.
Buy when Polymarket is cheaper than the fair chance by CRYPTO_MIN_EDGE.

Backtest (Nov 2026, 21 days, 924 settled BTC/ETH "above"/"range" markets): the model's Brier score
beat the market's (0.127 vs 0.130) and the trading rule made +17% per $ over 239 trades, BUT only
on weekdays (+24%); weekend trades lost (-13%), and the first half of the period only broke even.
So: weekdays only, small stakes, "touch" markets off until backtested.
"""
import math
import re
import statistics
import time
from datetime import datetime, timedelta, timezone

import requests

import config
import data

S = requests.Session()
S.headers["User-Agent"] = "polybot/2 (personal research)"
COINS = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "xrp": "XRP"}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
YEAR = 365 * 86400


# ---------------------------------------------------------------------
#  Live data
# ---------------------------------------------------------------------
def spot(sym: str) -> float | None:
    try:
        r = S.get(f"https://api.exchange.coinbase.com/products/{sym}-USD/ticker", timeout=15)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


def candles(sym: str, start: float, end: float, gran: int) -> list[list]:
    """Coinbase candles [time, low, high, open, close, volume], oldest first (300 per request)."""
    out, t = [], start
    while t < end:
        stop = min(end, t + gran * 300)
        try:
            r = S.get(f"https://api.exchange.coinbase.com/products/{sym}-USD/candles", timeout=20, params={
                "granularity": gran, "start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                "end": datetime.fromtimestamp(stop, timezone.utc).isoformat()})
            r.raise_for_status()
            out += r.json()
        except Exception:
            return []
        t = stop
    return sorted(out)


def vol(sym: str, now=None) -> float | None:
    """Annualised volatility from the last 7 days of hourly prices.
    Backtest (21 days, 924 BTC/ETH markets) scored this better than Deribit's DVOL or 1-3 day windows."""
    now = now or time.time()
    c = candles(sym, now - config.CRYPTO_VOL_HOURS * 3600, now, 3600)
    closes = [x[4] for x in c if x[4] > 0]
    if len(closes) < config.CRYPTO_VOL_HOURS // 2:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    return statistics.pstdev(rets) * math.sqrt(24 * 365)


def extremes_since(sym: str, start: float, now=None) -> tuple[float, float] | None:
    """Highest and lowest price from `start` until now (6-hour candles, so a little before start too)."""
    now = now or time.time()
    c = candles(sym, start - start % 21600, now, 21600)
    if not c:
        return None
    return max(x[2] for x in c), min(x[1] for x in c)


# ---------------------------------------------------------------------
#  Reading the markets
# ---------------------------------------------------------------------
def _num(s: str) -> float | None:
    s = s.replace(",", "").replace("$", "").strip().lower()
    mult = 1000 if s.endswith("k") else 1
    try:
        return float(s.rstrip("k")) * mult
    except ValueError:
        return None


def _et_midnight(y, m, d) -> float:
    """Midnight New York time (Polymarket's periods start then). Nov-Mar is UTC-5, otherwise UTC-4."""
    off = 5 if m in (11, 12, 1, 2, 3) else 4
    return datetime(y, m, d, off, tzinfo=timezone.utc).timestamp()


def period_start(title: str, end_ts: float) -> float | None:
    """When a 'what price will X hit ...' period began, from the event title."""
    t = title.lower()
    end = datetime.fromtimestamp(end_ts - 3600, timezone.utc)
    m = re.search(r"\bin (\d{4})\b", t)
    if m:
        return _et_midnight(int(m.group(1)), 1, 1)
    m = re.search(r"\b(" + "|".join(MONTHS) + r") (\d{1,2})(?:-\d{1,2})?\b", t)
    if m:
        mo = MONTHS[m.group(1)]
        return _et_midnight(end.year - (1 if mo > end.month else 0), mo, int(m.group(2)))
    m = re.search(r"\bin (" + "|".join(MONTHS) + r")\b", t)
    if m:
        mo = MONTHS[m.group(1)]
        return _et_midnight(end.year - (1 if mo > end.month else 0), mo, 1)
    return None


def classify(title: str, m) -> dict | None:
    """What a crypto market asks: {coin, kind, k / lo,hi}. None if it's not a price market we can price."""
    text = f"{title} {m.question}".lower()
    coin = next((COINS[w] for w in COINS if re.search(rf"\b{w}\b", text)), None)
    if not coin or "up or down" in text or not m.is_yes_no:
        return None
    g = (m.group_title or "").strip()
    q = m.question.lower()
    if re.search(r"\b(reach|hit)\b", q) or g.startswith("↑"):
        k = _num(re.sub(r"[^\d.,kK]", "", g)) if g else None
        k = k or _num((re.search(r"\$([\d,.]+k?)", q) or [None, ""])[1])
        return {"coin": coin, "kind": "touch_up", "k": k} if k else None
    if re.search(r"\b(dip|fall|drop)\b", q) or g.startswith("↓"):
        k = _num(re.sub(r"[^\d.,kK]", "", g)) if g else None
        k = k or _num((re.search(r"\$([\d,.]+k?)", q) or [None, ""])[1])
        return {"coin": coin, "kind": "touch_down", "k": k} if k else None
    if re.search(r"\bbe above \$", q):
        k = _num(re.search(r"above \$([\d,.]+k?)", q).group(1))
        return {"coin": coin, "kind": "above", "k": k} if k else None
    if re.search(r"\bprice of \w+ be (less than|between|greater than)", q):
        if "less than" in q:
            k = _num(re.search(r"less than \$([\d,.]+k?)", q).group(1))
            return {"coin": coin, "kind": "range", "lo": 0.0, "hi": k} if k else None
        if "greater than" in q:
            k = _num(re.search(r"greater than \$([\d,.]+k?)", q).group(1))
            return {"coin": coin, "kind": "range", "lo": k, "hi": math.inf} if k else None
        mm = re.search(r"between \$([\d,.]+k?) and \$([\d,.]+k?)", q)
        if mm:
            lo, hi = _num(mm.group(1)), _num(mm.group(2))
            return {"coin": coin, "kind": "range", "lo": lo, "hi": hi} if lo and hi else None
    return None


# ---------------------------------------------------------------------
#  The maths
# ---------------------------------------------------------------------
def _N(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def p_above(s, k, sigma, t):
    """Chance the price is above k after t years (log-normal, no drift)."""
    if k <= 0:
        return 1.0
    if t <= 0 or sigma <= 0:
        return 1.0 if s > k else 0.0
    sd = sigma * math.sqrt(t)
    return _N((math.log(s / k) - sd * sd / 2) / sd)


def p_touch(s, k, sigma, t):
    """Chance the price touches k (above or below today's price) at any time within t years."""
    if k == s:
        return 1.0
    if t <= 0 or sigma <= 0:
        return 0.0
    sd = sigma * math.sqrt(t)
    b = abs(math.log(k / s))
    nu = -sigma * sigma / 2 if k > s else sigma * sigma / 2   # drift of the log price towards k
    nt = nu * t
    return min(1.0, _N((-b + nt) / sd) + math.exp(2 * nu * b / (sigma * sigma)) * _N((-b - nt) / sd))


def fair(spec, s, sigma, t, hi=None, lo=None) -> float | None:
    kind = spec["kind"]
    if kind == "above":
        return p_above(s, spec["k"], sigma, t)
    if kind == "range":
        return p_above(s, spec["lo"], sigma, t) - (p_above(s, spec["hi"], sigma, t) if spec["hi"] != math.inf else 0)
    if kind == "touch_up":
        if hi is None or hi >= spec["k"] * (1 - config.CRYPTO_TOUCH_BUFFER):
            return None   # already hit (or too close to tell with Coinbase vs Binance): decided, skip
        return p_touch(s, spec["k"], sigma, t) if spec["k"] > s else None
    if kind == "touch_down":
        if lo is None or lo <= spec["k"] * (1 + config.CRYPTO_TOUCH_BUFFER):
            return None
        return p_touch(s, spec["k"], sigma, t) if spec["k"] < s else None
    return None


# ---------------------------------------------------------------------
#  Finding cheap markets
# ---------------------------------------------------------------------
def load_events() -> list[dict]:
    out = []
    for off in range(0, 300, 100):
        try:
            r = data.S.get(f"{config.GAMMA_URL}/events", timeout=20, params={
                "tag_slug": "crypto", "active": "true", "closed": "false", "limit": 100, "offset": off,
                "order": "volume24hr", "ascending": "false"})
            r.raise_for_status()
            batch = r.json()
        except Exception:
            break
        out += batch
        if len(batch) < 100:
            break
    return out


class Feed:
    """Live coin data, fetched once per coin per scan (tests pass their own)."""
    def __init__(self, now=None):
        self.now = now or time.time()
        self._spot, self._vol, self._ext = {}, {}, {}

    def spot(self, sym):
        if sym not in self._spot:
            self._spot[sym] = spot(sym)
        return self._spot[sym]

    def vol(self, sym):
        if sym not in self._vol:
            self._vol[sym] = vol(sym, self.now)
        return self._vol[sym]

    def extremes(self, sym, start):
        key = (sym, start)
        if key not in self._ext:
            self._ext[key] = extremes_since(sym, start, self.now)
        return self._ext[key]


def find_opportunities(events, feed, now=None) -> list[dict]:
    now = now or time.time()
    found = []
    for ev in events:
        title = ev.get("title", "")
        for raw in ev.get("markets", []):
            m = data.parse_market(raw)
            if not m or m.closed or not m.end_ts or m.liquidity < config.CRYPTO_MIN_LIQUIDITY:
                continue
            if m.end_ts - now < config.CRYPTO_MIN_HOURS_LEFT * 3600:
                continue
            spec = classify(title, m)
            if not spec or spec["kind"] not in config.CRYPTO_KINDS:
                continue
            s, sigma = feed.spot(spec["coin"]), feed.vol(spec["coin"])
            if not s or not sigma:
                continue
            hi = lo = None
            if spec["kind"].startswith("touch"):
                start = period_start(title, m.end_ts)
                ext = feed.extremes(spec["coin"], start) if start else None
                if not ext:
                    continue
                hi, lo = max(ext[0], s), min(ext[1], s)
            p = fair(spec, s, sigma, (m.end_ts - now) / YEAR, hi, lo)
            if p is None:
                continue
            for side, fp, price in (("YES", p, m.best_ask), ("NO", 1 - p, round(1 - m.best_bid, 4))):
                edge = fp - price
                if edge >= config.CRYPTO_MIN_EDGE and config.CRYPTO_MIN_PRICE <= price <= config.CRYPTO_MAX_PRICE:
                    found.append({"market": m, "side": side, "fair": fp, "price": price, "edge": edge,
                                  "coin": spec["coin"], "kind": spec["kind"], "spot": s, "vol": sigma,
                                  "event": title})
    found.sort(key=lambda x: -x["edge"])
    return found
