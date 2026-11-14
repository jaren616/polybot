"""Offline tests for the whole bot. No internet, no keys, no money.
Run:  py test_all.py   -> should end with ALL TESTS PASSED"""
import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

config.DB_PATH = ":memory:"
config.ANTHROPIC_API_KEY = "test"
import ai
import broker as broker_mod
import data
import dashboard
import news_feeds
import strategies
from broker import Broker, LiveTrader, kelly_stake
from db import DB
from engine import Engine

ok = 0


def check(cond, msg):
    global ok
    assert cond, msg
    ok += 1


def mk(id, q, bid, ask, closed=False, final=None, liq=50_000):
    prices = [str(final), str(1 - final)] if closed else [str((bid + ask) / 2), str(1 - (bid + ask) / 2)]
    return data.parse_market({
        "id": id, "question": q, "slug": f"s{id}", "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(prices), "bestBid": bid, "bestAsk": ask, "liquidityNum": liq,
        "volume24hr": 1000, "endDate": "2026-12-31", "closed": closed,
        "clobTokenIds": json.dumps([f"y{id}", f"n{id}"]), "orderMinSize": 5})


# ---------------- 1. Kelly sizing ----------------
check(kelly_stake(0.6, "YES", 0.5, 1000) == 10.0, "kelly capped at MAX_STAKE")       # 0.25*0.2*1000=50 -> cap 10
check(abs(kelly_stake(0.52, "YES", 0.5, 100) - 1.0) < 1e-9, "small kelly")            # 0.25*0.04*100 = 1.0
check(kelly_stake(0.45, "YES", 0.5, 1000) == 0.0, "no bet without edge")
check(kelly_stake(0.2, "NO", 0.6, 1000) == 10.0, "NO side: q=0.8 vs price 0.6")
check(kelly_stake(0.51, "YES", 0.5, 100) == 0.0, "below MIN_STAKE -> 0")

# ---------------- 2. Broker + risk ----------------
db = DB(":memory:")
b = Broker(db)
m1 = mk(1, "Will the Bank of England cut interest rates in December 2026?", 0.39, 0.41)
pid = b.buy(m1, "YES", 0.65, "news")
check(pid is not None, "paper buy works")
p = db.open_positions()[0]
check(abs(p["price"] - 0.41) < 1e-9 and p["side"] == "YES" and p["cost"] <= config.MAX_STAKE, "buy at ask, capped")
check(b.buy(m1, "YES", 0.65, "news") is None, "no second position in same market")
check(b.buy(mk(2, "x", 0.99, 0.995), "YES", 0.999, "news") is None, "refuse price above MAX_PRICE")
sid = db.add_signal("news", "t", "d")
b.buy(mk(3, "y", 0.30, 0.32), "NO", 0.10, "news", signal_id=sid)
p3 = [p for p in db.open_positions() if p["market_id"] == "3"][0]
check(p3["side"] == "NO" and abs(p3["price"] - 0.70) < 1e-9, "NO bought at 1 - bid")
check("paper buy No" in db.q("SELECT action FROM signals WHERE id=?", (sid,))[0]["action"], "signal records action")
db.set("paused", True)
check(b.buy(mk(4, "z", 0.30, 0.32), "YES", 0.6, "news") is None, "paused blocks trades")
db.set("paused", False)
old = config.MAX_TRADES_PER_DAY
config.MAX_TRADES_PER_DAY = 2
check(b.buy(mk(5, "w", 0.30, 0.32), "YES", 0.6, "news") is None, "max trades/day")
config.MAX_TRADES_PER_DAY = old

# ---------------- 3. Marking + settlement P&L ----------------
markets = {"1": mk(1, m1.question, 0.0, 0.0, closed=True, final=1.0),
           "3": mk(3, "y", 0.50, 0.52)}
strategies.mark_positions(db, get=lambda i: markets.get(i))
p3 = [p for p in db.open_positions() if p["market_id"] == "3"][0]
check(abs(p3["mark"] - 0.49) < 1e-9, "NO marked at 1 - mid")
n = strategies.settle_positions(db, get=lambda i: markets.get(i))
closed = db.q("SELECT * FROM positions WHERE status='closed'")
check(n == 1 and abs(closed[0]["pnl"] - (closed[0]["shares"] - closed[0]["cost"])) < 1e-9, "YES win pays shares - cost")
money = db.money()
check(abs(money["equity"] - (config.BANKROLL + money["realized"] + money["open_value"] - money["open_cost"])) < 1e-9, "equity identity")

# ---------------- 4. Daily loss limit auto-pause ----------------
db2 = DB(":memory:")
b2 = Broker(db2)
db2.x("INSERT INTO equity VALUES (?,?,?,?,?)", (time.time() - 60, 1000, 1000, 0, 0))
db2.add_position(ts=time.time(), mode="paper", strategy="t", market_id="L", question="loser", url="", side="YES",
                 token_id="", price=0.5, shares=200, cost=100, est_prob=0.6, market_yes=0.5, mark=0.5)
