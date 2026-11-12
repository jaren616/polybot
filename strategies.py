"""The three strategies, plus position upkeep (marking prices, settling resolved markets)."""
import time

import ai
import arb_core
import config
import data
import news_feeds
from matcher import Matcher


# =====================================================================
#  1. AI NEWS TRADING
# =====================================================================
def required_edge(buy_price):
    """Smaller edge allowed for high-probability buys (see config)."""
    return config.MIN_EDGE_HIGH if buy_price >= config.HIGH_PRICE_ZONE else config.MIN_EDGE


def decide(j, market):
    """LLM judgement -> (side, edge) or None.
    Buying YES costs best_ask, worth p.  Buying NO costs 1 - best_bid, worth 1 - p."""
    if not j or not j.relevant or j.already_priced_in or j.confidence < config.MIN_LLM_CONFIDENCE:
        return None
    best = None
    for side, price, edge in (("YES", market.best_ask, j.prob_yes - market.best_ask),
                              ("NO", 1 - market.best_bid, market.best_bid - j.prob_yes)):
        if price >= config.HIGH_PRICE_ZONE and j.confidence < config.HIGH_PRICE_MIN_CONFIDENCE:
            continue
        if edge >= required_edge(price) and (best is None or edge > best[1]):
            best = (side, edge)
    return best


class NewsStrategy:
    def __init__(self, db, broker):
        self.db, self.broker = db, broker
        self.matcher, self.loaded_at = None, 0.0

    def refresh(self):
        if time.time() - self.loaded_at > config.MARKETS_REFRESH_EVERY or not self.matcher:
            markets = data.load_active_markets()
            self.matcher, self.loaded_at = Matcher(markets), time.time()
            self.db.set("markets_loaded", len(markets))

    def run(self, items=None):
        self.refresh()
        items = news_feeds.fetch_all() if items is None else items
        now, n = time.time(), 0
        for item in items:
            if self.db.seen(item.id):
                continue
            age = (now - item.published_ts) / 60
            if age > config.MAX_NEWS_AGE_MINUTES:
                continue
            for score, mk in self.matcher.candidates(item.text):
                fresh = data.get_market(mk.id) or mk
                j = ai.judge(item, fresh, age)
                d = decide(j, fresh)
                detail = item.text[:300]
                payload = {"source": item.source, "news_url": item.url, "age_min": round(age),
                           "match": round(score, 1), "yes": round(fresh.mid, 3),
                           "bid": fresh.best_bid, "ask": fresh.best_ask}
                if j:
                    payload.update(p_yes=round(j.prob_yes, 3), conf=round(j.confidence, 2),
                                   priced_in=j.already_priced_in, reason=j.reasoning)
                sid = self.db.add_signal("news", fresh.question, detail, fresh.id, fresh.url, payload,
                                         action="" if j else (
                                             "AI didn't answer (check the API key / credit) - judge it yourself"
                                             if config.ANTHROPIC_API_KEY else "no AI key - judge it yourself"))
                n += 1
                if d:
                    side, edge = d
                    self.broker.buy(fresh, side, j.prob_yes, "news", signal_id=sid)
                elif j:
                    self.db.set_signal_action(sid, "no trade: " + (
                        "not relevant" if not j.relevant else
                        "already priced in" if j.already_priced_in else
                        "low confidence" if j.confidence < config.MIN_LLM_CONFIDENCE else "edge too small"))
        self.db.set("last_news", now)
        return n


