"""Niche-market arbitrage scanner - PAPER ONLY. Places no orders.

Looks for prices that contradict each other in small, less-watched Polymarket markets:

  1. SUM   - a mutually-exclusive "who wins" event where the YES bids add up to
             more than 100%. Buying NO on every outcome pays (n-1) whatever happens,
             but costs less than that.  Profit per set = sum(bids) - 1.
             (Also flags sum(asks) < 100%, but that one only works if the outcomes
             cover every possibility, so it's marked CHECK.)
  2. LADDER- "X by <date>" markets. An earlier deadline can never be more likely
             than a later one. If early YES bid > late YES ask:
             buy late YES + buy early NO.  Profit per pair >= bid_early - ask_late.

Every candidate is re-checked against the live order book (not cached prices),
and fees are subtracted. Each gap is logged with first_seen / last_seen so you
can measure how long opportunities survive.

Usage:
    py arb.py scan          one scan, opens the results in your browser
    py arb.py run           scan every few minutes until Ctrl+C
    py arb.py report        how many gaps, how big, how long they lasted
    py arb.py ci            (cloud only) one scan, writes index.html
"""
import json
import re
import sqlite3
import statistics
import sys
import time
import html as htmlmod
import os
import webbrowser

import requests

import config

# ---------- settings (niche filter) ----------
NICHE_MAX_VOLUME_24H = 5_000   # USD; skip busy markets where the big bots live
NICHE_MIN_LIQUIDITY = 200      # USD; below this there's nothing to trade
MAX_EVENTS = 4_000             # how many active events to page through
FEE_PER_LEG = 0.0              # set if Polymarket charges a fee on these markets
MIN_PROFIT = 0.01              # ignore gaps under 1 cent per $1 set
MIN_DOLLARS = 0.50             # ignore gaps worth less than this in total (tiny size)
SCAN_EVERY_SECONDS = 300
CLOB_URL = "https://clob.polymarket.com"


# ---------- data ----------
def _j(v, default):
    if isinstance(v, list):
        return v
    try:
        return json.loads(v or "null") or default
    except (ValueError, TypeError):
        return default


def leg_from_market(m: dict) -> dict | None:
    if m.get("closed") or m.get("active") is False or m.get("enableOrderBook") is False:
        return None
    outcomes = _j(m.get("outcomes"), [])
    if [o.lower() for o in outcomes] != ["yes", "no"]:
        return None
    tokens = _j(m.get("clobTokenIds"), [])
    return {
        "id": str(m.get("id")),
        "question": m.get("question", ""),
        "title": m.get("groupItemTitle") or m.get("question", ""),
        "end": (m.get("endDate") or "")[:10],
        "bid": float(m.get("bestBid") or 0),
        "ask": float(m.get("bestAsk") or 0) or 1.0,
        "yes_token": tokens[0] if tokens else None,
        "vol24": float(m.get("volume24hr") or 0),
        "liq": float(m.get("liquidityNum") or m.get("liquidity") or 0),
        "slug": m.get("slug", ""),
    }


def _page_events(extra: dict) -> list[dict]:
    """Page through active events. Polymarket refuses very deep pages (HTTP 422),
    so stop quietly when that happens instead of crashing."""
    out, offset = [], 0
    while offset < MAX_EVENTS:
        try:
            r = requests.get(f"{config.GAMMA_URL}/events", params={
                "active": "true", "closed": "false", "limit": 100, "offset": offset, **extra},
                timeout=30)
        except requests.RequestException as e:
            print(f"[arb] network error at offset {offset}: {e}")
            break
        if r.status_code in (400, 422):
            break  # hit Polymarket's paging limit
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        offset += 100
    return out


def load_events() -> list[dict]:
    # Pass 1: quietest events first (the niche ones we care about).
    # Pass 2: default order, to catch anything pass 1 missed.
    seen, events = set(), []
    for extra in ({"order": "volume24hr", "ascending": "true"}, {}):
        for ev in _page_events(extra):
            if ev.get("id") not in seen:
                seen.add(ev.get("id"))
                events.append(ev)
    return events


def get_book(token_id: str) -> dict | None:
    """Best YES bid/ask and the size available there, from the live order book."""
    try:
        r = requests.get(f"{CLOB_URL}/book", params={"token_id": token_id}, timeout=15)
        r.raise_for_status()
        b = r.json()
    except Exception:
        return None
    bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
    asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
    best_bid = max(bids, default=(0.0, 0.0))
    best_ask = min(asks, default=(1.0, 0.0))
    return {"bid": best_bid[0], "bid_size": best_bid[1], "ask": best_ask[0], "ask_size": best_ask[1]}