db2.close_position(1, 0.0)  # lose $100
check(b2.buy(mk(9, "q", 0.4, 0.42), "YES", 0.7, "news") is None and db2.paused(), "daily loss limit pauses bot")

# ---------------- 5. News strategy end-to-end ----------------
db3 = DB(":memory:")
b3 = Broker(db3)
ns = strategies.NewsStrategy(db3, b3)
mk_boe = mk(11, "Will the Bank of England cut interest rates in December 2026?", 0.39, 0.41)
data.load_active_markets = lambda n=None: [mk_boe, mk(12, "Will Arsenal win the Premier League?", 0.3, 0.32)]
data.get_market = lambda i: mk_boe if i == "11" else None
real_judge_story = ai.judge_story
ai.judge_story = lambda item, ms, age, **k: (0, ai.Judgement(True, 0.65, 0.8, False, "strong signal"))
items = [news_feeds.NewsItem("a", "BBC", "Bank of England governor signals interest rate cut is imminent", "u", time.time() - 60),
         news_feeds.NewsItem("b", "BBC", "Rain expected tomorrow", "u", time.time() - 60),
         news_feeds.NewsItem("c", "BBC", "Bank of England rates old story", "u", time.time() - 7200)]
check(ns.run(items) == 1, "one relevant fresh item -> one signal")
check(ns.run(items) == 0, "dedupe on second pass")
check(len(db3.open_positions()) == 1 and db3.open_positions()[0]["strategy"] == "news", "news trade placed")
# already holding the BoE market: it isn't shown to the AI again (saves the daily allowance)
asked = []
ai.judge_story = lambda item, ms, age, **k: asked.append(1) or (0, ai.Judgement(True, 0.65, 0.8, False, ""))
ns.run([news_feeds.NewsItem("d0", "BBC", "Bank of England interest rate cut again", "u", time.time())])
check(not asked, "held market skipped without an AI call")
db3.x("DELETE FROM positions")
ai.judge_story = lambda item, ms, age, **k: (0, ai.Judgement(True, 0.42, 0.9, True, "priced in"))
ns.run([news_feeds.NewsItem("d", "BBC", "Bank of England interest rate cut talk continues", "u", time.time())])
check("already priced in" in db3.q("SELECT action FROM signals ORDER BY id DESC LIMIT 1")[0]["action"], "priced-in recorded")
ai.judge_story = lambda item, ms, age, **k: (None, ai.Judgement(False, 0.5, 0.9, False, "none of these"))
ns.run([news_feeds.NewsItem("e", "BBC", "Bank of England staff party photos", "u", time.time())])
last = db3.q("SELECT * FROM signals ORDER BY id DESC LIMIT 1")[0]
check(last["action"] == "no trade: no market affected" and last["title"].startswith("(no market)"), "AI can say no market fits")
# price moves while the AI is thinking: re-check before buying
seen_then = {}
def judge_then(item, ms, age, prices_then=None):
    seen_then.update(prices_then or {})
    return 0, ai.Judgement(True, 0.47, 0.8, False, "")
ai.judge_story = judge_then
db3.add_prices([("11", 0.30, 0)], time.time() - 300)
moved = mk(11, "Will the Bank of England cut interest rates in December 2026?", 0.44, 0.46)
calls = iter([mk_boe, moved])
data.get_market = lambda i: next(calls)
ns.run([news_feeds.NewsItem("e2", "BBC", "Bank of England interest rate cut hint", "u", time.time() - 60)])
check("price moved" in db3.q("SELECT action FROM signals ORDER BY id DESC LIMIT 1")[0]["action"]
      and not db3.open_positions(), "no buy if the edge vanished while the AI judged")
check(seen_then.get("11") == 0.30, "AI told the price from when the news came out")
data.get_market = lambda i: mk_boe if i == "11" else None
ns.run([news_feeds.NewsItem("e3", "BBC", "Bank of England interest rate cut hinted", "u", time.time() - 60)])
check(len(db3.open_positions()) == 1, "buys when the price held still")
# the AI sees all candidates in one call and its pick is used
seen_prompts = []
real_ask = ai._ask
ai._ask = lambda prompt: (seen_prompts.append(prompt) or
                          '{"pick": 2, "prob_yes": 0.7, "confidence": 0.8, "already_priced_in": false, "reasoning": "x"}')
cand = [mk(70, "Will A happen?", 0.4, 0.41), mk(71, "Will B happen?", 0.3, 0.31)]
pk, jj = real_judge_story(news_feeds.NewsItem("f", "BBC", "B is happening", "u", time.time()), cand, 1,
                          prices_then={"71": 0.20})
check(pk == 1 and abs(jj.prob_yes - 0.7) < 1e-9 and "[1] Will A happen?" in seen_prompts[0]
      and "[2] Will B happen?" in seen_prompts[0], "one call shows every candidate; pick is 1-based")
