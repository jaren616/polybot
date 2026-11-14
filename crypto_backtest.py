"""Backtest crypto.py's fair prices on settled 'above' and 'range' markets (BTC/ETH), with several volatility choices.

Usage: python crypto_backtest.py [days]   (default 21; needs internet, takes ~5 minutes)
Compares the model with Polymarket's own price 24/12/6 hours before each close, and with the outcome.
"""
import sys, time, math, json, statistics
import crypto, data

S = crypto.S
now = time.time()
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 21
LEADS = [24, 12, 6]
iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

# 1. resolved events, one day at a time (the API errors on offset + date filters)
evs = []
for d in range(DAYS):
    lo, hi = now - (d + 1) * 86400, now - d * 86400
    for tag in ("crypto", "bitcoin", "ethereum"):
        try:
            b = S.get("https://gamma-api.polymarket.com/events", timeout=30, params={
                "tag_slug": tag, "closed": "true", "limit": 100, "end_date_min": iso(lo), "end_date_max": iso(hi)}).json()
        except Exception:
            continue
        if isinstance(b, list):
            evs += [e for e in b if isinstance(e, dict)]
evs = list({e["id"]: e for e in evs}.values())
mk = []
for e in evs:
    for raw in e.get("markets", []):
        m = data.parse_market(raw)
        if not m or not m.end_ts or m.end_ts > now:
            continue
        try:
            final = [float(x) for x in json.loads(raw.get("outcomePrices"))]
        except Exception:
            continue
        if final[0] not in (0.0, 1.0):
            continue
        spec = crypto.classify(e["title"], m)
        if spec and spec["kind"] in ("above", "range") and spec["coin"] in ("BTC", "ETH"):
            mk.append((m, spec, final[0]))
print(len(evs), "events,", len(mk), "resolved above/range BTC/ETH markets", file=sys.stderr)

# 2. coin history: hourly closes + DVOL
start = now - (DAYS + 9) * 86400
spot_h, dvol_h = {}, {}
for sym in ("BTC", "ETH"):
    spot_h[sym] = {int(c[0]): c[4] for c in crypto.candles(sym, start, now, 3600)}
    dvol_h[sym] = {}
    for a in range(int(start), int(now), 900 * 3600):   # Deribit returns ~1000 points per request
        try:
            r = S.get("https://www.deribit.com/api/v2/public/get_volatility_index_data", timeout=30, params={
                "currency": sym, "resolution": 3600, "start_timestamp": a * 1000,
                "end_timestamp": min(a + 900 * 3600, int(now)) * 1000})
            dvol_h[sym].update({int(x[0] / 1000): x[4] / 100 for x in r.json()["result"]["data"]})
        except Exception:
            pass
    print(sym, len(spot_h[sym]), "hours of price,", len(dvol_h[sym]), "of DVOL", file=sys.stderr)