def is_niche(legs) -> bool:
    vol = sum(l["vol24"] for l in legs)
    liq = sum(l["liq"] for l in legs)
    return vol <= NICHE_MAX_VOLUME_24H and liq >= NICHE_MIN_LIQUIDITY


def days_left(end: str) -> float:
    try:
        t = time.mktime(time.strptime(end[:10], "%Y-%m-%d"))
        return max((t - time.time()) / 86400, 1.0)
    except (ValueError, TypeError):
        return 365.0


def annualised(profit, cost, days) -> float:
    """Return per year, so a 1% gap locked up for 2 years doesn't look as good as it sounds."""
    return (profit / cost) * 365 / days if cost > 0 else 0.0


EXHAUSTIVE_RE = re.compile(r"^(other|others|any other|field|none|no one|nobody)\b|^[<>≤≥]|\+\s*$|or (more|higher|above|less|lower|below)$", re.I)


def covers_everything(legs) -> bool:
    """True if the outcomes look like they cover every possibility: an 'Other'
    option, or number ranges with open ends like '<17%' and '29%+'."""
    titles = [l["title"].strip() for l in legs]
    if any(re.match(r"^(other|others|any other|field|none|no one|nobody)\b", t, re.I) for t in titles):
        return True
    low = any(re.match(r"^[<≤]|^under |^below |^less than ", t, re.I) for t in titles)
    high = any(re.search(r"\+\s*$|or (more|higher|above)$|^[>≥]|^over |^above ", t, re.I) for t in titles)
    return low and high


# ---------- check 1: outcomes in a "who wins" event ----------
def check_sum(event, legs, book=get_book, complete=True):
    if not event.get("negRisk") or len(legs) < 2:
        return []  # only events Polymarket enforces as mutually exclusive
    n = len(legs)
    days = days_left(event.get("endDate") or max(l["end"] for l in legs))
    # buying YES on everything only works if one listed outcome MUST win
    yes_ok = complete and covers_everything(legs)
    found = []
    # quick screen on cached prices, then confirm with live books
    if sum(l["bid"] for l in legs) - 1 - n * FEE_PER_LEG > MIN_PROFIT or \
       (yes_ok and 1 - sum(l["ask"] for l in legs) - n * FEE_PER_LEG > MIN_PROFIT):
        books = [book(l["yes_token"]) if l["yes_token"] else None for l in legs]
        if any(b is None for b in books):
            return []
        sell = sum(b["bid"] for b in books) - 1 - n * FEE_PER_LEG
        if sell > MIN_PROFIT:
            size = min(b["bid_size"] for b in books)
            cost = n - sum(b["bid"] for b in books)
            found.append({"type": "SUM-NO", "key": f"sum-no:{event.get('id')}",
                          "event": event.get("title", ""), "profit": sell, "size": size,
                          "days": days, "apr": annualised(sell, cost, days),
                          "detail": "Buy NO on every outcome. YES bids add to "
                                    f"{sum(b['bid'] for b in books):.3f} (> 1)",
                          "legs": [(l["title"], round(b["bid"], 3)) for l, b in zip(legs, books)],
                          "url": f"https://polymarket.com/event/{event.get('slug', '')}"})
        buy = 1 - sum(b["ask"] for b in books) - n * FEE_PER_LEG
        if yes_ok and buy > MIN_PROFIT:
            size = min(b["ask_size"] for b in books)
            found.append({"type": "SUM-YES", "key": f"sum-yes:{event.get('id')}",
                          "event": event.get("title", ""), "profit": buy, "size": size,
                          "days": days, "apr": annualised(buy, 1 - buy, days),
                          "detail": "Buy YES on every outcome. Asks add to "
                                    f"{sum(b['ask'] for b in books):.3f} (< 1). Outcomes include "
                                    "'Other' or open-ended ranges - still read the rules.",
                          "legs": [(l["title"], round(b["ask"], 3)) for l, b in zip(legs, books)],
                          "url": f"https://polymarket.com/event/{event.get('slug', '')}"})
    return found


# ---------- check 2: "by <date>" ladders ----------
MONTHS = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)"
DATE_RE = re.compile(rf"\b(by|before|on or before)\s+(the\s+)?(end\s+of\s+)?({MONTHS}\.?\s*\d{{0,2}},?\s*(\d{{4}})?|\d{{4}}|q[1-4]\s*\d{{0,4}})", re.I)


