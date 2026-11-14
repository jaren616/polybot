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
# Free alternatives to Claude (see ai.py): GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY,
# or LLM_PROVIDER=ollama for a model on your own computer. Read at call time via key().
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "auto")   # auto | anthropic | gemini | groq | openrouter | ollama
LLM_MODEL_OVERRIDE = os.getenv("LLM_MODEL")        # e.g. "gemini-2.5-flash-lite" to pick another model
AI_MAX_CALLS_PER_DAY = int(os.environ["AI_MAX_CALLS_PER_DAY"]) if os.getenv("AI_MAX_CALLS_PER_DAY") else None


def key(name):
    return os.getenv(name)
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
ENABLE_SPORTS = True      # sports value: Polymarket vs bookmaker odds (needs ODDS_API_KEY)
ENABLE_MOMENTUM = False   # OFF: backtest lost money on all 12 settings tried (Nov 2026)
ENABLE_FAVOURITES = False # OFF: backtest showed no edge after the spread (Nov 2026)

# The high-volume strategies are PAPER ONLY: live mode refuses them. They buy and sell
# far more often, and their exits (selling early) aren't built for real orders.
PAPER_ONLY_STRATEGIES = {"sports", "momentum", "favourites"}

# =====================================================================
#  TIMING (seconds)
# =====================================================================
NEWS_EVERY = 60
MARKETS_REFRESH_EVERY = 300
SLOW_SCAN_EVERY = 300      # arbitrage + movers (loads thousands of events, takes 1-2 min)
MARK_EVERY = 120           # update prices of open positions + equity snapshot
SETTLE_EVERY = 1800        # check for resolved markets
GEOBLOCK_CHECK_EVERY = 3600  # live mode re-checks Polymarket access hourly
FAST_SCAN_EVERY = 60       # momentum + favourites: re-price the busiest markets every minute

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
MARKETS_TO_LOAD = 1500          # more markets = more chances a story matches
MIN_MARKET_LIQUIDITY = 1_000

# =====================================================================
#  NEWS STRATEGY
# =====================================================================
MAX_CANDIDATES_PER_ITEM = 12    # markets shown to the AI per story (one AI call picks the right one)
NEWS_PREFER_DAYS = 30           # markets ending within this many days are ranked first
MIN_MATCH_SCORE = 4.0           # looser keyword match: the AI filters out the wrong ones
MIN_SHARED_WORDS = 2
MAX_NEWS_AGE_MINUTES = int(os.getenv("POLYBOT_MAX_NEWS_AGE", "30"))  # cloud uses 60 (runs are ~10 min apart)
LLM_MODEL = "claude-haiku-5-5"   # Claude model if using ANTHROPIC_API_KEY (free providers have their own defaults)
MIN_LLM_CONFIDENCE = 0.55
MIN_EDGE = 0.05                  # estimate must beat the price by 5 points (was 7: more trades)

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
PRICE_HISTORY_HOURS = float(os.getenv("POLYBOT_PRICE_HISTORY_HOURS", "3"))  # cloud keeps 2 to stay small
MOVER_COOLDOWN_HOURS = 6    # don't re-alert the same market for this long

# =====================================================================
#  SPORTS VALUE (Polymarket vs bookmakers)
# =====================================================================
# Bookmakers' combined odds (with their margin removed) are a sharp estimate of the true
# chance. When Polymarket sells an outcome cheaper than that, buy it.
# Free key at the-odds-api.com: 500 credits a month. Each sport costs 1 credit per check.
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
ODDS_SPORTS = ["soccer_epl", "soccer_uefa_champs_league", "soccer_spain_la_liga",
               "soccer_italy_serie_a", "soccer_germany_bundesliga", "basketball_nba",
               "americanfootball_nfl", "icehockey_nhl"]
ODDS_REGIONS = "eu"            # "eu" includes Pinnacle, the sharpest bookmaker
ODDS_EVERY_MINUTES = int(os.getenv("POLYBOT_ODDS_EVERY", "720"))  # 8 sports x 2/day ~ 480 credits/month (free tier: 500)
ODDS_MIN_REMAINING = 15        # stop fetching when the monthly credits get this low
ODDS_MAX_AGE_MINUTES = 540     # don't trade on odds older than this
SPORTS_MIN_EDGE = 0.02         # Polymarket must be 2+ points cheaper than the bookmakers (was 3: more trades)
SPORTS_MIN_BOOKS = 3           # need at least this many bookmakers' prices
SPORTS_MAX_GAP = 0.20          # bigger gap than this = probably matched the wrong game: skip
SPORTS_MIN_MINUTES_BEFORE = 30 # only bet on games starting at least this far ahead
SPORTS_STAKE = 5.0

# =====================================================================
#  MOMENTUM
# =====================================================================
FAST_MARKETS = 1000             # how many of the busiest markets to re-price every minute
MOMENTUM_WINDOW_MINUTES = 15
MOMENTUM_MIN_MOVE = 0.06        # 6+ point move in 15 min = buy in the direction of the move
MOMENTUM_MIN_LIQUIDITY = 5_000
MOMENTUM_MAX_SPREAD = 0.03
MOMENTUM_STAKE = 5.0
MOMENTUM_COOLDOWN_HOURS = 3     # one momentum trade per market per 3 hours

# =====================================================================
#  FAVOURITES
# =====================================================================
FAV_MIN_PRICE = 0.90
FAV_MAX_PRICE = 0.97
FAV_MAX_HOURS = 48              # market must resolve within 2 days
FAV_MIN_LIQUIDITY = 5_000
FAV_MAX_SPREAD = 0.02
FAV_STAKE = 5.0
FAV_MAX_NEW_PER_SCAN = 5

# =====================================================================
#  EXITS (selling before the market resolves)
# =====================================================================
# Price points per share. Strategies not listed hold until the market resolves.
EXIT_RULES = {
    "momentum": {"take_profit": 0.05, "stop_loss": 0.04, "max_hours": 6},
}

# =====================================================================
#  RISK LIMITS (apply to paper AND live, so paper results are realistic)
# =====================================================================
BANKROLL = 1_000.0          # paper: starting cash. live: the most you're prepared to use
KELLY_FRACTION = 0.25       # bet a quarter of the Kelly amount (full Kelly is far too aggressive)
MIN_STAKE = 1.0
MAX_STAKE = 10.0            # never more than this on one trade
MAX_OPEN_POSITIONS = 200
MAX_EXPOSURE = 900.0        # total money in open positions
MAX_TRADES_PER_DAY = 200
STRATEGY_MAX_OPEN = {"news": 100, "sports": 80, "momentum": 40, "favourites": 60}
DAILY_LOSS_LIMIT = 100.0     # bot pauses itself if equity falls this much in a day (UTC)
MIN_PRICE = 0.05            # never buy long shots below 5c
MAX_PRICE = 0.99            # can buy favourites up to 99c (see HIGH_PRICE_ZONE above)

# =====================================================================
#  FILES / DASHBOARD
# =====================================================================
DB_PATH = "polybot_v2.db"
DASHBOARD_PORT = 8765
