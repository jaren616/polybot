"""Risk manager + broker. Every trade goes through the same risk checks in paper and live,
so paper results show what live would really have done."""
import math
import os
import time

import config
import data
from db import day_start


# =====================================================================
#  RISK
# =====================================================================
def kelly_stake(est_prob, side, price, bankroll) -> float:
    """Fractional Kelly. Buying a contract at `price` that pays 1 with probability q:
    optimal fraction f* = (q - price) / (1 - price). We bet KELLY_FRACTION of that."""
    q = est_prob if side == "YES" else 1 - est_prob
    if not (0 < price < 1) or q <= price:
        return 0.0
    f = (q - price) / (1 - price)
    stake = min(bankroll * config.KELLY_FRACTION * f, config.MAX_STAKE)
    return round(stake, 2) if stake >= config.MIN_STAKE else 0.0


class Risk:
    def __init__(self, db):
        self.db = db

    def daily_pnl(self) -> float:
        return self.db.money()["equity"] - self.db.equity_at(day_start())

    def check(self, market_id, price, stake) -> str | None:
        """Return None if OK, otherwise the reason the trade is refused."""
        if self.db.paused():
            return "bot is paused"
        if self.daily_pnl() <= -config.DAILY_LOSS_LIMIT:
            self.db.set("paused", True)
            self.db.log(f"Daily loss limit (${config.DAILY_LOSS_LIMIT:.0f}) hit - bot paused itself", "warn")
            return "daily loss limit hit"
        if not (config.MIN_PRICE <= price <= config.MAX_PRICE):
            return f"price {price:.2f} outside {config.MIN_PRICE}-{config.MAX_PRICE}"
        if self.db.has_open(market_id):
            return "already holding this market"
        opens = self.db.open_positions()
        if len(opens) >= config.MAX_OPEN_POSITIONS:
            return "max open positions"
        if sum(p["cost"] for p in opens) + stake > config.MAX_EXPOSURE:
            return "max exposure"
        if self.db.trades_today() >= config.MAX_TRADES_PER_DAY:
            return "max trades today"
        if stake > self.db.money()["cash"]:
            return "not enough cash"
        return None


# =====================================================================
#  LIVE TRADING (only created when MODE == "live" and every check passes)
# =====================================================================
class LiveTrader:
    def __init__(self, db):
        self.db = db
        problems = []
        if config.MODE != "live":
            problems.append("POLYBOT_MODE is not 'live'")
        if not config.POLY_PRIVATE_KEY:
            problems.append("POLY_PRIVATE_KEY not set")
        if not config.POLY_FUNDER:
            problems.append("POLY_FUNDER not set")
        if os.getenv("POLYBOT_LIVE_CONFIRM") != config.LIVE_CONFIRM_PHRASE:
            problems.append(f'POLYBOT_LIVE_CONFIRM must be exactly "{config.LIVE_CONFIRM_PHRASE}"')
        if problems:
            raise RuntimeError("Live trading not enabled: " + "; ".join(problems))
        self.check_access()
        try:
            from py_clob_client.client import ClobClient
        except ImportError:
            raise RuntimeError("Live trading needs: py -m pip install -r requirements-live.txt")
        self.client = ClobClient(config.CLOB_URL, key=config.POLY_PRIVATE_KEY, chain_id=137,
                                 signature_type=config.POLY_SIGNATURE_TYPE, funder=config.POLY_FUNDER)
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
        db.log("LIVE trading connected to Polymarket", "warn")

    def check_access(self):
        g = data.geoblock()
        if g is None:
            raise RuntimeError("Couldn't confirm with Polymarket that trading is allowed from here")
        if g.get("blocked"):
            raise RuntimeError(f"Polymarket says trading is blocked from your location ({g.get('country')})")
        self.db.set("geo", g)

    def buy(self, token_id, dollars, limit_price):
        """Fill-or-kill buy: either fills completely at <= limit_price, or nothing happens."""
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        try:
            try:
                args = MarketOrderArgs(token_id=token_id, amount=round(dollars, 2), side=BUY, price=limit_price)
            except TypeError:  # older library versions have no price cap
                args = MarketOrderArgs(token_id=token_id, amount=round(dollars, 2), side=BUY)
            signed = self.client.create_market_order(args)
            resp = self.client.post_order(signed, OrderType.FOK)
        except Exception as e:
            return None, f"order error: {e}"
        if isinstance(resp, dict) and resp.get("success"):
            return resp.get("orderID") or "ok", None
        return None, (resp.get("errorMsg") if isinstance(resp, dict) else str(resp)) or "rejected"


# =====================================================================
#  BROKER
# =====================================================================
class Broker:
    def __init__(self, db, live: LiveTrader | None = None):
        self.db = db
        self.risk = Risk(db)
        self.live = live

    @property
    def mode(self):
        return "live" if self.live else "paper"

    def buy(self, market, side, est_prob, strategy, signal_id=None):
        """Size with Kelly, run risk checks, then place (paper or live). Returns position id or None."""
        if side == "YES":
            price, token = market.best_ask, market.yes_token
        else:
            price, token = round(1 - market.best_bid, 4), market.no_token
        stake = kelly_stake(est_prob, side, price, self.db.money()["equity"])
        if stake <= 0:
            return self._refuse(signal_id, "edge too small to size")
        shares = math.floor(stake / price * 100) / 100
        if shares < market.min_size:
            return self._refuse(signal_id, f"order below market minimum ({market.min_size:.0f} shares)")
        reason = self.risk.check(market.id, price, stake)
        if reason:
            return self._refuse(signal_id, reason)

        order_id = None
        if self.live:
            if not token:
                return self._refuse(signal_id, "no order-book token")
            order_id, err = self.live.buy(token, stake, price)
            if err:
                self.db.log(f"LIVE order failed on {market.question[:60]}: {err}", "error")
                return self._refuse(signal_id, f"live order failed: {err}")

        pid = self.db.add_position(
            ts=time.time(), mode=self.mode, strategy=strategy, market_id=market.id,
            question=market.question, url=market.url, side=side, token_id=token, price=price,
            shares=shares, cost=round(shares * price, 4), est_prob=est_prob, market_yes=market.mid,
            mark=price, order_id=order_id, signal_id=signal_id)
        self.db.log(f"{self.mode.upper()} BUY {side} @ {price:.3f} ${shares * price:.2f} - {market.question[:70]}")
        if signal_id:
            self.db.set_signal_action(signal_id, f"{self.mode} buy {side} @ {price:.3f} (${shares * price:.2f})")
        return pid

    def _refuse(self, signal_id, reason):
        if signal_id:
            self.db.set_signal_action(signal_id, f"no trade: {reason}")
        return None
