"""Sports value: compare Polymarket prices with bookmakers' odds.

1. Every few hours, fetch head-to-head odds from many bookmakers (The Odds API).
2. Remove each bookmaker's margin ("de-vig") and take a fair probability per outcome
   (Pinnacle's if available - the sharpest book - otherwise the median).
3. Find the same game on Polymarket (both teams named, start time within 3 hours).
4. If Polymarket sells an outcome 4+ points below the fair probability, buy it.
"""
import re
import statistics
import time
import unicodedata

import requests

import config
import data

ODDS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"

# ---------------------------------------------------------------------
#  Team-name matching
# ---------------------------------------------------------------------
ALIASES = {"man": "manchester", "utd": "united", "spurs": "tottenham", "wolves": "wolverhampton",
           "nottm": "nottingham", "forest": "forest", "st": "saint", "la": "los angeles",
           "ny": "new york", "psg": "paris saint germain", "brighton": "brighton", "villa": "villa"}
DROP = {"fc", "afc", "cf", "sc", "the", "club", "and", "hove", "&"}
GENERIC = {"city", "united", "town", "county", "athletic", "rovers", "wanderers", "albion", "state",
           "manchester", "west", "real", "atletico", "inter", "sporting", "los", "angeles", "new",
           "york", "san", "saint", "golden", "oklahoma", "kansas", "las", "green", "bay", "tampa",
           "the", "de", "ac", "sheffield", "bristol", "nottingham"}


def _tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    out = []
    for w in re.findall(r"[a-z0-9']+", text):
        w = w.replace("'", "")
        for part in ALIASES.get(w, w).split():
            if part not in DROP:
                out.append(part)
    return out


def team_in(team: str, text_tokens: set[str]) -> bool:
    """Is this bookmaker team name present in the Polymarket text?
    Every word matches, or a distinctive word does ('Lakers' for 'Los Angeles Lakers')."""
    t = _tokens(team)
    if not t:
        return False
    if all(w in text_tokens for w in t):
        return True
    # 'Manchester United' has no distinctive word, so it must match in full (never via 'Manchester').
    distinctive = [w for w in t if w not in GENERIC and len(w) >= 4]
    return any(w in text_tokens for w in distinctive)


# ---------------------------------------------------------------------
#  Bookmaker odds
# ---------------------------------------------------------------------
def devig(outcomes: list[dict]) -> dict[str, float] | None:
    """Decimal odds -> probabilities with the bookmaker's margin removed."""
    try:
        raw = {o["name"]: 1 / float(o["price"]) for o in outcomes if float(o["price"]) > 1}
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        return None
    total = sum(raw.values())
    if len(raw) < 2 or total <= 0:
        return None
    return {k: v / total for k, v in raw.items()}


def fair_probs(game: dict) -> tuple[dict[str, float], int] | None:
    """Combine all bookmakers for one game. Returns (fair probabilities, number of books)."""
    books = {}
    for b in game.get("bookmakers", []):
        for mk in b.get("markets", []):
            if mk.get("key") == "h2h":
                p = devig(mk.get("outcomes", []))
                if p:
                    books[b.get("key")] = p
    if len(books) < config.SPORTS_MIN_BOOKS:
        return None
    if "pinnacle" in books:
        return books["pinnacle"], len(books)
    names = set().union(*[set(p) for p in books.values()])
    med = {n: statistics.median([p[n] for p in books.values() if n in p]) for n in names}
    total = sum(med.values())
    return {n: v / total for n, v in med.items()}, len(books)


def fetch_odds(sport: str) -> tuple[list[dict], int | None]:
    r = requests.get(ODDS_URL.format(sport=sport), timeout=20, params={
        "apiKey": config.ODDS_API_KEY, "regions": config.ODDS_REGIONS,
        "markets": "h2h", "oddsFormat": "decimal"})
    remaining = r.headers.get("x-requests-remaining")
    r.raise_for_status()
    games = []
    for g in r.json():
        fp = fair_probs(g)
        if fp:
            games.append({"sport": sport, "home": g["home_team"], "away": g["away_team"],
                          "start": data.parse_ts(g.get("commence_time")), "fair": fp[0], "books": fp[1]})
    try:
        remaining = int(float(remaining)) if remaining is not None else None
    except ValueError:
        remaining = None
    return games, remaining