check("TODAY (UTC): " + time.strftime("%Y-%m-%d", time.gmtime()) in seen_prompts[0]
      and "was 0.200 when the news came out" in seen_prompts[0], "prompt has today's date and the earlier price")
ai._ask = lambda prompt: '{"pick": null, "confidence": 0.9, "reasoning": "nothing"}'
check(real_judge_story(news_feeds.NewsItem("g", "BBC", "x", "u", time.time()), cand, 1)[0] is None, "null pick")
ai._ask = lambda prompt: '{"pick": 9, "prob_yes": 0.7, "confidence": 0.8}'
check(real_judge_story(news_feeds.NewsItem("h", "BBC", "x", "u", time.time()), cand, 1)[0] is None, "out-of-range pick ignored")
ai._ask = lambda prompt: None
check(real_judge_story(news_feeds.NewsItem("i", "BBC", "x", "u", time.time()), cand, 1) == (None, None), "no answer")
ai._ask = real_ask
# markets ending soon are shown first
soon, late = mk(80, "Will the Fed cut rates in December?", 0.4, 0.41), mk(81, "Will the Fed cut rates in 2028?", 0.4, 0.41)
soon.end_ts, late.end_ts = time.time() + 20 * 86400, time.time() + 600 * 86400
ns2 = strategies.NewsStrategy(DB(":memory:"), Broker(DB(":memory:")))
ns2.matcher = strategies.Matcher([late, soon] + [mk(82 + i, f"Will team {i} win the league?", 0.3, 0.31) for i in range(8)])
sl = ns2.shortlist("Fed signals it will cut rates soon")
check(sl and sl[0][1].id == "80", "near-term market ranked first")

# ---------------- 6. Arbitrage + movers ----------------
def ev_mk(id, q, bid, ask, title=None, vol=100, liq=1000, end="2026-12-31"):
    return {"id": id, "question": q, "groupItemTitle": title, "outcomes": json.dumps(["Yes", "No"]),
            "clobTokenIds": json.dumps([f"t{id}", f"n{id}"]), "bestBid": bid, "bestAsk": ask,
            "endDate": end + "T00:00:00Z", "volume24hr": vol, "liquidityNum": liq, "slug": f"m{id}",
            "active": True, "closed": False}
events = [{"id": "e1", "title": "Who wins Tiny Town?", "negRisk": True, "slug": "tiny", "endDate": "2026-12-31",
           "markets": [ev_mk(1, "Ann?", 0.50, 0.52, "Ann"), ev_mk(2, "Bob?", 0.35, 0.37, "Bob"),
                       ev_mk(3, "Cat?", 0.20, 0.22, "Cat")]},
          {"id": "e2", "title": "Mover", "slug": "mv", "markets": [ev_mk(50, "Will X happen?", 0.40, 0.42, liq=5000)]}]
book = lambda t: {"bid": 0.5 if t == "t1" else 0.35 if t == "t2" else 0.2, "bid_size": 200,
                  "ask": 0.52, "ask_size": 150} if t in ("t1", "t2", "t3") else None
db4 = DB(":memory:")
scan = strategies.SlowScan(db4)
now = time.time()
strategies.detect_movers(db4, [], now - 3600)  # empty history baseline is fine
found = scan.run(events, book)
check(len(found) == 1 and found[0]["type"] == "SUM-NO", "arb found")
check(db4.q("SELECT COUNT(*) n FROM signals WHERE kind='arb'")[0]["n"] == 1, "arb signal once")
scan.run(events, book)
check(db4.q("SELECT COUNT(*) n FROM signals WHERE kind='arb'")[0]["n"] == 1, "no duplicate arb signal")
check(db4.q("SELECT scans FROM arbs")[0]["scans"] == 2, "arb tracked across scans")
# movers: price 0.41 an hour ago -> 0.61 now
db5 = DB(":memory:")
leg = lambda bid, ask, vol=1000: [{"id": "50", "question": "Will X happen?", "slug": "mv", "bid": bid, "ask": ask,
                                    "vol24": vol, "liq": 5000}]
strategies.detect_movers(db5, leg(0.40, 0.42), now - 3700)
check(strategies.detect_movers(db5, leg(0.60, 0.62, 20000), now) == 1, "mover detected")
check("price up 20 points" in db5.q("SELECT detail FROM signals")[0]["detail"], "mover detail")
check(strategies.detect_movers(db5, leg(0.80, 0.82), now + 60) == 0, "mover cooldown")

# ---------------- 7. Live mode refuses without setup ----------------
config.MODE = "live"
try:
    LiveTrader(DB(":memory:"))
    check(False, "live should refuse")
except RuntimeError as e:
    check("POLY_PRIVATE_KEY" in str(e) and "POLYBOT_LIVE_CONFIRM" in str(e), "live refuses with clear reasons")
