"""Polymarket data access (read-only, public, no keys needed)."""
import json
from dataclasses import dataclass

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


def parse_market(m: dict) -> Market | None:
    outcomes = _j(m.get("outcomes"), [])
    try:
        prices = [float(p) for p in _j(m.get("outcomePrices"), [])]
    except (ValueError, TypeError):
        return None
    if [str(o).lower() for o in outcomes] != ["yes", "no"] or len(prices) != 2:
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


def geoblock() -> dict | None:
    """Polymarket's own check of whether your connection may trade. None = couldn't check."""
    try:
        r = S.get(config.GEOBLOCK_URL, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None