def ladder_key(question: str) -> str | None:
    """'Will X happen by June 30, 2027?' -> 'will x happen by <D>'. None if not a 'by date' market."""
    if not DATE_RE.search(question):
        return None
    if re.search(r"\b(not|no longer|never|fail to|remain|still|stay)\b", question, re.I):
        return None  # 'not by' / 'remain until' run the opposite way - skip them
    k = DATE_RE.sub(r"\1 <D>", question.lower())
    k = re.sub(r"[^a-z0-9<> ]+", " ", k)
    return re.sub(r"\s+", " ", k).strip()


def check_ladders(all_legs, book=get_book):
    groups: dict[str, list] = {}
    for l in all_legs:
        k = ladder_key(l["question"])
        if k and l["end"]:
            groups.setdefault(k, []).append(l)
    found = []
    for k, legs in groups.items():
        legs = sorted(legs, key=lambda l: l["end"])
        if len({l["end"] for l in legs}) < 2 or not is_niche(legs):
            continue
        for i, early in enumerate(legs):
            for late in legs[i + 1:]:
                if late["end"] <= early["end"]:
                    continue
                if early["bid"] - late["ask"] - 2 * FEE_PER_LEG <= MIN_PROFIT:
                    continue
                be, bl = book(early["yes_token"]), book(late["yes_token"])
                if not be or not bl:
                    continue
                profit = be["bid"] - bl["ask"] - 2 * FEE_PER_LEG
                if profit > MIN_PROFIT:
                    days = days_left(late["end"])
                    found.append({
                        "type": "LADDER", "key": f"ladder:{early['id']}:{late['id']}",
                        "event": early["question"], "profit": profit,
                        "days": days, "apr": annualised(profit, bl["ask"] + 1 - be["bid"], days),
                        "size": min(be["bid_size"], bl["ask_size"]),
                        "detail": f"Earlier deadline ({early['end']}) YES bid {be['bid']:.3f} > "
                                  f"later deadline ({late['end']}) YES ask {bl['ask']:.3f}. "
                                  "Buy later YES + buy earlier NO. Check both use the SAME rules.",
                        "legs": [(early["question"], be["bid"]), (late["question"], bl["ask"])],
                        "url": f"https://polymarket.com/market/{early['slug']}"})
    return found


# ---------- logging ----------
class ArbLog:
    def __init__(self, path=None):
        self.db = sqlite3.connect(path or config.DB_PATH)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS arbs (
            key TEXT PRIMARY KEY, type TEXT, event TEXT, detail TEXT, url TEXT,
            first_seen REAL, last_seen REAL, scans INTEGER,
            best_profit REAL, last_profit REAL, last_size REAL, open INTEGER)""")
        self.db.commit()

    def record(self, found, now=None):
        now = now or time.time()
        keys = set()
        for a in found:
            keys.add(a["key"])
            row = self.db.execute("SELECT * FROM arbs WHERE key=?", (a["key"],)).fetchone()
            if row and row["open"]:
                self.db.execute("""UPDATE arbs SET last_seen=?, scans=scans+1, last_profit=?, last_size=?,
                                   best_profit=MAX(best_profit, ?) WHERE key=?""",
                                (now, a["profit"], a["size"], a["profit"], a["key"]))
            else:  # new, or re-opened after closing (log as a fresh gap)
                self.db.execute("INSERT OR REPLACE INTO arbs VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                                (a["key"], a["type"], a["event"], a["detail"], a["url"],
                                 now, now, 1, a["profit"], a["profit"], a["size"]))
        for row in self.db.execute("SELECT key FROM arbs WHERE open=1").fetchall():
            if row["key"] not in keys:
                self.db.execute("UPDATE arbs SET open=0 WHERE key=?", (row["key"],))
        self.db.commit()

    def all(self):
        return self.db.execute("SELECT * FROM arbs ORDER BY first_seen").fetchall()


# ---------- browser page ----------
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket Arb Scan</title>%REFRESH%<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--mute:#667085;--line:#e4e7ec;
--good:#067647;--goodbg:#ecfdf3;--warn:#b54708;--warnbg:#fffaeb;--bad:#b42318;--badbg:#fef3f2;--acc:#1e5eff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--ink:#e8eaf0;--mute:#98a2b3;--line:#262a33;
--goodbg:#0b2a1c;--good:#6ce9a6;--warnbg:#2b1d08;--warn:#fec84b;--badbg:#2d1313;--bad:#fda29b;--acc:#7aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mute);margin:0 0 20px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.stat b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.stat span{color:var(--mute);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:12px;overflow-x:auto}
.top{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.tag{font-size:12px;font-weight:600;padding:2px 8px;border-radius:999px;background:var(--goodbg);color:var(--good)}
.tag.warn{background:var(--warnbg);color:var(--warn)}
.title{font-weight:600;font-size:16px;flex:1;min-width:200px}
.nums{display:flex;gap:18px;flex-wrap:wrap;margin:10px 0;font-variant-numeric:tabular-nums}
.nums div span{display:block;color:var(--mute);font-size:12px}.nums div b{font-size:17px}
.low{color:var(--bad)}.ok{color:var(--good)}
.detail{color:var(--mute);margin:6px 0 8px}
details summary{cursor:pointer;color:var(--acc);font-size:14px}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px}
td{padding:4px 6px;border-top:1px solid var(--line)}td:last-child{text-align:right;font-variant-numeric:tabular-nums}
a{color:var(--acc)}.empty{text-align:center;color:var(--mute);padding:40px}
.note{background:var(--warnbg);color:var(--warn);border-radius:10px;padding:10px 14px;margin-bottom:16px;font-size:14px}
</style></head><body><div class="wrap">%BODY%</div></body></html>"""