config.MODE = "paper"
# live blocked by geoblock even with everything set
config.MODE, config.POLY_PRIVATE_KEY, config.POLY_FUNDER = "live", "0xabc", "0xdef"
os.environ["POLYBOT_LIVE_CONFIRM"] = config.LIVE_CONFIRM_PHRASE
data.geoblock = lambda: {"blocked": True, "country": "GB"}
try:
    LiveTrader(DB(":memory:"))
    check(False, "geoblocked should refuse")
except RuntimeError as e:
    check("blocked" in str(e), "geoblock respected")
config.MODE, config.POLY_PRIVATE_KEY, config.POLY_FUNDER = "paper", None, None
os.environ.pop("POLYBOT_LIVE_CONFIRM")

# ---------------- 8. Dashboard API ----------------
srv = dashboard.serve(db3, port=0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
st = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state").read())
check(st["mode"] in ("paper", "live") and len(st["open"]) == 1 and len(st["signals"]) >= 2, "state API")
html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
check("<title>Polybot Dashboard</title>" in html, "dashboard page")
urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/pause", method="POST"))
check(db3.paused(), "pause button")
urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/resume", method="POST"))
check(not db3.paused(), "resume button")
srv.shutdown()

# ---------------- 9. High-probability decisions ----------------
J = ai.Judgement
fav = mk(20, "fav", 0.92, 0.93)                     # YES costs 0.93, NO costs 0.08
check(strategies.decide(J(True, 0.96, 0.85, False, ""), fav) == ("YES", 0.96 - 0.93) or
      abs(strategies.decide(J(True, 0.96, 0.85, False, ""), fav)[1] - 0.03) < 1e-9, "high-prob YES with 3pt edge")
check(strategies.decide(J(True, 0.96, 0.70, False, ""), fav) is None, "high-prob needs 80% confidence")
check(strategies.decide(J(True, 0.94, 0.90, False, ""), fav) is None, "1pt edge too small even at 90c+")
mid = mk(21, "mid", 0.49, 0.51)
check(strategies.decide(J(True, 0.54, 0.90, False, ""), mid) is None, "normal zone still needs 5 points")
check(strategies.decide(J(True, 0.59, 0.90, False, ""), mid)[0] == "YES", "normal zone 8pt edge trades")
check(strategies.decide(J(True, 0.565, 0.90, False, ""), mid)[0] == "YES", "5.5pt edge now trades")
longshot = mk(22, "ls", 0.06, 0.07)                 # NO costs 0.94
d = strategies.decide(J(True, 0.02, 0.9, False, ""), longshot)
check(d is not None and d[0] == "NO" and abs(d[1] - 0.04) < 1e-9, "NO on a long shot counts as high-prob")
db6 = DB(":memory:")
check(Broker(db6).buy(fav, "YES", 0.96, "news") is not None, "can now buy at 93c")
check(abs(db6.open_positions()[0]["price"] - 0.93) < 1e-9, "bought at ask")

# ---------------- 10. Parallel feed fetching ----------------
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Test Feed</title>
<item><title>Fed holds rates &amp; signals cut</title><description>&lt;p&gt;Powell said&lt;/p&gt;</description>
<link>https://x/1</link><pubDate>Wed, 11 Nov 2026 12:00:00 GMT</pubDate></item></channel></rss>"""
class FakeResp:
    def __init__(s, ok): s.ok, s.content = ok, RSS
    def raise_for_status(s):
        if not s.ok: raise RuntimeError("404")
calls = []
def fake_get(url, timeout, headers):
    calls.append(url); time.sleep(0.2)
    return FakeResp("bad" not in url)
real_get = news_feeds.requests.get
news_feeds.requests.get = fake_get
old_feeds = config.RSS_FEEDS
config.RSS_FEEDS = [f"https://feed{i}" for i in range(12)] + ["https://bad"]
t0 = time.time(); got = news_feeds.fetch_rss(); took = time.time() - t0
config.RSS_FEEDS = old_feeds
news_feeds.requests.get = real_get
check(len(calls) == 13 and took < 1.0, f"13 feeds fetched in parallel ({took:.2f}s)")
check(len(got) == 1 and got[0].text == "Fed holds rates & signals cut. Powell said", "dedupe across feeds + HTML cleaned")
check(len(config.RSS_FEEDS) >= 30, "30+ news sources configured")

# ---------------- 11. Static (GitHub Pages) dashboard ----------------
import tempfile, re as _re
out = os.path.join(tempfile.mkdtemp(), "site", "index.html")
dashboard.export_static(db3, out)
page = open(out, encoding="utf-8").read()
blob = _re.search(r"window.STATIC_STATE=(.*?);</script>", page, _re.S).group(1)
st2 = json.loads(blob.replace("<\\/", "</"))
check(st2["static"] and len(st2["open"]) == 1, "static dashboard embeds the data")
check('http-equiv="refresh"' in page and "</script><script>" in page, "static page auto-refreshes")

# ---------------- 12. Engine one pass ----------------
news_feeds.fetch_all = lambda: []
strategies.arb_core.load_events = lambda: events
strategies.arb_core.get_book = book
e = Engine(DB(":memory:"), Broker(DB(":memory:")))
e.run_once()
check(e.db.get("last_slow") and e.db.get("last_news"), "engine ran every task")

# ---------------- 13. Sports value ----------------
import sports
now = time.time()
start = now + 5 * 3600
iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))
def sm(id, q, outcomes, p0, bid, ask, title="", smt="moneyline", line=None):
    return {"id": id, "question": q, "outcomes": json.dumps(outcomes), "outcomePrices": json.dumps([str(p0), str(1 - p0)]),
            "bestBid": bid, "bestAsk": ask, "slug": f"s{id}", "liquidityNum": 20000, "volume24hr": 5000,
            "gameStartTime": iso, "sportsMarketType": smt, "line": line, "groupItemTitle": title,
            "clobTokenIds": json.dumps([f"a{id}", f"b{id}"]), "endDate": iso}
