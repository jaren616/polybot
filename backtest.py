"""Backtest the momentum and favourites strategies on resolved Polymarket markets.

    py backtest.py                     last 60 days, 300 busiest resolved markets
    py backtest.py --days 90 --markets 500
    py backtest.py --sweep             also try other settings (and check them on unseen data)
    py backtest.py --offline           re-run on already-downloaded data (instant)

How it works
  1. Download resolved Yes/No and two-team markets that closed in the window, busiest first.
  2. Download each market's price history for its last 14 days (cached in backtest_cache/,
     so re-runs are instant).
  3. Replay the strategies on those prices, paying a spread on every buy and sell.
  4. Report win rate, P&L, profit factor, max drawdown - split into the OLDER 70% of markets
     (in-sample) and the NEWER 30% (out-of-sample). Trust the out-of-sample numbers.

Limits (read before trusting it)
  - Price history is one price per 5 minutes, not the real order book: the spread is assumed.
  - Volume is used as a stand-in for liquidity on the day.
  - News and sports-value strategies can't be backtested here (no historical headlines or odds).
"""
import argparse
import html
import json
import os
import statistics
import sys
import time
import webbrowser
from dataclasses import dataclass, field

import requests

import config
import data

CACHE_DIR = "backtest_cache"
HISTORY_DAYS = 14            # price history per market before it closed
FIDELITY_MIN = 5             # one price every 5 minutes
HALF_SPREAD = 0.01           # pay 1c above the price when buying, get 1c below when selling
FEE_RATE = 0.0               # extra fee as a fraction of each trade's cost
STAKE = 5.0
MIN_VOLUME = 25_000          # stand-in for liquidity: skip markets that traded less than this
TRAIN_FRACTION = 0.70


# =====================================================================
#  DATA
# =====================================================================
@dataclass
class Series:
    id: str
    question: str
    outcomes: tuple
    end_sched: float          # scheduled end (known in advance)
    closed_at: float          # when it actually closed
    final: float              # 1.0 = first outcome won, 0.0 = second won
    volume: float
    points: list = field(default_factory=list)   # [(unix time, price of first outcome)]


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def fetch_resolved(days, n, log=print):
    """Resolved two-outcome markets that closed in the last `days`, busiest first."""
    end_min = _iso(time.time() - days * 86400)
    out, offset = [], 0
    while len(out) < n and offset < 3000:
        try:
            r = data.S.get(f"{config.GAMMA_URL}/markets", timeout=30, params={
                "closed": "true", "limit": 100, "offset": offset, "order": "volumeNum",
                "ascending": "false", "end_date_min": end_min, "volume_num_min": MIN_VOLUME})
        except requests.RequestException as e:
            log(f"  market list failed at {offset}: {e}")
            break
        if r.status_code in (400, 422):
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for raw in batch:
            m = data.parse_market(raw)
            if not m or m.yes_final is None or not m.yes_token:
                continue  # unresolved, 50/50 or no order book
            if float(raw.get("volumeNum") or raw.get("volume") or 0) < MIN_VOLUME:
                continue
            closed_at = data.parse_ts(raw.get("closedTime")) or m.end_ts
            if not closed_at or closed_at < time.time() - days * 86400:
                continue
            out.append((m, closed_at, float(raw.get("volumeNum") or raw.get("volume") or 0)))
        offset += 100
    return out[:n]


def fetch_history(token, start, end):
    """Price history in <=14-day chunks (whole-history requests come back empty for
    resolved markets). Falls back to coarser data if fine data is missing."""
    for fidelity in (FIDELITY_MIN, 60, 720):
        pts, t0 = [], start
        while t0 < end:
            t1 = min(t0 + 14 * 86400, end)
            try:
                r = requests.get(f"{config.CLOB_URL}/prices-history", timeout=30, params={
                    "market": token, "startTs": int(t0), "endTs": int(t1), "fidelity": fidelity})
                if r.status_code == 429:  # rate limited: wait, then retry this chunk once
                    time.sleep(3)
                    r = requests.get(f"{config.CLOB_URL}/prices-history", timeout=30, params={
                        "market": token, "startTs": int(t0), "endTs": int(t1), "fidelity": fidelity})
                r.raise_for_status()
                pts += [(float(h["t"]), float(h["p"])) for h in r.json().get("history", [])]
            except Exception:
                pass
            t0 = t1
        if len(pts) >= 10:
            return sorted(set(pts))
    return []


