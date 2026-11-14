"""Start the bot and its dashboard.

    py run.py            start everything, open the dashboard in your browser
    py run.py once       one pass of every strategy, then exit (good for testing)
    py run.py resume     clear a pause (e.g. after the daily loss limit) without the dashboard
    py run.py cloud      (GitHub only) one pass of everything + write site/index.html
    py run.py reset      wipe the paper account and start again at BANKROLL ($1,000)
                         (on GitHub: change the text in the RESET file instead)
"""
import json
import os
import sys
import threading
import time
import webbrowser

import config
from broker import Broker, LiveTrader
from dashboard import serve
from db import DB
from engine import Engine


def make_broker(db):
    if config.MODE == "live":
        try:
            return Broker(db, LiveTrader(db))
        except RuntimeError as e:
            print(f"\n*** {e}\n*** Not starting. Fix the above, or run in paper mode.\n")
            sys.exit(1)
    return Broker(db)


def run_summary(db, since):
    """Print what happened this run, so the GitHub log explains trades (or the lack of them)."""
    import ai
    rows = db.q("SELECT kind, action, data, title FROM signals WHERE ts>=?", (since,))
    print(f"\n===== RUN SUMMARY =====")
    print(f"AI: {', '.join(ai.available()) or 'none'} | AI checks today: "
          f"{db.get('ai_calls_' + time.strftime('%Y%m%d', time.gmtime()), 0)}/{ai.daily_limit()}")
    print(f"Markets loaded for news matching: {db.get('markets_loaded')} | news items seen so far: "
          f"{db.q('SELECT COUNT(*) n FROM seen')[0]['n']}")
    kinds = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print("Signals this run:", kinds or "none")
    news = [r for r in rows if r["kind"] == "news"]
    reasons = {}
    for r in news:
        a = r["action"] or "?"
        key = a.split(" @")[0] if a.startswith(("paper", "live")) else a.split(" (")[0]
        reasons[key] = reasons.get(key, 0) + 1
    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  news -> {k}: {v}")
    near = []
    for r in news:
        d = json.loads(r["data"] or "{}")
        if d.get("p_yes") is not None:
            gap = max(d["p_yes"] - d.get("ask", 1), d.get("bid", 0) - d["p_yes"])
            near.append((gap, d, r["title"]))
    near.sort(key=lambda x: -x[0])
    if near:
        print("Closest to a trade (AI estimate vs price):")
        for gap, d, title in near[:5]:
            print(f"  {gap * 100:+5.1f} pts | AI {d['p_yes']:.2f} conf {d.get('conf')} | bid {d.get('bid')} ask {d.get('ask')}"
                  f" | relevant={d.get('relevant', '?')} priced_in={d.get('priced_in')} | {title[:70]}")
    m = db.money()
    print(f"Equity ${m['equity']:.2f} | open positions {len(db.open_positions())}")
    print("=======================\n")


def cloud(db):
    """One pass on GitHub's servers. Paper only: GitHub's servers are in the US, where
    Polymarket trading isn't allowed, and a private key must never go on GitHub."""
    if config.MODE != "paper":
        print("Cloud mode is paper-only. Refusing to run.")
        sys.exit(1)
    # Changing the RESET file in the repository wipes the paper account on the next run.
    if os.path.exists("RESET"):
        with open("RESET", encoding="utf-8") as f:
            marker = f.read().strip()
        if marker and marker != db.get("reset_marker"):
            db.reset(marker)
    paused = os.getenv("POLYBOT_PAUSED", "").strip().lower()
    if paused in ("true", "false"):
        db.set("paused", paused == "true")
    started = time.time()
    engine = Engine(db, Broker(db))
    engine.run_once()
    # Keep working for a while instead of one quick pass (GitHub only starts runs every
    # 30-60 min, so a single pass would miss most news). Each strategy runs on its own timer.
    minutes = float(os.getenv("POLYBOT_LOOP_MINUTES", "0"))
    end = time.time() + minutes * 60
    while time.time() < end:
        time.sleep(15)
        engine.fast_tick()
        engine.slow_tick()
    try:
        run_summary(db, started)
    except Exception as e:
        print("summary failed:", e)
    from dashboard import export_static
    print("Dashboard written to", export_static(db))
    with db.lock:  # fold the write-ahead log into the main file before GitHub saves it
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if time.gmtime().tm_hour == 3 and time.gmtime().tm_min < 15:
            db.conn.execute("VACUUM")  # shrink the file once a day
    db.conn.close()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    db = DB()
    if cmd == "resume":
        db.set("paused", False)
        print("Resumed.")
        return
    if cmd == "cloud":
        return cloud(db)
    if cmd == "reset":
        if input(f"Wipe all paper trades and start again at ${config.BANKROLL:,.0f}? Type yes: ").strip().lower() == "yes":
            db.reset()
            print("Done.")
        return
    broker = make_broker(db)
    engine = Engine(db, broker)
    if cmd == "once":
        engine.run_once()
        m = db.money()
        print(f"\nDone. Equity ${m['equity']:.2f}. Start the full bot with: py run.py")
        return
    srv = serve(db)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://localhost:{config.DASHBOARD_PORT}"
    print(f"\nPolybot running in {broker.mode.upper()} mode. Dashboard: {url}")
    print("Leave this window open. Press Ctrl+C to stop.\n")
    engine.start()
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        engine.stop.set()
        srv.shutdown()
        print("Stopped.")


if __name__ == "__main__":
    main()