sp_events = [
    {"title": "Lakers vs. Celtics", "markets": [
        sm(101, "Lakers vs. Celtics", ["Lakers", "Celtics"], 0.40, 0.39, 0.41),
        sm(102, "Lakers vs. Celtics: O/U 220.5", ["Over", "Under"], 0.5, 0.49, 0.51, smt="totals", line=220.5)]},
    {"title": "Man City vs. Chelsea", "markets": [
        sm(201, "Will Man City win on 2026-11-15?", ["Yes", "No"], 0.50, 0.49, 0.51, title="Man City"),
        sm(202, "Will Man City vs. Chelsea end in a draw?", ["Yes", "No"], 0.25, 0.24, 0.26, title="Draw"),
        sm(203, "Will Chelsea win on 2026-11-15?", ["Yes", "No"], 0.25, 0.24, 0.26, title="Chelsea")]},
    {"title": "Man United vs. Arsenal", "markets": [   # same time, different game: must NOT match City
        sm(301, "Will Man United win?", ["Yes", "No"], 0.30, 0.29, 0.31, title="Man United")]}]
games = [{"sport": "basketball_nba", "home": "Boston Celtics", "away": "Los Angeles Lakers", "start": start,
          "fair": {"Boston Celtics": 0.52, "Los Angeles Lakers": 0.48}, "books": 8},
         {"sport": "soccer_epl", "home": "Manchester City", "away": "Chelsea", "start": start + 600,
          "fair": {"Manchester City": 0.58, "Draw": 0.22, "Chelsea": 0.20}, "books": 9}]
opps = sports.find_opportunities(sp_events, games, now)
got = {(o["market"].id, o["side"]): round(o["edge"], 3) for o in opps}
check(got.get(("101", "YES")) == 0.07, f"NBA: Lakers 48% fair vs 41c ask -> 7pt edge {got}")
check(got.get(("201", "YES")) == 0.07, "EPL: City 58% fair vs 51c -> buy YES")
check(got.get(("203", "NO")) is not None and abs(got[("203", "NO")] - 0.04) < 1e-9, "EPL: Chelsea NO 80% vs 76c")
check(not any(k[0] in ("102", "301") for k in got), "totals market and the wrong game are skipped")
check(not sports.find_opportunities(sp_events, games, start - 600), "no bets within 30 min of kick-off")
odd = [dict(games[0], fair={"Boston Celtics": 0.15, "Los Angeles Lakers": 0.85})]
check(not sports.find_opportunities(sp_events, odd, now), "huge gap = probably wrong match -> skipped")
# odds fetching respects schedule + credits
dbs = DB(":memory:")
config.ODDS_API_KEY = "k"
calls2 = []
fake = lambda sport: (calls2.append(sport) or ([dict(games[0])] if sport == "basketball_nba" else []), 100)
sports.refresh_odds(dbs, fetch=fake, now=now)
sports.refresh_odds(dbs, fetch=fake, now=now + 60)
check(len(calls2) == len(config.ODDS_SPORTS), "odds fetched once, not every scan")
dbs.set("odds_cache", {"ts": 0, "games": [], "remaining": 3})
sports.refresh_odds(dbs, fetch=fake, now=now)
check(len(calls2) == len(config.ODDS_SPORTS), "stops when credits run low")
dbs.set("odds_cache", {"ts": now, "games": games, "remaining": 100})
ss = strategies.SportsStrategy(dbs, Broker(dbs))
check(ss.run(sp_events, fetch=fake, now=now) == 3, "sports strategy places 3 paper trades")
lbls = sorted(p["label"] for p in dbs.open_positions())
check(lbls == ["Lakers", "No", "Yes"], f"outcome labels stored {lbls}")
check(all(p["cost"] <= config.SPORTS_STAKE + 0.01 for p in dbs.open_positions()), "fixed $5 sports stake")
config.ODDS_API_KEY = None
check(sports.devig([{"name": "A", "price": 1.9}, {"name": "B", "price": 1.9}]) == {"A": 0.5, "B": 0.5}, "margin removed")
fp = sports.fair_probs({"bookmakers": [{"key": k, "markets": [{"key": "h2h", "outcomes": [
    {"name": "A", "price": pa}, {"name": "B", "price": pb}]}]} for k, pa, pb in
    [("x", 1.5, 2.6), ("pinnacle", 1.6, 2.5), ("y", 1.4, 2.9)]]})