def write_page(found, n_events, n_niche, path="arb_report.html", open_it=True, log=None, refresh=False):
    e = htmlmod.escape
    total = sum(a["profit"] * a["size"] for a in found)
    body = [f"<h1>Polymarket arbitrage scan</h1><p class='sub'>Last scan {time.strftime('%d %b %Y, %H:%M UTC', time.gmtime())} &middot; "
            f"paper only, no trades placed</p>",
            "<div class='stats'>"
            f"<div class='stat'><b>{n_events:,}</b><span>events scanned</span></div>"
            f"<div class='stat'><b>{n_niche:,}</b><span>niche events</span></div>"
            f"<div class='stat'><b>{len(found)}</b><span>gaps after fees</span></div>"
            f"<div class='stat'><b>${total:.2f}</b><span>max profit if all were real</span></div></div>",
            "<div class='note'>Before trusting any gap, open the link and check the rules. "
            "Two markets that look the same can resolve differently.</div>"]
    if not found:
        body.append("<div class='card empty'>No gaps this scan. That's normal: niche markets are "
                    "more efficient than they look.</div>")
    for a in sorted(found, key=lambda a: -a["apr"]):
        apr_cls = "low" if a["apr"] < 0.05 else "ok"
        rows = "".join(f"<tr><td>{e(str(n))}</td><td>{p}</td></tr>" for n, p in a["legs"])
        body.append(
            f"<div class='card'><div class='top'><span class='tag{'' if a['type'] == 'SUM-NO' else ' warn'}'>"
            f"{e(a['type'])}</span><span class='title'>{e(a['event'])}</span></div>"
            "<div class='nums'>"
            f"<div><span>profit per $1 set</span><b>{a['profit'] * 100:.1f}c</b></div>"
            f"<div><span>shares available</span><b>{a['size']:.0f}</b></div>"
            f"<div><span>max profit</span><b>${a['profit'] * a['size']:.2f}</b></div>"
            f"<div><span>money locked</span><b>{a['days']:.0f} days</b></div>"
            f"<div><span>per year</span><b class='{apr_cls}'>{a['apr']:.0%}</b></div></div>"
            f"<div class='detail'>{e(a['detail'])}"
            + (" <b class='low'>Worse than a savings account.</b>" if a["apr"] < 0.05 else "")
            + f"</div><details><summary>{len(a['legs'])} outcomes</summary><table>{rows}</table></details>"
            f"<p><a href='{e(a['url'])}' target='_blank'>Open on Polymarket &rarr;</a></p></div>")
    if log is not None:
        body.append(history_html(log))
    page = PAGE.replace("%BODY%", "".join(body))
    page = page.replace("%REFRESH%", '<meta http-equiv="refresh" content="300">' if refresh else "")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    if open_it:
        webbrowser.open("file://" + os.path.abspath(path))
    return path