# =====================================================================
#  2 + 3. ARBITRAGE + PRICE MOVERS (share one big load of events)
# =====================================================================
class SlowScan:
    def __init__(self, db):
        self.db = db

    def run(self, events=None, book=None):
        book = book or arb_core.get_book
        events = arb_core.load_events() if events is None else events
        all_legs, found, n_niche = [], [], 0
        for ev in events:
            raw = ev.get("markets", [])
            legs = [l for l in (arb_core.leg_from_market(m) for m in raw) if l]
            all_legs.extend(legs)
            if config.ENABLE_ARB and legs and arb_core.is_niche(legs):
                n_niche += 1
                found += arb_core.check_sum(ev, legs, book, complete=len(legs) == len(raw))
        if config.ENABLE_ARB:
            found += arb_core.check_ladders(all_legs, book)
            found = [a for a in found if a["profit"] * a["size"] >= arb_core.MIN_DOLLARS]
            before = {r["key"] for r in self.db.q("SELECT key FROM arbs WHERE open=1")}
            self.db.record_arbs(found)
            for a in found:
                if a["key"] not in before:  # only signal new gaps, not every scan
                    self.db.add_signal("arb", a["event"], a["detail"], None, a["url"],
                                       {"type": a["type"], "profit_c": round(a["profit"] * 100, 1),
                                        "size": round(a["size"]), "max_usd": round(a["profit"] * a["size"], 2),
                                        "days": round(a["days"]), "apr": round(a["apr"], 3),
                                        "legs": a["legs"][:12]},
                                       action="alert only - check rules before acting")
        movers = detect_movers(self.db, all_legs) if config.ENABLE_MOVERS else 0
        self.db.set("last_slow", time.time())
        self.db.set("slow_stats", {"events": len(events), "niche": n_niche,
                                   "arbs_open": len(found), "movers": movers})
        return found


def detect_movers(db, legs, now=None) -> int:
    now = now or time.time()
    good = {l["id"]: l for l in legs
            if l["liq"] >= config.MOVER_MIN_LIQUIDITY and 0 < l["bid"] < l["ask"] <= 1}
    ref_ts, ref = db.prices_at_or_before(now - config.MOVE_WINDOW_MINUTES * 60)
    db.add_prices([(i, (l["bid"] + l["ask"]) / 2, l["vol24"]) for i, l in good.items()], now)
    if ref_ts is None or now - ref_ts > config.MOVE_WINDOW_MINUTES * 60 * 1.5:
        return 0  # not enough history yet (needs an hour of running)
    n = 0
    cooldown = now - config.MOVER_COOLDOWN_HOURS * 3600
    for i, l in good.items():
        if i not in ref or l["ask"] - l["bid"] > config.MOVER_MAX_SPREAD:
            continue
        old_mid, old_vol = ref[i]
        mid = (l["bid"] + l["ask"]) / 2
        move, vol_jump = mid - old_mid, l["vol24"] - old_vol
        reasons = []
        if abs(move) >= config.MOVE_POINTS:
            reasons.append(f"price {'up' if move > 0 else 'down'} {abs(move) * 100:.0f} points "
                           f"({old_mid:.2f} -> {mid:.2f}) in ~{(now - ref_ts) / 60:.0f} min")
        if vol_jump >= config.VOL_SPIKE_USD:
            reasons.append(f"24h volume up ${vol_jump:,.0f}")
        if reasons and not db.recent_signal("mover", i, cooldown):
            db.add_signal("mover", l["question"], "; ".join(reasons), i,
                          f"https://polymarket.com/market/{l['slug']}",
                          {"from": round(old_mid, 3), "to": round(mid, 3), "move": round(move, 3),
                           "vol_jump": round(vol_jump)},
                          action="alert only - look for the news behind it")
            n += 1
    return n


# =====================================================================
#  POSITION UPKEEP
# =====================================================================
def mark_positions(db, get=None):
    get = get or data.get_market
    for p in db.open_positions():
        m = get(p["market_id"])
        if m:
            db.update_mark(p["id"], m.mid if p["side"] == "YES" else 1 - m.mid)
    return db.snapshot_equity()


def settle_positions(db, get=None):
    get = get or data.get_market
    n = 0
    for p in db.open_positions():
        m = get(p["market_id"])
        if m and m.closed and m.yes_final is not None:
            pnl = db.close_position(p["id"], m.yes_final)
            db.log(f"SETTLED {p['side']} {p['question'][:60]}: {'+' if pnl >= 0 else ''}${pnl:.2f}"
                   + (" (redeem winnings on polymarket.com)" if p["mode"] == "live" and pnl > 0 else ""))
            n += 1
    db.set("last_settle", time.time())
    return n