check(fp and fp[1] == 3 and abs(fp[0]["A"] - (1 / 1.6) / (1 / 1.6 + 1 / 2.5)) < 1e-9, "Pinnacle preferred")

# ---------------- 14. Momentum + exits ----------------
_old_mom, _old_fav = config.ENABLE_MOMENTUM, config.ENABLE_FAVOURITES
config.ENABLE_MOMENTUM = config.ENABLE_FAVOURITES = True   # test them even when switched off in config
dbm = DB(":memory:")
fs = strategies.FastScan(dbm, Broker(dbm))
def fm(id, bid, ask, liq=50000, end=None):
    m = mk(id, f"Market {id}?", bid, ask, liq=liq)
    m.end_ts = end
    return m
fs.run([fm(1, 0.40, 0.41), fm(2, 0.50, 0.51), fm(3, 0.30, 0.31, liq=100)], now=now - 900)
n = fs.run([fm(1, 0.48, 0.49), fm(2, 0.51, 0.52), fm(3, 0.45, 0.46, liq=100)], now=now)
opn = dbm.open_positions()
check(n == 1 and opn[0]["market_id"] == "1" and opn[0]["side"] == "YES" and opn[0]["strategy"] == "momentum",
      "momentum buys the 8pt mover only (not small move, not illiquid)")
check(fs.run([fm(1, 0.60, 0.61)], now=now + 60) == 0, "momentum cooldown / one position per market")
# exits: take profit
strategies.mark_positions(dbm, get=lambda i: fm(1, 0.55, 0.56), now=now + 120)
c = dbm.q("SELECT * FROM positions WHERE status='closed'")
check(len(c) == 1 and c[0]["exit_reason"] == "take profit" and abs(c[0]["pnl"] - (c[0]["shares"] * 0.55 - c[0]["cost"])) < 1e-9,
      "take profit sells at the bid")
pid = dbm.add_position(ts=now, mode="paper", strategy="momentum", market_id="9", question="q", url="", side="NO",
                       token_id="", price=0.60, shares=10, cost=6.0, est_prob=None, market_yes=0.4, mark=0.6, label="No")
strategies.mark_positions(dbm, get=lambda i: fm(9, 0.44, 0.45), now=now)   # NO now sells at 0.55
check(dbm.q("SELECT exit_reason FROM positions WHERE id=?", (pid,))[0]["exit_reason"] == "stop loss", "stop loss")
pid = dbm.add_position(ts=now - 7 * 3600, mode="paper", strategy="momentum", market_id="8", question="q", url="",
                       side="YES", token_id="", price=0.50, shares=10, cost=5.0, est_prob=None, market_yes=0.5, mark=0.5, label="Yes")
strategies.mark_positions(dbm, get=lambda i: fm(8, 0.50, 0.51), now=now)
check(dbm.q("SELECT exit_reason FROM positions WHERE id=?", (pid,))[0]["exit_reason"] == "time limit", "time limit exit")

# ---------------- 15. Favourites ----------------
dbf = DB(":memory:")
ff = strategies.FastScan(dbf, Broker(dbf))
config.ENABLE_MOMENTUM = False
ff.run([fm(31, 0.94, 0.95, end=now + 10 * 3600),    # YES at 95c, resolves in 10h -> buy
        fm(32, 0.94, 0.95, end=now + 5 * 86400),    # too far away
        fm(33, 0.04, 0.05, end=now + 3600),         # NO at 96c -> buy
        fm(34, 0.98, 0.99, end=now + 3600),         # 99c: above FAV_MAX_PRICE
        fm(35, 0.60, 0.61, end=now + 3600)], now=now)
config.ENABLE_MOMENTUM = True
fav = {(p["market_id"], p["side"]) for p in dbf.open_positions()}
check(fav == {("31", "YES"), ("33", "NO")}, f"favourites picks the right ones {fav}")

config.ENABLE_MOMENTUM, config.ENABLE_FAVOURITES = _old_mom, _old_fav

# ---------------- 16. Safety: paper-only strategies, caps, old databases ----------------
class FakeLive:
    def buy(self, *a): raise AssertionError("must not reach a live order")