def at(series, t):
    h = int(t // 3600 * 3600) - 3600   # candle labelled h-1h closed at h <= t (no peeking ahead)
    for back in range(4):
        v = series.get(h - back * 3600)
        if v:
            return v
    return None


# 3. market price history -> model vs market at each lead time
rows = []
for i, (m, spec, outcome) in enumerate(mk):
    try:
        r = S.get("https://clob.polymarket.com/prices-history", timeout=20, params={
            "market": m.yes_token, "startTs": int(m.end_ts - 30 * 3600), "endTs": int(m.end_ts), "fidelity": 60})
        hist = sorted((p["t"], p["p"]) for p in r.json().get("history", []))
    except Exception:
        continue
    sym = spec["coin"]
    for lead in LEADS:
        t = m.end_ts - lead * 3600
        before = [p for ts, p in hist if ts <= t]
        s, v = at(spot_h[sym], t), at(dvol_h[sym], t)
        if not before or not s:
            continue   # (DVOL may be missing: the bot doesn't use it, so keep the row)
        h = int(t // 3600 * 3600) - 3600
        closes = [c for c in (spot_h[sym].get(h - k * 3600) for k in range(168, -1, -1)) if c]

        def rv(hours):
            c = closes[-hours - 1:]
            rr = [math.log(y / x) for x, y in zip(c, c[1:])]
            return statistics.pstdev(rr) * math.sqrt(24 * 365) if len(rr) > 10 else None
        vols = {"dvol": v, "rv24": rv(24), "rv72": rv(72), "rv168": rv(168)}
        T = lead * 3600 / crypto.YEAR
        rows.append({"lead": lead, "mkt": before[-1], "y": outcome, "wkend": time.gmtime(t).tm_wday >= 5, "coin": sym, "end": m.end_ts,
                     "models": {k: crypto.fair(spec, s, x, T) for k, x in vols.items() if x}})
    if i % 250 == 0:
        print(f"{i} / {len(mk)} markets", file=sys.stderr)
if len(sys.argv) > 2:
    json.dump(rows, open(sys.argv[2], "w"))   # optional: save the raw rows for more analysis


def score(R, key, edge=0.04):
    R = [r for r in R if key in r["models"]]
    if not R:
        return 0, 0, 0, 0, 0, 0
    bm = statistics.mean((r["models"][key] - r["y"]) ** 2 for r in R)
    bk = statistics.mean((r["mkt"] - r["y"]) ** 2 for r in R)
    pnl = n = w = 0
    for r in R:
        p = r["models"][key]
        for fp, price, won in ((p, r["mkt"] + 0.01, r["y"] == 1), (1 - p, 1 - r["mkt"] + 0.01, r["y"] == 0)):
            if fp - price >= edge and 0.10 <= price <= 0.90:
                n += 1; w += won; pnl += (1 / price if won else 0) - 1
    return len(R), bm, bk, n, w, (pnl / n if n else 0)


print(f"\n{len(rows)} (market, lead time) points from {len(mk)} markets over {DAYS} days")
for lead in LEADS + ["all"]:
    R = [r for r in rows if (lead == "all" or r["lead"] == lead) and 0.02 < r["mkt"] < 0.98]
    if not R:
        continue
    print(f"--- {lead}{'h' if lead != 'all' else ''} before close ---")
    for key in ("dvol", "rv24", "rv72", "rv168"):
        n_, bm, bk, n, w, ret = score(R, key)
        print(f"  {key:6} n={n_:4} Brier model {bm:.4f} vs market {bk:.4f} | rule {n:3} trades, won {w:3}, return/$ {ret:+.3f}")

print("=== robustness (all lead times, price 0.02-0.98) ===")
R = [r for r in rows if 0.02 < r["mkt"] < 0.98]
months = sorted({time.strftime("%Y-%m", time.gmtime(r["end"])) for r in R})
splits = [(f"month {mo}", [r for r in R if time.strftime("%Y-%m", time.gmtime(r["end"])) == mo]) for mo in months]
splits += [("BTC only", [r for r in R if r["coin"] == "BTC"]), ("ETH only", [r for r in R if r["coin"] == "ETH"]),
           ("weekend", [r for r in R if r["wkend"]]), ("weekday", [r for r in R if not r["wkend"]])]
splits += [(f"weekday {mo}", [r for r in R if not r["wkend"] and time.strftime("%Y-%m", time.gmtime(r["end"])) == mo])
           for mo in months]
for name, sub in splits:
    for key in ("dvol", "rv168"):
        n_, bm, bk, n, w, ret = score(sub, key)
        if n_:
            print(f"  {name:22} {key:6} n={n_:4} Brier {bm:.4f} vs mkt {bk:.4f} | {n:3} trades won {w:3} return/$ {ret:+.3f}")


def trades(sub, key="rv168", edge=0.04):
    """(settle time, profit per $1) for each trade the rule would make."""
    out = []
    for r in sub:
        p = r["models"].get(key)
        if p is None:
            continue
        for fp, price, won in ((p, r["mkt"] + 0.01, r["y"] == 1), (1 - p, 1 - r["mkt"] + 0.01, r["y"] == 0)):
            if fp - price >= edge and 0.10 <= price <= 0.90:
                out.append((r["end"], (1 / price if won else 0) - 1))
    return out


bot = [r for r in R if not r["wkend"]]   # what the bot actually trades: weekdays, 7-day vol
weeks = {}
for t, x in trades(bot):
    weeks.setdefault(time.strftime("%m-%d", time.gmtime(t - time.gmtime(t).tm_wday * 86400)), []).append(x)
print("  bot rule (weekdays, rv168) profit per week ($1 bets):", " ".join(f"{w}:{sum(v):+.1f}" for w, v in sorted(weeks.items())))
print("  winning weeks", sum(sum(v) > 0 for v in weeks.values()), "of", len(weeks))

print("=== baselines (all lead times, price 0.02-0.98) ===")
print(f"  Brier: always 50% 0.2500 | Polymarket {statistics.mean((r['mkt'] - r['y']) ** 2 for r in R):.4f} | "
      f"model rv168 {statistics.mean((r['models']['rv168'] - r['y']) ** 2 for r in R if 'rv168' in r['models']):.4f}")
import random
random.seed(0)


def simple(sub, pick):
    xs = []
    for r in sub:
        side = pick(r)
        price = (r["mkt"] if side == "YES" else 1 - r["mkt"]) + 0.01
        if 0.10 <= price <= 0.90:
            xs.append((1 / price if (r["y"] == 1) == (side == "YES") else 0) - 1)
    return xs


for name, pick in [("buy YES everywhere", lambda r: "YES"), ("buy NO everywhere", lambda r: "NO"),
                   ("random side", lambda r: random.choice(["YES", "NO"])),
                   ("buy the favourite", lambda r: "YES" if r["mkt"] >= 0.5 else "NO")]:
    xs = simple(bot, pick)
    print(f"  weekdays, {name:20} {len(xs):5} trades return/$ {statistics.mean(xs):+.3f}")
bt = [x for _, x in trades(bot)]
print(f"  weekdays, MODEL rule           {len(bt):5} trades return/$ {statistics.mean(bt):+.3f}" if bt else "  no model trades")
if bt:
    sims = sorted(statistics.mean(random.sample(simple(bot, lambda r: random.choice(["YES", "NO"])), len(bt)))
                  for _ in range(1000))
    print(f"  luck check: 1000 sets of {len(bt)} random trades -> median {sims[500]:+.3f}, best 5% {sims[950]:+.3f}; "
          f"model beat {sum(s < statistics.mean(bt) for s in sims) / 10:.1f}% of them")