def load_series(days, n, offline=False, log=print) -> list[Series]:
    os.makedirs(CACHE_DIR, exist_ok=True)
    if offline:
        out = []
        for f in os.listdir(CACHE_DIR):
            if f.endswith(".json"):
                with open(os.path.join(CACHE_DIR, f), encoding="utf-8") as fh:
                    d = json.load(fh)
                d["outcomes"] = tuple(d["outcomes"])
                d["points"] = [tuple(p) for p in d["points"]]
                if d["closed_at"] >= time.time() - days * 86400:
                    out.append(Series(**d))
        log(f"Loaded {len(out)} markets from {CACHE_DIR}/")
        return out
    log(f"Finding resolved markets from the last {days} days...")
    markets = fetch_resolved(days, n, log)
    log(f"  {len(markets)} markets. Downloading price histories (cached after the first run)...")
    out = []
    for i, (m, closed_at, vol) in enumerate(markets, 1):
        path = os.path.join(CACHE_DIR, f"{m.id}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            d["outcomes"] = tuple(d["outcomes"])
            d["points"] = [tuple(p) for p in d["points"]]
            out.append(Series(**d))
            continue
        pts = fetch_history(m.yes_token, closed_at - HISTORY_DAYS * 86400, closed_at)
        if len(pts) < 10:
            continue
        s = Series(m.id, m.question, m.outcomes, m.end_ts or closed_at, closed_at, m.yes_final, vol, pts)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(s.__dict__, fh)
        out.append(s)
        if i % 25 == 0:
            log(f"  {i}/{len(markets)}")
    log(f"  {len(out)} markets with usable price history.")
    return out


# =====================================================================
#  SIMULATION
# =====================================================================
@dataclass
class Trade:
    strategy: str
    market: str
    question: str
    side: str
    entry_t: float
    exit_t: float
    entry: float
    exit: float
    pnl: float
    reason: str


def _buy_price(p, side):
    return min((p if side == "YES" else 1 - p) + HALF_SPREAD, 0.999)


def _sell_price(p, side):
    return max((p if side == "YES" else 1 - p) - HALF_SPREAD, 0.0)


def _trade(strategy, s, side, i_entry, entry, exit_t, exit_px, reason):
    shares = STAKE / entry
    pnl = shares * exit_px - STAKE - STAKE * FEE_RATE
    return Trade(strategy, s.id, s.question, side, s.points[i_entry][0], exit_t, entry, exit_px, pnl, reason)


def run_momentum(s: Series, window_min, min_move, tp, sl, max_hours, cooldown_hours, reverse=False):
    """Follow (or with reverse=True, fade) any move of min_move within window_min."""
    trades, pts = [], s.points
    j, i, last_entry = 0, 1, -1e18
    window, final_payout = window_min * 60, s.final
    while i < len(pts):
        t, p = pts[i]
        while j + 1 < i and pts[j + 1][0] <= t - window:
            j += 1
        tj, pj = pts[j]
        if not (t - 1.5 * window <= tj <= t - window) or t - last_entry < cooldown_hours * 3600:
            i += 1
            continue
        move = p - pj
        if abs(move) < min_move or not (0.10 < p < 0.90):
            i += 1
            continue
        side = ("YES" if move > 0 else "NO")
        if reverse:
            side = "NO" if side == "YES" else "YES"
        entry, last_entry = _buy_price(p, side), t
        exit_t = exit_px = reason = None
        k = i + 1
        while k < len(pts):
            tk, pk = pts[k]
            sell = _sell_price(pk, side)
            if sell >= entry + tp:
                reason = "take profit"
            elif sell <= entry - sl:
                reason = "stop loss"
            elif tk - t >= max_hours * 3600:
                reason = "time limit"
            if reason:
                exit_t, exit_px = tk, sell
                break
            k += 1
        if reason is None:  # held until the market resolved
            exit_t, reason = s.closed_at, "resolved"
            exit_px = final_payout if side == "YES" else 1 - final_payout
        trades.append(_trade("momentum" + (" (reversed)" if reverse else ""), s, side, i, entry, exit_t, exit_px, reason))
        i = k + 1
    return trades


def run_favourites(s: Series, lo, hi, max_hours):
    """Buy the first time an outcome costs lo-hi within max_hours of the scheduled end; hold."""
    for i, (t, p) in enumerate(s.points):
        if not (0 < s.end_sched - t <= max_hours * 3600):
            continue
        for side in ("YES", "NO"):
            entry = _buy_price(p, side)
            if lo <= entry <= hi:
                payout = s.final if side == "YES" else 1 - s.final
                return [_trade("favourites", s, side, i, entry, s.closed_at, payout, "resolved")]
    return []


def default_params():
    ex = config.EXIT_RULES.get("momentum", {"take_profit": 0.05, "stop_loss": 0.04, "max_hours": 6})
    return {
        "momentum": dict(window_min=config.MOMENTUM_WINDOW_MINUTES, min_move=config.MOMENTUM_MIN_MOVE,
                         tp=ex["take_profit"], sl=ex["stop_loss"], max_hours=ex["max_hours"],
                         cooldown_hours=config.MOMENTUM_COOLDOWN_HOURS),
        "favourites": dict(lo=config.FAV_MIN_PRICE, hi=config.FAV_MAX_PRICE, max_hours=config.FAV_MAX_HOURS),
    }


def simulate(series, params):
    trades = []
    for s in series:
        trades += run_momentum(s, **params["momentum"])
        trades += run_momentum(s, **params["momentum"], reverse=True)
        trades += run_favourites(s, **params["favourites"])
    return trades


# =====================================================================
#  STATS
# =====================================================================
def stats(trades):
    if not trades:
        return {"n": 0}
    pnl = [t.pnl for t in trades]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x <= 0]
    eq, peak, dd, curve = 0.0, 0.0, 0.0, []
    for t in sorted(trades, key=lambda t: t.exit_t):
        eq += t.pnl
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        curve.append((t.exit_t, eq))
    return {
        "n": len(trades), "win_rate": len(wins) / len(trades), "pnl": sum(pnl),
        "staked": STAKE * len(trades), "roi": sum(pnl) / (STAKE * len(trades)),
        "avg_win": statistics.mean(wins) if wins else 0.0, "avg_loss": statistics.mean(losses) if losses else 0.0,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf"),
        "max_dd": dd, "avg_entry": statistics.mean(t.entry for t in trades), "curve": curve,
        "reasons": {r: sum(1 for t in trades if t.reason == r) for r in sorted({t.reason for t in trades})},
    }


