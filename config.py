"""All settings in one place. Change numbers here, not in the code."""
import os

# =====================================================================
#  MODE
# =====================================================================
# "paper" = simulated trades only (default, safe).
# "live"  = real orders on Polymarket. Live ALSO needs these to be set:
#           POLY_PRIVATE_KEY, POLY_FUNDER, and POLYBOT_LIVE_CONFIRM (see README).
MODE = os.getenv("POLYBOT_MODE", "paper").lower()
LIVE_CONFIRM_PHRASE = "I accept the risk"

# =====================================================================
#  KEYS (read from environment variables, never typed into this file)
# =====================================================================
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN")
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY")        # live only
POLY_FUNDER = os.getenv("POLY_FUNDER")                  # live only: your Polymarket wallet address
POLY_SIGNATURE_TYPE = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))  # 1 = email/Magic login, 2 = browser wallet

# =====================================================================
#  STRATEGIES ON/OFF
# =====================================================================
ENABLE_NEWS = True     # AI news trading (trades, paper or live)
ENABLE_ARB = True      # arbitrage scanner (alerts + gap tracking, never auto-traded)
ENABLE_MOVERS = True   # price-move / volume-spike alerts (alerts only)

# =====================================================================
#  TIMING (seconds)
# =====================================================================
NEWS_EVERY = 60
MARKETS_REFRESH_EVERY = 300
SLOW_SCAN_EVERY = 300      # arbitrage + movers (loads thousands of events, takes 1-2 min)
MARK_EVERY = 120           # update prices of open positions + equity snapshot
SETTLE_EVERY = 1800        # check for resolved markets
GEOBLOCK_CHECK_EVERY = 3600  # live mode re-checks Polymarket access hourly

# =====================================================================
#  NEWS SOURCES
# =====================================================================
RSS_FEEDS = [
    # --- World / general ---
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/uk/rss.xml",
    "https://www.theguardian.com/world/rss",
    "https://feeds.npr.org/1001/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://feeds.skynews.com/feeds/rss/world.xml",
    "https://www.france24.com/en/rss",
    "https://rss.dw.com/rdf/rss-en-all",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    # --- Politics (US + UK) ---
    "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "https://www.theguardian.com/us-news/rss",
    "https://rss.politico.com/politics-news.xml",
    "https://thehill.com/news/feed/",
    "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
    # --- Business / markets / central banks ---
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://www.theguardian.com/business/rss",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.cnbc.com/id/100727362/device/rss/rss.html",
    "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.bankofengland.co.uk/rss/news",
    "https://www.sec.gov/news/pressreleases.rss",
    # --- Crypto ---
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    # --- Tech / AI ---
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    # --- Sport (Polymarket has a lot of sports markets) ---
    "https://feeds.bbci.co.uk/sport/rss.xml",
    "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "https://www.theguardian.com/football/rss",
    "https://www.espn.com/espn/rss/news",
    "https://www.espn.com/espn/rss/nfl/news",
    "https://www.espn.com/espn/rss/nba/news",
]
X_ACCOUNTS = ["Reuters", "AP", "BNONews"]   # only used if X_BEARER_TOKEN is set (billed per read)
X_MAX_RESULTS_PER_ACCOUNT = 5

# =====================================================================
#  POLYMARKET
# =====================================================================
GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
MARKETS_TO_LOAD = 1000          # more markets = more chances a story matches
MIN_MARKET_LIQUIDITY = 1_000

# =====================================================================
#  NEWS STRATEGY
# =====================================================================
MAX_CANDIDATES_PER_ITEM = 3
MIN_MATCH_SCORE = 6.0
MIN_SHARED_WORDS = 2
MAX_NEWS_AGE_MINUTES = int(os.getenv("POLYBOT_MAX_NEWS_AGE", "30"))  # cloud uses 60 (runs are ~10 min apart)
LLM_MODEL = "claude-haiku-5-5"   # cheap + fast; "claude-sonnet-5-5" is smarter but ~3x the cost
MIN_LLM_CONFIDENCE = 0.6
MIN_EDGE = 0.07                  # estimate must beat the price by 7 points

# High-probability trades (buying at 90c+, e.g. a favourite, or NO on a long shot).
# A 7-point edge is impossible up there, so they use a smaller edge - but need more
# confidence, because one wrong 95c bet wipes out about 19 winning ones.
HIGH_PRICE_ZONE = 0.90           # buy prices at or above this count as high-probability
MIN_EDGE_HIGH = 0.02             # 2-point edge is enough in that zone...
HIGH_PRICE_MIN_CONFIDENCE = 0.8  # ...but only when the AI is at least 80% confident

# =====================================================================
#  PRICE-MOVE ALERTS
# =====================================================================
MOVE_POINTS = 0.10          # flag a 10-point move...
MOVE_WINDOW_MINUTES = 60    # ...within the last hour
MOVER_MAX_SPREAD = 0.05     # ignore markets with wide spreads (moves there are noise)
MOVER_MIN_LIQUIDITY = 1_000
VOL_SPIKE_USD = 10_000      # flag if 24h volume jumps by this much within the window
PRICE_HISTORY_HOURS = float(os.getenv("POLYBOT_PRICE_HISTORY_HOURS", "6"))  # cloud keeps 2 to stay small
MOVER_COOLDOWN_HOURS = 6    # don't re-alert the same market for this long

# =====================================================================
#  RISK LIMITS (apply to paper AND live, so paper results are realistic)
# =====================================================================
BANKROLL = 1_000.0          # paper: starting cash. live: the most you're prepared to use
KELLY_FRACTION = 0.25       # bet a quarter of the Kelly amount (full Kelly is far too aggressive)
MIN_STAKE = 1.0
MAX_STAKE = 10.0            # never more than this on one trade
MAX_OPEN_POSITIONS = 50
MAX_EXPOSURE = 500.0        # total money in open positions
MAX_TRADES_PER_DAY = 30
DAILY_LOSS_LIMIT = 50.0     # bot pauses itself if equity falls this much in a day (UTC)
MIN_PRICE = 0.05            # never buy long shots below 5c
MAX_PRICE = 0.99            # can buy favourites up to 99c (see HIGH_PRICE_ZONE above)

# =====================================================================
#  FILES / DASHBOARD
# =====================================================================
DB_PATH = "polybot_v2.db"
DASHBOARD_PORT = 8765
