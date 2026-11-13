"""SQLite storage shared by the engine threads and the dashboard."""
import json
import sqlite3
import threading
import time

import config

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY, ts REAL, kind TEXT, title TEXT, detail TEXT,
    market_id TEXT, url TEXT, data TEXT, action TEXT);
CREATE INDEX IF NOT EXISTS sig_kind ON signals(kind, market_id, ts);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY, ts REAL, mode TEXT, strategy TEXT, market_id TEXT, question TEXT,
    url TEXT, side TEXT, token_id TEXT, price REAL, shares REAL, cost REAL,
    est_prob REAL, market_yes REAL, mark REAL, status TEXT DEFAULT 'open',
    yes_final REAL, pnl REAL, closed_ts REAL, order_id TEXT, signal_id INTEGER);
CREATE TABLE IF NOT EXISTS equity (ts REAL PRIMARY KEY, equity REAL, cash REAL, open_value REAL, realized REAL);
CREATE TABLE IF NOT EXISTS arbs (
    key TEXT PRIMARY KEY, type TEXT, event TEXT, detail TEXT, url TEXT,
    first_seen REAL, last_seen REAL, scans INTEGER, best_profit REAL,
    last_profit REAL, last_size REAL, apr REAL, days REAL, open INTEGER);
CREATE TABLE IF NOT EXISTS prices (market_id TEXT, ts REAL, mid REAL, vol24 REAL);
CREATE INDEX IF NOT EXISTS prices_ts ON prices(ts);
CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY, ts REAL, level TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


def day_start(ts=None) -> float:
    ts = ts or time.time()
    return ts - ts % 86400  # UTC midnight


