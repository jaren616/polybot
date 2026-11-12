"""Polymarket data access (read-only, public, no keys needed)."""
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

import config

S = requests.Session()
S.headers["User-Agent"] = "polybot/2 (personal research)"


@dataclass
class Market:
    id: str
    question: str
    description: str
    slug: str
    yes_price: float
    best_bid: float
    best_ask: float
    liquidity: float
    vol24: float
    end_date: str
    closed: bool
    yes_final: float | None
    yes_token: str | None = None
    no_token: str | None = None
    min_size: float = 5.0
    tick: float = 0.01
    outcomes: tuple = ("Yes", "No")      # sports markets: e.g. ("Lakers", "Celtics")
    game_start: float | None = None      # unix time a sports game starts
    sports_type: str | None = None       # e.g. "moneyline"
    line: float | None = None            # spread/total line (we skip those)
    group_title: str = ""                # e.g. "Draw" or a team name inside a match event
    end_ts: float | None = None

    @property
    def is_yes_no(self) -> bool:
        return [o.lower() for o in self.outcomes] == ["yes", "no"]

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    def label(self, side: str) -> str:
        return self.outcomes[0 if side == "YES" else 1]

    @property
    def url(self) -> str:
        return f"https://polymarket.com/market/{self.slug}"

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2


def _j(v, default):
    if isinstance(v, list):
        return v
    try:
        return json.loads(v or "null") or default
    except (ValueError, TypeError):
        return default


def parse_ts(v) -> float | None:
    """Polymarket dates come in a few shapes: '2026-11-15T20:00:00Z', '2026-11-15 20:00:00+00'..."""
    if not v:
        return None
    s = str(v).strip().replace(" ", "T").replace("Z", "+00:00")
    if len(s) >= 3 and s[-3] in "+-" and s[-2:].isdigit():   # '+00' -> '+00:00'
        s += ":00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def parse_market(m: dict) -> Market | None:
    """Any two-outcome market: Yes/No, or two teams (sports)."""
    outcomes = _j(m.get("outcomes"), [])
    try:
        prices = [float(p) for p in _j(m.get("outcomePrices"), [])]
    except (ValueError, TypeError):
        return None
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    yes = prices[0]
    bid = float(m.get("bestBid") or 0) or max(yes - 0.01, 0.0)
    ask = float(m.get("bestAsk") or 0) or min(yes + 0.01, 1.0)
    closed = bool(m.get("closed"))
    tokens = _j(m.get("clobTokenIds"), [])
    return Market(
        id=str(m["id"]),
        question=m.get("question", ""),
        description=(m.get("description") or "")[:2000],
        slug=m.get("slug", ""),
        yes_price=yes, best_bid=bid, best_ask=ask,
        liquidity=float(m.get("liquidityNum") or m.get("liquidity") or 0),
        vol24=float(m.get("volume24hr") or 0),
        end_date=(m.get("endDate") or "")[:10],
        closed=closed,
        yes_final=yes if closed and yes in (0.0, 1.0) else None,
        yes_token=tokens[0] if len(tokens) > 0 else None,
        no_token=tokens[1] if len(tokens) > 1 else None,
        min_size=float(m.get("orderMinSize") or 5),
        tick=float(m.get("orderPriceMinTickSize") or 0.01),
        outcomes=(str(outcomes[0]), str(outcomes[1])),
        game_start=parse_ts(m.get("gameStartTime") or m.get("eventStartTime")),
        sports_type=m.get("sportsMarketType"),
        line=float(m["line"]) if m.get("line") not in (None, "") else None,
        group_title=m.get("groupItemTitle") or "",
        end_ts=parse_ts(m.get("endDate")),
    )


def load_active_markets(n=None) -> list[Market]:
    n = n or config.MARKETS_TO_LOAD
    out, offset = [], 0
    while len(out) < n and offset < 2000:
        r = S.get(f"{config.GAMMA_URL}/markets", params={
            "active": "true", "closed": "false", "limit": 100, "offset": offset,
            "order": "volume24hr", "ascending": "false"}, timeout=20)
        if r.status_code in (400, 422):
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for raw in batch:
            mk = parse_market(raw)
            if mk and mk.liquidity >= config.MIN_MARKET_LIQUIDITY:
                out.append(mk)
        offset += 100
    return out[:n]


def get_market(market_id: str) -> Market | None:
    try:
        r = S.get(f"{config.GAMMA_URL}/markets/{market_id}", timeout=15)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    return parse_market(r.json())


def get_markets(ids) -> dict[str, Market]:
    """Many markets in a few requests (100 per request), open or closed."""
    out, ids = {}, list(dict.fromkeys(str(i) for i in ids))
    for i in range(0, len(ids), 100):
        try:
            r = S.get(f"{config.GAMMA_URL}/markets",
                      params={"id": ids[i:i + 100], "closed": "any", "limit": 100}, timeout=20)
            r.raise_for_status()
            for raw in r.json():
                mk = parse_market(raw)
                if mk:
                    out[mk.id] = mk
        except Exception:
            for one in ids[i:i + 100]:  # fall back to one at a time
                mk = get_market(one)
                if mk:
                    out[one] = mk
    return out


def geoblock() -> dict | None:
    """Polymarket's own check of whether your connection may trade. None = couldn't check."""
    try:
        r = S.get(config.GEOBLOCK_URL, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None
