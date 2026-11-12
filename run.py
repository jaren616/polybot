"""Start the bot and its dashboard.

    py run.py            start everything, open the dashboard in your browser
    py run.py once       one pass of every strategy, then exit (good for testing)
    py run.py resume     clear a pause (e.g. after the daily loss limit) without the dashboard
    py run.py cloud      (GitHub only) one pass of everything + write site/index.html
"""
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


def cloud(db):
    """One pass on GitHub's servers. Paper only: GitHub's servers are in the US, where
    Polymarket trading isn't allowed, and a private key must never go on GitHub."""
    if config.MODE != "paper":
        print("Cloud mode is paper-only. Refusing to run.")
        sys.exit(1)
    paused = os.getenv("POLYBOT_PAUSED", "").strip().lower()
    if paused in ("true", "false"):
        db.set("paused", paused == "true")
    Engine(db, Broker(db)).run_once()
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
