"""Offline tests for backtest.py using made-up price paths with known answers.
Run:  py test_backtest.py   -> ALL BACKTEST TESTS PASSED"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt
import config

ok = 0


def check(c, msg):
    global ok
    assert c, msg
    ok += 1


T0 = 1_790_000_000
H = 3600


def series(id, prices, final, step=300, end_after=None, start=T0):
    pts = [(start + i * step, p) for i, p in enumerate(prices)]
    closed = pts[-1][0] + step
    return bt.Series(id, f"Market {id}?", ("Yes", "No"), end_after or closed, closed, final, 100000, pts)


P = bt.default_params()
mom = dict(P["momentum"], window_min=15, min_move=0.06, tp=0.05, sl=0.04, max_hours=6, cooldown_hours=3)

# 1. Take profit: flat 0.40, jumps to 0.47 (move 7 pts in 15 min), then rises to 0.55
s = series("tp", [0.40] * 6 + [0.47, 0.50, 0.53, 0.55, 0.56] + [0.56] * 5, final=1.0)
tr = bt.run_momentum(s, **mom)
check(len(tr) == 1 and tr[0].side == "YES" and tr[0].reason == "take profit", f"take profit {tr}")
check(abs(tr[0].entry - 0.48) < 1e-9 and abs(tr[0].exit - 0.54) < 1e-9, "entry pays spread; first sell price >= entry+5 is 0.55-1c")
check(abs(tr[0].pnl - (5 / 0.48 * 0.54 - 5)) < 1e-9, "P&L = shares x exit - stake")

# 2. Stop loss on a fall after a NO entry (price drops = buy NO)
s = series("sl", [0.60] * 6 + [0.52, 0.55, 0.58, 0.62, 0.64] + [0.64] * 3, final=0.0)
tr = bt.run_momentum(s, **mom)
check(len(tr) == 1 and tr[0].side == "NO" and tr[0].reason == "stop loss", f"stop loss {tr}")

# 3. Time limit: jump then flat for 7 hours
s = series("tl", [0.30] * 6 + [0.37] + [0.37] * 90, final=1.0)
tr = bt.run_momentum(s, **mom)
check(len(tr) == 1 and tr[0].reason == "time limit" and abs(tr[0].exit_t - tr[0].entry_t - 6 * H) < 301, "time limit")

# 4. Held to resolution when the market closes first
s = series("rs", [0.30] * 6 + [0.37, 0.38, 0.39], final=1.0)
tr = bt.run_momentum(s, **mom)
check(len(tr) == 1 and tr[0].reason == "resolved" and tr[0].exit == 1.0, "resolved -> pays 1")

# 5. Small moves and extreme prices never trade; reversed bets the other way
check(not bt.run_momentum(series("sm", [0.40, 0.41, 0.42, 0.43, 0.44, 0.45, 0.46, 0.45] * 3, 1.0), **mom), "slow drift ignored")
check(not bt.run_momentum(series("ex", [0.88] * 6 + [0.95] * 6, 1.0), **mom), "no momentum above 90c")
rv = bt.run_momentum(series("tp", [0.40] * 6 + [0.47, 0.50, 0.53, 0.55, 0.56] + [0.56] * 5, final=1.0), **mom, reverse=True)
check(rv and rv[0].side == "NO" and rv[0].strategy == "momentum (reversed)", "reversed fades the move")

# 6. Cooldown: two jumps 1 hour apart -> one trade
s = series("cd", [0.40] * 4 + [0.47] + [0.47] * 3 + [0.54] + [0.54] * 15, final=1.0)
check(len(bt.run_momentum(s, **dict(mom, tp=0.5, sl=0.5, max_hours=0.5))) == 1, "3-hour cooldown")

# 7. Favourites: only inside the last 48h and in the price band; payout on resolution
fav = dict(lo=0.90, hi=0.97, max_hours=48)
s = series("fv", [0.80] * 10 + [0.93] * 20, final=1.0, step=4 * H)  # 120h long; 0.93 + 1c = 0.94 entry
tr = bt.run_favourites(s, **fav)
check(len(tr) == 1 and tr[0].side == "YES" and abs(tr[0].entry - 0.94) < 1e-9, "favourite bought at 94c")
check(s.end_sched - tr[0].entry_t <= 48 * H, "only within 48h of the scheduled end")
check(abs(tr[0].pnl - (5 / 0.94 - 5)) < 1e-9, "favourite win P&L")
s = series("fn", [0.05] * 30, final=1.0, step=4 * H)  # NO at 96c, but YES happened -> lose $5
tr = bt.run_favourites(s, **fav)
check(len(tr) == 1 and tr[0].side == "NO" and abs(tr[0].pnl + 5) < 1e-9, "favourite loss = whole stake")
check(not bt.run_favourites(series("nb", [0.50] * 30, 1.0, step=4 * H), **fav), "no favourite at 50c")

# 8. Stats, split, calibration, sweep, report
many = [series(f"m{i}", [0.40] * 6 + [0.47, 0.50, 0.53, 0.55, 0.56] + [0.56] * 5, final=1.0, start=T0 + i * 86400)
        for i in range(30)]
trades = bt.simulate(many, P)
st = bt.stats([t for t in trades if t.strategy == "momentum"])
check(st["n"] == 30 and st["win_rate"] == 1.0 and st["max_dd"] == 0 and st["profit_factor"] == float("inf"), "stats")
lose = bt.stats([bt.Trade("x", "a", "q", "YES", 0, 1, 0.5, 0, -5, "r"), bt.Trade("x", "b", "q", "YES", 0, 2, 0.5, 1, 5, "r"),
                 bt.Trade("x", "c", "q", "YES", 0, 3, 0.5, 0, -5, "r")])
check(lose["max_dd"] == 5 and abs(lose["profit_factor"] - 0.5) < 1e-9, "drawdown + profit factor")
tr_s, te_s = bt.split(many)
check(len(tr_s) == 21 and len(te_s) == 9 and max(s.closed_at for s in tr_s) < min(s.closed_at for s in te_s), "time-ordered split")
cal = bt.calibration([series(f"c{i}", [0.92] * 400, final=1.0 if i < 9 else 0.0) for i in range(10)])
check(cal and cal[-1]["n"] == 10 and abs(cal[-1]["happened"] - 0.9) < 1e-9, "calibration")
sw = bt.sweep(tr_s, te_s)
check(len(sw["momentum"]) == 12 and len(sw["favourites"]) == 5, "sweep grid")
out = os.path.join(tempfile.mkdtemp(), "bt.html")
bt.write_report(many, trades, tr_s, te_s, P, sw, out)
page = open(out, encoding="utf-8").read()
check("Polybot backtest" in page and "Other settings tried" in page and "<svg" in page, "HTML report")
check(bt.verdict(bt.stats([]))[0] == "Not enough trades to judge", "verdict needs 20+ trades")

# 9. Download + cache with a fake Polymarket
d = tempfile.mkdtemp()
cwd = os.getcwd()
os.chdir(d)
mk = lambda i, vol=50000, final="1": {"id": i, "question": f"Q{i}?", "outcomes": json.dumps(["Yes", "No"]),
    "outcomePrices": json.dumps([final, str(1 - float(final))]), "closed": True, "clobTokenIds": json.dumps([f"t{i}", f"n{i}"]),
    "endDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400)), "volumeNum": vol, "slug": f"s{i}"}
calls = {"hist": 0, "list": 0}
class R:
    def __init__(s, body, code=200): s.body, s.status_code = body, code
    def json(s): return s.body
    def raise_for_status(s): pass
def fake_list(url, timeout, params):
    calls["list"] += 1
    if params["offset"] > 0:
        return R([])
    return R([mk(1), mk(2), mk(3, vol=10), mk(4, final="0.5")])
def fake_hist(url, timeout, params):
    calls["hist"] += 1
    start = params["startTs"]
    return R({"history": [{"t": start + k * 300, "p": 0.5} for k in range(50)]})
bt.data.S.get = fake_list
bt.requests.get = fake_hist
ser = bt.load_series(60, 10, log=lambda *a: None)
check(sorted(s.id for s in ser) == ["1", "2"], "skips low volume and unresolved 50/50 markets")
n1 = calls["hist"]
ser2 = bt.load_series(60, 10, log=lambda *a: None)
check(calls["hist"] == n1 and len(ser2) == 2, "second run uses the cache")
off = bt.load_series(60, 10, offline=True, log=lambda *a: None)
check(len(off) == 2 and isinstance(off[0].points[0], tuple), "offline mode")
rc = bt.main(["--offline", "--no-open", "--out", "r.html"])
check(rc == 0 and os.path.exists("r.html"), "command line runs end to end")
os.chdir(cwd)
shutil.rmtree(d)

print(f"\n{ok} checks passed.\nALL BACKTEST TESTS PASSED")