dbl = DB(":memory:")
bl = Broker(dbl, live=FakeLive())
check(bl.buy(fm(40, 0.4, 0.41), "YES", None, "momentum", stake=5) is None, "momentum refused in live mode")
old_cap = config.STRATEGY_MAX_OPEN["favourites"]
config.STRATEGY_MAX_OPEN["favourites"] = 1
dbc = DB(":memory:"); bc = Broker(dbc)
bc.buy(fm(41, 0.94, 0.95), "YES", None, "favourites", stake=5)
check(bc.buy(fm(42, 0.94, 0.95), "YES", None, "favourites", stake=5) is None, "per-strategy cap")
config.STRATEGY_MAX_OPEN["favourites"] = old_cap
import sqlite3 as _sq, tempfile as _tf
oldp = os.path.join(_tf.mkdtemp(), "old.db")
c0 = _sq.connect(oldp)
c0.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, ts REAL, mode TEXT, strategy TEXT, market_id TEXT, question TEXT, url TEXT, side TEXT, token_id TEXT, price REAL, shares REAL, cost REAL, est_prob REAL, market_yes REAL, mark REAL, status TEXT DEFAULT 'open', yes_final REAL, pnl REAL, closed_ts REAL, order_id TEXT, signal_id INTEGER)")
c0.execute("INSERT INTO positions (ts,mode,strategy,market_id,question,url,side,price,shares,cost) VALUES (1,'paper','news','m','q','u','YES',0.5,10,5)")
c0.commit(); c0.close()
dbo = DB(oldp)
check(dbo.open_positions()[0]["label"] is None and len(dbo.strategy_stats()) == 1, "old database upgraded in place")
st3 = dashboard.state(dbm)
check(any(r["strategy"] == "momentum" for r in st3["by_strategy"]), "dashboard strategy breakdown")

# ---------------- 17. Free AI providers ----------------
import importlib
ai_real = importlib.reload(ai)   # earlier sections replaced ai.judge with a fake
strategies.ai = ai_real
saved = config.ANTHROPIC_API_KEY
config.ANTHROPIC_API_KEY = None
config.LLM_PROVIDER, config.LLM_MODEL_OVERRIDE = "auto", None  # ignore this computer's own settings
for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
    os.environ.pop(k, None)
check(ai_real.provider_name() is None, "no key -> no AI")
os.environ["GROQ_API_KEY"] = "g"
check(ai_real.provider_name() == "groq" and ai_real.model_name() == "openai/gpt-oss-120b", "Groq picked up")
os.environ["GEMINI_API_KEY"] = "m"
check(ai_real.provider_name() == "gemini", "Gemini preferred over Groq when both set")
config.LLM_PROVIDER = "groq"
check(ai_real.provider_name() == "groq", "LLM_PROVIDER forces a choice")
config.LLM_PROVIDER = "ollama"
check(ai_real.provider_name() == "ollama", "Ollama needs no key")
config.LLM_PROVIDER = "auto"
check(ai_real.daily_limit() == 300, "free tiers capped at 300 calls/day by default")
sent = {}
class PR:
    def __init__(s, code, body): s.status_code, s._b, s.text = code, body, json.dumps(body)
    def json(s): return s._b
def fake_post(url, headers, json, timeout):
    sent.update(url=url, headers=headers, body=json)
    return PR(200, {"choices": [{"message": {"content": 'Sure: {"relevant": true, "prob_yes": 0.7, "confidence": 0.8, '
                                              '"already_priced_in": false, "reasoning": "big news"}'}}]})