class DB:
    def __init__(self, path=None):
        self.conn = sqlite3.connect(path or config.DB_PATH, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            for col in ("label TEXT", "exit_reason TEXT"):  # columns added in later versions
                try:
                    self.conn.execute(f"ALTER TABLE positions ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass  # already there
            self.conn.commit()

    # ---------- low level ----------
    def q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def xmany(self, sql, rows):
        with self.lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    # ---------- state / logs ----------
    def get(self, key, default=None):
        r = self.q("SELECT value FROM state WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def set(self, key, value):
        self.x("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def log(self, msg, level="info"):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")
        self.x("INSERT INTO logs (ts, level, msg) VALUES (?,?,?)", (time.time(), level, msg))

    def paused(self) -> bool:
        return bool(self.get("paused", False))

    def reset(self, marker=None):
        """Start the paper account again at BANKROLL. Keeps cached bookmaker odds and
        today's AI call count (so a reset can't blow through free-tier limits)."""
        with self.lock:
            for t in ("positions", "equity", "signals", "arbs", "prices", "logs", "seen"):
                self.conn.execute(f"DELETE FROM {t}")
            self.conn.execute("DELETE FROM state WHERE key NOT LIKE 'ai_calls_%' AND key != 'odds_cache'")
            self.conn.commit()
        if marker is not None:
            self.set("reset_marker", marker)
        self.log(f"Paper account reset to ${config.BANKROLL:,.0f}")

    # ---------- news dedupe ----------
    def seen(self, item_id) -> bool:
        if self.q("SELECT 1 FROM seen WHERE id=?", (item_id,)):
            return True
        self.x("INSERT OR IGNORE INTO seen VALUES (?,?)", (item_id, time.time()))
        return False

    # ---------- signals ----------
    def add_signal(self, kind, title, detail, market_id=None, url=None, data=None, action=""):
        return self.x("INSERT INTO signals (ts,kind,title,detail,market_id,url,data,action) VALUES (?,?,?,?,?,?,?,?)",
                      (time.time(), kind, title, detail, market_id, url, json.dumps(data or {}), action))

    def set_signal_action(self, sid, action):
        self.x("UPDATE signals SET action=? WHERE id=?", (action, sid))

    def recent_signal(self, kind, market_id, since) -> bool:
        return bool(self.q("SELECT 1 FROM signals WHERE kind=? AND market_id=? AND ts>=?", (kind, market_id, since)))

    # ---------- positions ----------
    def open_positions(self):
        return self.q("SELECT * FROM positions WHERE status='open' ORDER BY ts")

    def has_open(self, market_id) -> bool:
        return bool(self.q("SELECT 1 FROM positions WHERE status='open' AND market_id=?", (market_id,)))

    def open_count(self, strategy) -> int:
        return self.q("SELECT COUNT(*) n FROM positions WHERE status='open' AND strategy=?", (strategy,))[0]["n"]

    def trades_today(self) -> int:
        return self.q("SELECT COUNT(*) n FROM positions WHERE ts>=?", (day_start(),))[0]["n"]

    def add_position(self, **p):
        cols = ",".join(p)
        return self.x(f"INSERT INTO positions ({cols}) VALUES ({','.join('?' * len(p))})", tuple(p.values()))

    def update_mark(self, pid, mark):
        self.x("UPDATE positions SET mark=? WHERE id=?", (mark, pid))

    def close_position(self, pid, yes_final):
        p = self.q("SELECT * FROM positions WHERE id=?", (pid,))[0]
        payout_per_share = yes_final if p["side"] == "YES" else 1 - yes_final
        pnl = p["shares"] * payout_per_share - p["cost"]
        self.x("UPDATE positions SET status='closed', yes_final=?, pnl=?, closed_ts=?, mark=? WHERE id=?",
               (yes_final, pnl, time.time(), payout_per_share, pid))
        return pnl

    def close_position_at(self, pid, sell_price, reason):
        """Sell before resolution (paper): shares x sell price - cost."""
        p = self.q("SELECT * FROM positions WHERE id=?", (pid,))[0]
        pnl = p["shares"] * sell_price - p["cost"]
        self.x("UPDATE positions SET status='closed', pnl=?, closed_ts=?, mark=?, exit_reason=? WHERE id=?",
               (pnl, time.time(), sell_price, reason, pid))
        return pnl

    def strategy_stats(self):
        rows = {}
        for p in self.q("SELECT * FROM positions"):
            r = rows.setdefault(p["strategy"], {"strategy": p["strategy"], "open": 0, "closed": 0, "wins": 0,
                                                "realized": 0.0, "unrealized": 0.0, "staked": 0.0})
            r["staked"] += p["cost"]
            if p["status"] == "open":
                r["open"] += 1
                r["unrealized"] += p["shares"] * (p["mark"] if p["mark"] is not None else p["price"]) - p["cost"]
            else:
                r["closed"] += 1
                r["wins"] += p["pnl"] > 0
                r["realized"] += p["pnl"]
        return sorted(rows.values(), key=lambda r: -(r["realized"] + r["unrealized"]))

    # ---------- money ----------
    def money(self):
        realized = self.q("SELECT COALESCE(SUM(pnl),0) s FROM positions WHERE status='closed'")[0]["s"]
        opens = self.open_positions()
        open_cost = sum(p["cost"] for p in opens)
        open_value = sum(p["shares"] * (p["mark"] if p["mark"] is not None else p["price"]) for p in opens)
        cash = config.BANKROLL + realized - open_cost
        return {"cash": cash, "open_cost": open_cost, "open_value": open_value,
                "realized": realized, "equity": cash + open_value}

    def snapshot_equity(self):
        m = self.money()
        self.x("INSERT OR REPLACE INTO equity VALUES (?,?,?,?,?)",
               (time.time(), m["equity"], m["cash"], m["open_value"], m["realized"]))
        return m

    def equity_at(self, ts):
        r = self.q("SELECT equity FROM equity WHERE ts<=? ORDER BY ts DESC LIMIT 1", (ts,))
        return r[0]["equity"] if r else config.BANKROLL

    # ---------- arbs ----------
    def record_arbs(self, found):
        now = time.time()
        keys = {a["key"] for a in found}
        for a in found:
            row = self.q("SELECT open FROM arbs WHERE key=?", (a["key"],))
            if row and row[0]["open"]:
                self.x("""UPDATE arbs SET last_seen=?, scans=scans+1, last_profit=?, last_size=?, apr=?,
                          best_profit=MAX(best_profit, ?) WHERE key=?""",
                       (now, a["profit"], a["size"], a["apr"], a["profit"], a["key"]))
            else:
                self.x("INSERT OR REPLACE INTO arbs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                       (a["key"], a["type"], a["event"], a["detail"], a["url"], now, now, 1,
                        a["profit"], a["profit"], a["size"], a["apr"], a["days"]))
        for r in self.q("SELECT key FROM arbs WHERE open=1"):
            if r["key"] not in keys:
                self.x("UPDATE arbs SET open=0 WHERE key=?", (r["key"],))
        return keys

    # ---------- prices (for movers) ----------
    def add_prices(self, rows, ts):
        self.xmany("INSERT INTO prices VALUES (?,?,?,?)", [(mid, ts, m, v) for mid, m, v in rows])
        self.x("DELETE FROM prices WHERE ts<?", (ts - config.PRICE_HISTORY_HOURS * 3600,))

    def prices_at_or_before(self, ts):
        r = self.q("SELECT MAX(ts) t FROM prices WHERE ts<=?", (ts,))
        t = r[0]["t"] if r else None
        if t is None:
            return None, {}
        return t, {row["market_id"]: (row["mid"], row["vol24"])
                   for row in self.q("SELECT market_id, mid, vol24 FROM prices WHERE ts=?", (t,))}