def refresh_odds(db, fetch=fetch_odds, now=None) -> dict:
    """Fetch odds if they're due and credits allow. Cached in the database between runs."""
    now = now or time.time()
    cache = db.get("odds_cache", {"ts": 0, "games": [], "remaining": None})
    if not config.ODDS_API_KEY:
        return cache
    due = now - cache.get("ts", 0) >= config.ODDS_EVERY_MINUTES * 60
    rem = cache.get("remaining")
    if not due or (rem is not None and rem < config.ODDS_MIN_REMAINING):
        return cache
    games, remaining = [], rem
    for sport in config.ODDS_SPORTS:
        try:
            g, remaining = fetch(sport)
            games += g
        except Exception as e:
            db.log(f"sports odds {sport} failed: {e}", "error")
    cache = {"ts": now, "games": games, "remaining": remaining}
    db.set("odds_cache", cache)
    db.log(f"Bookmaker odds updated: {len(games)} games"
           + (f", {remaining} API credits left this month" if remaining is not None else ""))
    return cache


# ---------------------------------------------------------------------
#  Matching games to Polymarket markets
# ---------------------------------------------------------------------
SKIP_WORDS = re.compile(r"\b(spread|o/u|over|under|total|1h|2h|half|quarter|period|map|set|"
                        r"handicap|points|goals|corners|cards|btts|both teams)\b", re.I)


def outcome_for(team_names: list[str], label: str, question: str) -> str | None:
    """Which bookmaker outcome does this Polymarket outcome/market mean?"""
    if re.search(r"\bdraw\b|\btie\b", f"{label} {question}", re.I):
        return "Draw"
    toks = set(_tokens(label))
    hits = [t for t in team_names if team_in(t, toks)]
    return hits[0] if len(hits) == 1 else None


def find_opportunities(events: list[dict], games: list[dict], now=None) -> list[dict]:
    """Every (market, side) where Polymarket's price is below the bookmakers' fair probability."""
    now = now or time.time()
    found = []
    for ev in events:
        markets = [m for m in (data.parse_market(x) for x in ev.get("markets", [])) if m]
        markets = [m for m in markets if m.game_start and not m.closed and m.line is None
                   and (m.sports_type in (None, "", "moneyline")) and not SKIP_WORDS.search(m.question)]
        if not markets:
            continue
        text = set(_tokens(" ".join([ev.get("title", "")] + [m.question for m in markets])))
        start = markets[0].game_start
        for g in games:
            if not g["start"] or abs(g["start"] - start) > 3 * 3600:
                continue
            if not (team_in(g["home"], text) and team_in(g["away"], text)):
                continue
            if start - now < config.SPORTS_MIN_MINUTES_BEFORE * 60:
                break  # too close to kick-off (or already playing)
            teams = [g["home"], g["away"]]
            for m in markets:
                if m.is_yes_no:  # e.g. "Will Arsenal win?" / "Will it be a draw?"
                    who = outcome_for(teams, m.group_title or m.question, m.question)
                    if not who or who not in g["fair"]:
                        continue
                    sides = [("YES", g["fair"][who], m.best_ask), ("NO", 1 - g["fair"][who], 1 - m.best_bid)]
                else:            # e.g. outcomes ("Lakers", "Celtics")
                    a = outcome_for(teams, m.outcomes[0], "")
                    b = outcome_for(teams, m.outcomes[1], "")
                    if not a or not b or a == b or a not in g["fair"] or b not in g["fair"]:
                        continue
                    sides = [("YES", g["fair"][a], m.best_ask), ("NO", g["fair"][b], 1 - m.best_bid)]
                for side, fair, price in sides:
                    edge = fair - price
                    if abs(fair - (m.mid if side == "YES" else 1 - m.mid)) > config.SPORTS_MAX_GAP:
                        continue  # gap too big to be real: probably a mismatched game
                    if edge >= config.SPORTS_MIN_EDGE and 0.05 <= price <= 0.95:
                        found.append({"market": m, "side": side, "fair": fair, "price": price,
                                      "edge": edge, "game": f"{g['away']} @ {g['home']}",
                                      "books": g["books"], "sport": g["sport"]})
            break
    found.sort(key=lambda x: -x["edge"])
    return found