def split(series):
    """Older 70% of markets (by close time) = in-sample, newer 30% = out-of-sample."""
    s = sorted(series, key=lambda x: x.closed_at)
    k = int(len(s) * TRAIN_FRACTION)
    return s[:k], s[k:]


def by_strategy(trades):
    names = ["momentum", "momentum (reversed)", "favourites"]
    return {n: stats([t for t in trades if t.strategy == n]) for n in names}


def calibration(series, hours_before=24):
    """How often did an outcome priced at X actually happen? Price taken `hours_before` the close."""
    buckets = {}
    for s in series:
        target = s.closed_at - hours_before * 3600
        before = [p for t, p in s.points if t <= target]
        if not before:
            continue
        p = before[-1]
        b = min(int(p * 10), 9)
        buckets.setdefault(b, []).append((p, s.final))
    rows = []
    for b in sorted(buckets):
        v = buckets[b]
        rows.append({"range": f"{b * 10}-{b * 10 + 10}c", "n": len(v),
                     "avg_price": statistics.mean(p for p, _ in v), "happened": statistics.mean(f for _, f in v)})
    return rows


def sweep(train, test):
    """Try other momentum and favourites settings on in-sample data; report the best on unseen data."""
    base = default_params()
    results = {"momentum": [], "favourites": []}
    for window in (15, 30, 60):
        for move in (0.04, 0.06, 0.08, 0.10):
            p = dict(base["momentum"], window_min=window, min_move=move)
            tr = [t for s in train for t in run_momentum(s, **p)]
            te = [t for s in test for t in run_momentum(s, **p)]
            results["momentum"].append({"settings": f"{window} min, {move * 100:.0f}+ pts",
                                        "train": stats(tr), "test": stats(te)})
    for lo, hi in ((0.85, 0.90), (0.90, 0.94), (0.94, 0.97), (0.90, 0.97), (0.97, 0.99)):
        p = dict(base["favourites"], lo=lo, hi=hi)
        tr = [t for s in train for t in run_favourites(s, **p)]
        te = [t for s in test for t in run_favourites(s, **p)]
        results["favourites"].append({"settings": f"{lo * 100:.0f}-{hi * 100:.0f}c",
                                      "train": stats(tr), "test": stats(te)})
    return results