def history_html(log, limit=60) -> str:
    """Every gap ever logged: when it appeared, how long it lasted. This is the research result."""
    e = htmlmod.escape
    rows = log.all()
    if not rows:
        return ""
    closed = [r for r in rows if not r["open"]]
    mins = [(r["last_seen"] - r["first_seen"]) / 60 for r in closed]
    since = time.strftime("%d %b %Y", time.gmtime(rows[0]["first_seen"]))
    out = [f"<h2 style='margin:32px 0 6px;font-size:18px'>History since {since}</h2>",
           "<div class='stats'>",
           f"<div class='stat'><b>{len(rows)}</b><span>gaps logged in total</span></div>",
           f"<div class='stat'><b>{len(rows) - len(closed)}</b><span>still open</span></div>"]
    if mins:
        out.append(f"<div class='stat'><b>{statistics.median(mins):.0f} min</b>"
                   "<span>median time before a gap closed</span></div>")
    out.append("</div><div class='card'><table><tr><td><b>First seen (UTC)</b></td><td><b>Type</b></td>"
               "<td><b>Market</b></td><td><b>Best</b></td><td><b>Lasted</b></td></tr>")
    for r in list(reversed(rows))[:limit]:
        lasted = (r["last_seen"] - r["first_seen"]) / 60
        status = "open now" if r["open"] else f"{lasted:.0f} min"
        out.append(f"<tr><td>{time.strftime('%d %b %H:%M', time.gmtime(r['first_seen']))}</td>"
                   f"<td>{e(r['type'])}</td><td><a href='{e(r['url'])}' target='_blank'>{e(r['event'][:70])}</a></td>"
                   f"<td>{r['best_profit'] * 100:.1f}c</td><td>{status}</td></tr>")
    out.append("</table></div>")
    return "".join(out)


# ---------- main ----------
def scan(log=None, events=None, book=get_book, open_page=None, page_path="arb_report.html", refresh=False):
    log = log or ArbLog()
    events = load_events() if events is None else events
    all_legs, found, n_niche = [], [], 0
    for ev in events:
        raw = ev.get("markets", [])
        legs = [l for l in (leg_from_market(m) for m in raw) if l]
        all_legs.extend(legs)
        if legs and is_niche(legs):
            n_niche += 1
            found += check_sum(ev, legs, book, complete=len(legs) == len(raw))
    found += check_ladders(all_legs, book)
    found = [a for a in found if a["profit"] * a["size"] >= MIN_DOLLARS]
    log.record(found)
    print(f"[arb] {len(events)} events scanned, {n_niche} niche. {len(found)} gap(s) after fees.")
    if open_page is not None:
        write_page(found, len(events), n_niche, path=page_path, open_it=open_page, log=log, refresh=refresh)
        print(f"[arb] results page: {page_path}")
    for a in sorted(found, key=lambda a: -a["profit"]):
        print(f"\n  [{a['type']}] {a['event'][:90]}")
        print(f"   profit {a['profit'] * 100:.1f}c per $1 set | ~{a['size']:.0f} shares at that price "
              f"(~${a['profit'] * a['size']:.2f} max)")
        print(f"   money locked ~{a['days']:.0f} days -> {a['apr']:.0%} a year"
              + ("  (worse than a savings account!)" if a["apr"] < 0.05 else ""))
        print(f"   {a['detail']}")
        for name, p in a["legs"][:8]:
            print(f"     - {name[:70]}: {p}")
        if len(a["legs"]) > 8:
            print(f"     ... +{len(a['legs']) - 8} more")
        print(f"   {a['url']}")
    return found


def report(log=None):
    rows = (log or ArbLog()).all()
    if not rows:
        print("No gaps logged yet. Run 'py arb.py run' for a few days.")
        return
    closed = [r for r in rows if not r["open"]]
    print(f"Gaps found: {len(rows)}   still open: {len(rows) - len(closed)}")
    for t in sorted({r["type"] for r in rows}):
        sub = [r for r in rows if r["type"] == t]
        print(f"  {t}: {len(sub)}  avg best profit {statistics.mean(r['best_profit'] for r in sub) * 100:.1f}c/$")
    if closed:
        mins = [(r["last_seen"] - r["first_seen"]) / 60 for r in closed]
        print(f"How long closed gaps lasted: median {statistics.median(mins):.0f} min, "
              f"max {max(mins):.0f} min  (0 = seen in one scan only)")
    theo = sum(r["best_profit"] * (r["last_size"] or 0) for r in rows)
    print(f"Theoretical max profit if you'd caught every one at its best: ${theo:.2f}")
    print("(Before trusting this: open a few of the links and check the rules really match.)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "scan":
        scan(open_page=True)
    elif cmd == "run":
        print(f"Arb scanner running every {SCAN_EVERY_SECONDS // 60} min. Ctrl+C to stop.")
        first = True
        while True:
            try:
                scan(open_page=first)  # opens the browser once, then just refresh the tab
                first = False
            except Exception as e:
                print(f"[error] {e}")
            time.sleep(SCAN_EVERY_SECONDS)
    elif cmd == "ci":  # used by the GitHub cloud version: one scan, write index.html, no browser
        scan(open_page=False, page_path="index.html", refresh=True)
    elif cmd == "report":
        report()
    else:
        print(__doc__)