ai_real.requests.post = fake_post
item = news_feeds.NewsItem("z", "BBC", "Bank of England cuts rates", "u", time.time())
jj = ai_real.judge(item, mk_boe, 2)
check(jj and abs(jj.prob_yes - 0.7) < 1e-9, "Gemini answer parsed")
check(sent["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
      and sent["headers"]["Authorization"] == "Bearer m" and sent["body"]["model"] == "gemini-3.8-flash",
      "correct Gemini request")
ai_real.requests.post = lambda url, headers, json, timeout: PR(429, {"error": "rate"})
check(ai_real.judge(item, mk_boe, 2) is None and "rate limit" in ai_real.last_error, "429 handled")
ai_real.requests.post = fake_post
check(ai_real.judge(item, mk_boe, 2) is None, "pauses AI for 5 minutes after a rate limit")
ai_real._cooldown.clear()
ai_real.requests.post = lambda url, headers, json, timeout: PR(401, {"error": "bad key"})
check(ai_real.judge(item, mk_boe, 2) is None and "401" in ai_real.last_error, "bad key handled")
check(ai_real._cooldown["gemini"] > time.time() + 3000, "bad key/model pauses AI for an hour (saves the allowance)")
ai_real.requests.post = lambda url, headers, json, timeout: PR(404, [{"error": {"code": 404, "message": "model gone"}}])
ai_real._cooldown.clear()
ai_real.judge(item, mk_boe, 2)
check("model gone" in ai_real.last_error, "provider's own error message is shown")
ai_real._cooldown.clear()
# backup: Gemini overloaded (503) -> Groq answers
os.environ["GROQ_API_KEY"] = "q"
config.LLM_PROVIDER = "gemini"
def busy_gemini(url, headers, json, timeout):
    if "googleapis" in url:
        return PR(503, [{"error": {"code": 503, "message": "high demand"}}])
    return fake_post(url, headers, json, timeout)
ai_real.requests.post = busy_gemini
jj = ai_real.judge(item, mk_boe, 2)
check(jj is not None and ai_real.judged_by == "openai/gpt-oss-120b" and "api.groq.com" in sent["url"],
      "Gemini busy -> Groq answers instead")
check(ai_real.available() == ["gemini", "groq"], "backup order: chosen first, then the rest")
def timeout_gemini(url, headers, json, timeout):
    if "googleapis" in url:
        raise ai_real.requests.exceptions.ReadTimeout("read timed out")
    return fake_post(url, headers, json, timeout)
ai_real._cooldown.clear()
ai_real.requests.post = timeout_gemini
check(ai_real.judge(item, mk_boe, 2) is not None and ai_real.judged_by == "openai/gpt-oss-120b", "timeout -> backup")
config.LLM_PROVIDER = "auto"
os.environ.pop("GROQ_API_KEY")
ai_real._cooldown.clear()
# daily budget inside the news strategy
ai_real.requests.post = fake_post
os.environ["AI_MAX_CALLS_PER_DAY"] = "1"
config.AI_MAX_CALLS_PER_DAY = 1
db7 = DB(":memory:")
ns7 = strategies.NewsStrategy(db7, Broker(db7))
data.load_active_markets = lambda n=None: [mk_boe, mk(12, "Will Arsenal win the Premier League?", 0.3, 0.32)]
ns7.run([news_feeds.NewsItem("b1", "BBC", "Bank of England interest rate cut signalled", "u", time.time()),
         news_feeds.NewsItem("b2", "BBC", "Bank of England interest rate cut confirmed soon", "u", time.time())])
acts = [r["action"] for r in db7.q("SELECT action FROM signals ORDER BY id")]
check(len(acts) == 2 and "daily limit" in acts[1], f"daily AI budget respected {acts}")
check(json.loads(db7.q("SELECT data FROM signals ORDER BY id LIMIT 1")[0]["data"]).get("ai") == "gemini-3.8-flash",
      "signal records which AI judged it")
config.AI_MAX_CALLS_PER_DAY = None
os.environ.pop("AI_MAX_CALLS_PER_DAY")
for k in ("GEMINI_API_KEY", "GROQ_API_KEY"):
    os.environ.pop(k, None)
config.ANTHROPIC_API_KEY = saved

# ---------------- 18. Reset ----------------
dbr = DB(":memory:")
Broker(dbr).buy(mk(60, "q", 0.39, 0.41), "YES", 0.65, "news")
dbr.add_signal("news", "t", "d")
dbr.set("odds_cache", {"ts": 1, "games": [], "remaining": 99})
dbr.set("ai_calls_20261113", 42)
dbr.snapshot_equity()
dbr.reset("r1")
check(not dbr.open_positions() and not dbr.q("SELECT * FROM signals") and dbr.money()["equity"] == config.BANKROLL,
      "reset wipes trades and signals back to the bankroll")
check(dbr.get("odds_cache")["remaining"] == 99 and dbr.get("ai_calls_20261113") == 42 and dbr.get("reset_marker") == "r1",
      "reset keeps odds cache and today's AI count")
import run as run_mod
d = tempfile.mkdtemp(); cwd0 = os.getcwd(); os.chdir(d)
dbx = DB(os.path.join(d, "x.db"))
Broker(dbx).buy(mk(61, "q", 0.39, 0.41), "YES", 0.65, "news")
with open("RESET", "w") as f:
    f.write("2026-11-13 restart")
run_mod.Engine = lambda db, broker: type("E", (), {"run_once": lambda self: None})()
run_mod.cloud(dbx)
dbx = DB(os.path.join(d, "x.db"))
check(not dbx.open_positions() and dbx.get("reset_marker") == "2026-11-13 restart", "RESET file triggers a reset")
Broker(dbx).buy(mk(62, "q", 0.39, 0.41), "YES", 0.65, "news")
run_mod.cloud(dbx)
dbx = DB(os.path.join(d, "x.db"))
check(len(dbx.open_positions()) == 1, "same RESET text doesn't reset again")
# loop mode keeps ticking until time runs out
ticks = []
class LoopE:
    def run_once(self): ticks.append("once")
    def fast_tick(self): ticks.append("fast")
    def slow_tick(self): ticks.append("slow")
run_mod.Engine = lambda db, broker: LoopE()
real_sleep = run_mod.time.sleep
run_mod.time.sleep = lambda s: None
os.environ["POLYBOT_LOOP_MINUTES"] = "0.001"
t_end = time.time() + 0.2
run_mod.cloud(DB(os.path.join(d, "y.db")))
os.environ.pop("POLYBOT_LOOP_MINUTES")
run_mod.time.sleep = real_sleep
check(ticks[0] == "once" and "fast" in ticks and "slow" in ticks, "loop mode keeps running the strategies")
os.chdir(cwd0)

print(f"\n{ok} checks passed.\nALL TESTS PASSED")