# =====================================================================
#  REPORT
# =====================================================================
def _fmt(st, key, kind):
    if not st.get("n"):
        return "–"
    v = st[key]
    if kind == "pct":
        return f"{v:.0%}"
    if kind == "usd":
        return ("+" if v > 0 else "") + f"${v:,.2f}".replace("$-", "-$")
    if kind == "pf":
        return "∞" if v == float("inf") else f"{v:.2f}"
    return str(v)


def verdict(test):
    if not test.get("n") or test["n"] < 20:
        return ("Not enough trades to judge", "mute")
    if test["pnl"] > 0 and test["profit_factor"] > 1.2:
        return ("Made money on unseen data", "pos")
    if test["pnl"] > 0:
        return ("Barely positive: likely noise", "warn")
    return ("Lost money on unseen data", "neg")


def svg_curve(curves, w=640, h=180):
    pts_all = [p for c in curves.values() for p in c]
    if len(pts_all) < 2:
        return "<p class='mute'>Not enough trades to draw.</p>"
    xs, ys = [p[0] for p in pts_all], [p[1] for p in pts_all] + [0]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if y1 - y0 < 1:
        y0, y1 = y0 - 1, y1 + 1
    X = lambda t: 50 + (t - x0) / ((x1 - x0) or 1) * (w - 60)
    Y = lambda v: 10 + (1 - (v - y0) / (y1 - y0)) * (h - 30)
    colors = {"momentum": "var(--acc)", "momentum (reversed)": "var(--mute)", "favourites": "var(--good)"}
    out = [f"<svg viewBox='0 0 {w} {h}' style='width:100%;height:auto' role='img' aria-label='Cumulative P&L'>"]
    for v in (y0, 0, y1):
        out.append(f"<line x1='50' x2='{w - 10}' y1='{Y(v):.1f}' y2='{Y(v):.1f}' stroke='var(--line)'/>"
                   f"<text x='44' y='{Y(v) + 4:.1f}' text-anchor='end'>{'-' if v < 0 else ''}${abs(v):,.0f}</text>")
    for name, c in curves.items():
        if len(c) > 1:
            d = "M" + " L".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in c)
            out.append(f"<path d='{d}' fill='none' stroke='{colors.get(name, 'var(--ink)')}' stroke-width='2'/>")
    out.append("</svg><div class='legend'>" + "".join(
        f"<span><i style='background:{colors.get(n)}'></i>{html.escape(n)}</span>" for n in curves) + "</div>")
    return "".join(out)


