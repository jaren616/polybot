"""Runs the strategies on their schedules in two background threads:
  fast thread: news (every minute), price marks, settlement, live access checks
  slow thread: arbitrage + price movers (loads thousands of events, ~1-2 min)"""
import threading
import time
import traceback

import config
import strategies


class Engine:
    def __init__(self, db, broker):
        self.db, self.broker = db, broker
        self.news = strategies.NewsStrategy(db, broker)
        self.slow = strategies.SlowScan(db)
        self.sports = strategies.SportsStrategy(db, broker)
        self.fast = strategies.FastScan(db, broker)
        self.crypto = strategies.CryptoStrategy(db, broker)
        self.stop = threading.Event()
        self.next_run = {}

    def _due(self, name, every):
        now = time.time()
        if now >= self.next_run.get(name, 0):
            self.next_run[name] = now + every
            return True
        return False

    def _safe(self, name, fn):
        try:
            fn()
        except Exception as e:
            self.db.log(f"{name} error: {e}", "error")
            traceback.print_exc()

    def fast_tick(self):
        if config.ENABLE_NEWS and self._due("news", config.NEWS_EVERY):
            self._safe("news", self.news.run)
        if (config.ENABLE_MOMENTUM or config.ENABLE_FAVOURITES) and self._due("fast", config.FAST_SCAN_EVERY):
            self._safe("momentum/favourites", self.fast.run)
        if self._due("mark", config.MARK_EVERY):
            self._safe("mark", lambda: strategies.mark_positions(self.db))
        if self._due("settle", config.SETTLE_EVERY):
            self._safe("settle", lambda: strategies.settle_positions(self.db))
        if self.broker.live and self._due("geo", config.GEOBLOCK_CHECK_EVERY):
            def geo():
                try:
                    self.broker.live.check_access()
                except RuntimeError as e:
                    self.db.set("paused", True)
                    self.db.log(f"Paused: {e}", "error")
            self._safe("geo", geo)

    def slow_tick(self):
        if (config.ENABLE_ARB or config.ENABLE_MOVERS or config.ENABLE_SPORTS) \
                and self._due("slow", config.SLOW_SCAN_EVERY):
            self._safe("arb/movers", self.slow.run)
            if config.ENABLE_SPORTS and self.slow.events:
                self._safe("sports", lambda: self.sports.run(self.slow.events))
        if config.ENABLE_CRYPTO and self._due("crypto", config.CRYPTO_EVERY_MINUTES * 60):
            self._safe("crypto", self.crypto.run)

    def _loop(self, tick):
        while not self.stop.is_set():
            tick()
            self.stop.wait(3)

    def start(self):
        self.db.set("mode", self.broker.mode)
        self.db.set("started", time.time())
        self.db.log(f"Engine started in {self.broker.mode.upper()} mode")
        for t in (self.fast_tick, self.slow_tick):
            threading.Thread(target=self._loop, args=(t,), daemon=True).start()

    def run_once(self):
        """One pass of everything (for testing)."""
        self.db.set("mode", self.broker.mode)
        self.fast_tick()
        self.slow_tick()
        self._safe("mark", lambda: strategies.mark_positions(self.db))
