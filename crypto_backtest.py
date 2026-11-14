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
    r = S.get("https://www.deribit.com/api/v2/public/get_volatility_index_data", timeout=30, params={
        "currency": sym, "resolution": 3600, "start_timestamp": int(start * 1000), "end_timestamp": int(now * 1000)})
    dvol_h[sym] = {int(x[0] / 1000): x[4] / 100 for x in r.json()["result"]["data"]}


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
        if not before or not s or not v:
            continue
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


def score(R, key, edge=0.04):
    R = [r for r in R if key in r["models"]]
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
mid = sorted(r["end"] for r in R)[len(R) // 2]
for name, sub in [("first half of period", [r for r in R if r["end"] < mid]), ("second half", [r for r in R if r["end"] >= mid]),
                  ("BTC only", [r for r in R if r["coin"] == "BTC"]), ("ETH only", [r for r in R if r["coin"] == "ETH"]),
                  ("weekend", [r for r in R if r["wkend"]]), ("weekday", [r for r in R if not r["wkend"]])]:
    for key in ("dvol", "rv168"):
        n_, bm, bk, n, w, ret = score(sub, key)
        print(f"  {name:22} {key:6} n={n_:4} Brier {bm:.4f} vs mkt {bk:.4f} | {n:3} trades won {w:3} return/$ {ret:+.3f}")
days = {}
for r in R:
    p = r["models"].get("rv168")
    if p is None: continue
    for fp, price, won in ((p, r["mkt"] + 0.01, r["y"] == 1), (1 - p, 1 - r["mkt"] + 0.01, r["y"] == 0)):
        if fp - price >= 0.04 and 0.10 <= price <= 0.90:
            d = time.strftime("%m-%d", time.gmtime(r["end"])); days.setdefault(d, []).append((1 / price if won else 0) - 1)
print("  rv168 profit by settle day:", " ".join(f"{d}:{sum(v):+.1f}" for d, v in sorted(days.items())))
print("  winning days", sum(sum(v) > 0 for v in days.values()), "of", len(days))