def write_report(series, trades_all, train, test, params, sweep_res=None, path="backtest.html"):
    e = html.escape
    tr_ids, te_ids = {s.id for s in train}, {s.id for s in test}
    st_tr = by_strategy([t for t in trades_all if t.market in tr_ids])
    st_te = by_strategy([t for t in trades_all if t.market in te_ids])
    st_all = by_strategy(trades_all)
    span = (time.strftime("%d %b", time.gmtime(min(s.closed_at for s in series))) + " – " +
            time.strftime("%d %b %Y", time.gmtime(max(s.closed_at for s in series)))) if series else "–"
    rows = []
    for name in st_all:
        a, b = st_tr[name], st_te[name]
        v, cls = verdict(b)
        rows.append(f"<tr><td><b>{e(name)}</b></td><td class='r'>{a.get('n', 0)}</td><td class='r'>{_fmt(a, 'win_rate', 'pct')}</td>"
                    f"<td class='r {('pos' if a.get('pnl', 0) > 0 else 'neg') if a.get('n') else ''}'>{_fmt(a, 'pnl', 'usd')}</td>"
                    f"<td class='r'>{b.get('n', 0)}</td><td class='r'>{_fmt(b, 'win_rate', 'pct')}</td>"
                    f"<td class='r {('pos' if b.get('pnl', 0) > 0 else 'neg') if b.get('n') else ''}'>{_fmt(b, 'pnl', 'usd')}</td>"
                    f"<td class='r'>{_fmt(b, 'profit_factor', 'pf')}</td><td class='{cls}'>{v}</td></tr>")
    detail = []
    for name, st in st_all.items():
        if not st.get("n"):
            continue
        detail.append(f"<tr><td><b>{e(name)}</b></td><td class='r'>{st['n']}</td><td class='r'>{st['win_rate']:.0%}</td>"
                      f"<td class='r'>{st['avg_entry']:.2f}</td><td class='r pos'>{_fmt(st, 'avg_win', 'usd')}</td>"
                      f"<td class='r neg'>{_fmt(st, 'avg_loss', 'usd')}</td><td class='r'>{_fmt(st, 'profit_factor', 'pf')}</td>"
                      f"<td class='r'>{st['roi']:+.1%}</td><td class='r {"neg" if st["max_dd"] > 0 else ""}'>{"-$%.2f" % st["max_dd"] if st["max_dd"] > 0 else "$0.00"}</td>"
                      f"<td class='small'>{e(', '.join(f'{k} {v}' for k, v in st['reasons'].items()))}</td></tr>")
    cal = calibration(series)
    cal_rows = "".join(f"<tr><td>{c['range']}</td><td class='r'>{c['n']}</td><td class='r'>{c['avg_price']:.0%}</td>"
                       f"<td class='r'>{c['happened']:.0%}</td><td class='r {'pos' if c['happened'] > c['avg_price'] else 'neg'}'>"
                       f"{(c['happened'] - c['avg_price']) * 100:+.0f} pts</td></tr>" for c in cal)
    sw = ""
    if sweep_res:
        for strat, res in sweep_res.items():
            best = max((r for r in res if r["train"].get("n", 0) >= 20), key=lambda r: r["train"]["pnl"], default=None)
            body = "".join(
                f"<tr{' class=best' if r is best else ''}><td>{e(r['settings'])}{' ← best in-sample' if r is best else ''}</td>"
                f"<td class='r'>{r['train'].get('n', 0)}</td><td class='r'>{_fmt(r['train'], 'pnl', 'usd')}</td>"
                f"<td class='r'>{r['test'].get('n', 0)}</td><td class='r {('pos' if r['test'].get('pnl', 0) > 0 else 'neg') if r['test'].get('n') else ''}'>"
                f"{_fmt(r['test'], 'pnl', 'usd')}</td></tr>" for r in res)
            sw += (f"<h3>{e(strat.title())}</h3><div class='tbl'><table><tr><th>Settings</th><th class='r'>In-sample trades</th>"
                   f"<th class='r'>In-sample P&L</th><th class='r'>Unseen trades</th><th class='r'>Unseen P&L</th></tr>{body}</table></div>")
        sw = (f"<div class='card'><h2>Other settings tried</h2><p class='mute'>Picking the best in-sample row and "
              f"checking it on unseen markets shows whether tuning helps or just fits noise.</p>{sw}</div>")
    m, f = params["momentum"], params["favourites"]
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polybot Backtest</title><style>
:root{{--bg:#f5f6f8;--card:#fff;--ink:#14161b;--mute:#667085;--line:#e4e7ec;--good:#067647;--bad:#c01048;--warn:#b54708;--acc:#1d4ed8;--soft:#f2f4f7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d0f13;--card:#161920;--ink:#e9ebf0;--mute:#98a2b3;--line:#252a33;--good:#47cd89;--bad:#fd6f8e;--warn:#fdb022;--acc:#84adff;--soft:#1d2129}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:20px 16px 60px}}h1{{font-size:20px;margin:0 0 4px}}h2{{font-size:15px;margin:0 0 8px}}h3{{font-size:13px;margin:14px 0 6px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:14px 0;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}th{{font-size:12px;color:var(--mute);font-weight:500;text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}
td{{padding:7px 8px;border-bottom:1px solid var(--line)}}tr:last-child td{{border:0}}.r{{text-align:right}}.small{{font-size:12px;color:var(--mute)}}
.pos{{color:var(--good)}}.neg{{color:var(--bad)}}.warn{{color:var(--warn)}}.mute{{color:var(--mute)}}tr.best td{{background:var(--soft)}}
svg text{{fill:var(--mute);font-size:11px}}.legend{{display:flex;gap:14px;font-size:12px;color:var(--mute);margin-top:4px}}.legend i{{display:inline-block;width:10px;height:3px;margin-right:5px;vertical-align:middle}}
.note{{background:var(--soft);border-radius:10px;padding:10px 14px;font-size:13px}}
</style></head><body><div class="wrap">
<h1>Polybot backtest</h1><p class="mute">{len(series)} resolved markets that closed {span} · ${STAKE:.0f} per trade · {HALF_SPREAD * 100:.0f}c spread paid on every buy and sell · run {time.strftime('%d %b %Y %H:%M UTC', time.gmtime())}</p>
<div class="card"><h2>Did it make money on markets it hadn't seen?</h2>
<p class="mute">Older {TRAIN_FRACTION:.0%} of markets = in-sample, newer {1 - TRAIN_FRACTION:.0%} = unseen. The unseen columns are the honest test. "Momentum (reversed)" bets against every move: if it does as well as momentum, the direction isn't what's making the money.</p>
<div class="tbl"><table><tr><th>Strategy</th><th class="r">In-sample trades</th><th class="r">Win rate</th><th class="r">P&L</th>
<th class="r">Unseen trades</th><th class="r">Win rate</th><th class="r">P&L</th><th class="r">Profit factor</th><th>Verdict</th></tr>{''.join(rows)}</table></div></div>
<div class="card"><h2>Cumulative P&L, all markets</h2>{svg_curve({n: st['curve'] for n, st in st_all.items() if st.get('n')})}</div>
<div class="card"><h2>Details, all markets</h2><div class="tbl"><table><tr><th>Strategy</th><th class="r">Trades</th><th class="r">Win rate</th><th class="r">Avg entry</th>
<th class="r">Avg win</th><th class="r">Avg loss</th><th class="r">Profit factor</th><th class="r">Return on stake</th><th class="r">Max drawdown</th><th>How trades ended</th></tr>{''.join(detail)}</table></div>
<p class="small">Favourites need a win rate above their average entry price to break even (e.g. buying at 94c needs 94%+ wins).</p></div>
<div class="card"><h2>Are Polymarket prices accurate? (24 hours before close)</h2>
<p class="mute">If outcomes priced 90-100c happened more often than their price, buying favourites has an edge. If less often, long shots are underpriced.</p>
<div class="tbl"><table><tr><th>Price range</th><th class="r">Markets</th><th class="r">Average price</th><th class="r">Actually happened</th><th class="r">Gap</th></tr>{cal_rows}</table></div></div>
{sw}
<div class="card"><h2>Settings tested</h2><p class="small">Momentum: {m['min_move'] * 100:.0f}+ point move in {m['window_min']} min, take profit +{m['tp'] * 100:.0f}, stop loss −{m['sl'] * 100:.0f}, max {m['max_hours']}h, cooldown {m['cooldown_hours']}h.
Favourites: {f['lo'] * 100:.0f}-{f['hi'] * 100:.0f}c within {f['max_hours']}h of the scheduled end.</p>
<p class="note">Limits: prices are one point per 5 minutes, not the real order book, so fills are approximate; volume stands in for liquidity; a backtest shows the past, not a promise. News and sports-value strategies aren't included (no historical headlines or bookmaker odds).</p></div>
</div></body></html>"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return path


def print_summary(series, trades, train, test):
    tr_ids, te_ids = {s.id for s in train}, {s.id for s in test}
    st_te = by_strategy([t for t in trades if t.market in te_ids])
    st_tr = by_strategy([t for t in trades if t.market in tr_ids])
    print(f"\n{len(series)} markets  ({len(train)} in-sample, {len(test)} unseen)\n")
    print(f"{'strategy':22} {'in-sample':>22}   {'unseen':>22}   verdict")
    for name in st_te:
        a, b = st_tr[name], st_te[name]
        fa = f"{a.get('n', 0)} trades {_fmt(a, 'pnl', 'usd'):>9}"
        fb = f"{b.get('n', 0)} trades {_fmt(b, 'pnl', 'usd'):>9}"
        print(f"{name:22} {fa:>22}   {fb:>22}   {verdict(b)[0]}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backtest momentum + favourites on resolved Polymarket markets")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--markets", type=int, default=300)
    ap.add_argument("--sweep", action="store_true", help="also try other settings")
    ap.add_argument("--offline", action="store_true", help="use cached data only")
    ap.add_argument("--out", default="backtest.html")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args(argv)
    series = load_series(a.days, a.markets, a.offline)
    if not series:
        print("No market data. Check your internet connection, or run without --offline first.")
        return 1
    params = default_params()
    trades = simulate(series, params)
    train, test = split(series)
    sw = sweep(train, test) if a.sweep else None
    print_summary(series, trades, train, test)
    path = write_report(series, trades, train, test, params, sw, a.out)
    print(f"\nFull report: {path}")
    if not a.no_open:
        webbrowser.open("file://" + os.path.abspath(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
