# Polybot v2

A Polymarket trading bot with three strategies, risk controls, and a live browser dashboard.
**Paper trading by default.** Live trading only switches on when you deliberately set it up.

| Strategy | What it does | Trades? |
|---|---|---|
| **AI news** | Reads news every minute, matches it to markets, asks Claude whether it changes the odds, buys when the estimate beats the price by 7+ points | Yes (paper or live) |
| **Arbitrage** | Every 5 min, scans ~4,000 events for prices that contradict each other in quiet markets; tracks how long each gap lasts | Alerts only |
| **Price moves** | Flags markets that jump 10+ points in an hour, or get a sudden volume spike (often someone knows something) | Alerts only |
| **Sports value** | Compares Polymarket's game prices with bookmakers' odds (margin removed, Pinnacle preferred); buys outcomes 4+ points cheaper on Polymarket | Yes, paper only, $5 each |
| **Momentum** | Re-prices the 1,000 busiest markets every minute; follows any 6+ point move in 15 min; sells at +5 points, -4 points, or after 6 hours | Yes, paper only, $5 each |
| **Favourites** | Buys outcomes priced 90-97c that resolve within 2 days | Yes, paper only, $5 each |

The dashboard's **By strategy** table shows each strategy's own P&L and win rate, so you can
see which ones actually work. Turn any of them off with the `ENABLE_...` settings in config.py.

**Sports value needs a free odds key:** sign up at the-odds-api.com (500 credits a month free),
then `set ODDS_API_KEY=...` before `py run.py` (on GitHub: a secret named `ODDS_API_KEY`).
With the default 3 sports checked every 6 hours it uses about 360 credits a month.

Arbitrage is never auto-traded: it needs several orders to fill at once, and if only some fill
you're left holding a one-sided bet. Act on those alerts by hand after reading the rules.

---

## 1. Run it (paper mode)

In a terminal inside this folder:

```
py -m pip install -r requirements.txt
py test_all.py
py run.py
```

`test_all.py` should end with **ALL TESTS PASSED**. `run.py` starts the bot and opens the dashboard
at **http://localhost:8765**. Leave the terminal window open; Ctrl+C stops it.

For the AI news strategy to trade, set an Anthropic API key first (console.anthropic.com):

```
set ANTHROPIC_API_KEY=sk-ant-...
py run.py
```

Without it, news alerts still show on the dashboard, but nothing is traded.

**Free alternatives to Claude:** set any one of these instead (the bot uses the first it finds):

| Key | Where to get it (free) | Default model |
|---|---|---|
| `GEMINI_API_KEY` | aistudio.google.com → Get API key (Google account, no card) | `gemini-3.8-flash` |
| `GROQ_API_KEY` | console.groq.com → API Keys | `openai/gpt-oss-120b` |
| `OPENROUTER_API_KEY` | openrouter.ai → Keys | `openai/gpt-oss-120b:free` |
| `LLM_PROVIDER=ollama` | ollama.com, then `ollama pull llama3.1:8b` (runs on your PC, laptop only) | `llama3.1:8b` |

**Set more than one key and they back each other up:** if your main AI is busy, down, timing out
or rate-limited, the bot asks the next one (e.g. Gemini first, Groq as backup). Pick a different
model with `LLM_MODEL`. Free tiers have daily limits, so the bot stops at 150 AI
checks a day (change with `AI_MAX_CALLS_PER_DAY`) and pauses 5 minutes if it's rate-limited.
Free tiers may use your prompts to improve their products; the prompts are only public news and
market questions. The dashboard shows which AI is in use and how many checks it's used today.

## 2. The dashboard

- **Top row:** equity, today's P&L, total P&L, open positions, win rate, and **Brier score vs the market**.
  The Brier score is the number that matters: if yours isn't lower than the market's after 50+ settled
  trades, there's no real edge.
- **Equity chart**, open positions (with live prices), closed trades.
- **Signals feed:** every news match, arbitrage gap and price move, with what the bot did and why.
- **Pause / Resume** button: stops new trades instantly. Scanning and alerts continue.

## 3. Risk controls (config.py)

Applied identically in paper and live, so paper results reflect what live would have done.

| Setting | Default | Meaning |
|---|---|---|
| `BANKROLL` | $1,000 | Paper starting cash; in live, the most you intend to use |
| `KELLY_FRACTION` | 0.25 | Bets a quarter of the mathematically "optimal" Kelly size |
| `MAX_STAKE` | $10 | Never more than this on one trade |
| `MAX_EXPOSURE` | $200 | Total money in open positions |
| `MAX_OPEN_POSITIONS` | 20 | |
| `MAX_TRADES_PER_DAY` | 10 | |
| `DAILY_LOSS_LIMIT` | $50 | Bot **pauses itself** if equity drops this much in a day |
| `MIN_PRICE` / `MAX_PRICE` | 0.05 / 0.95 | Skips long shots and near-certainties |
| `MIN_EDGE` | 0.07 | Estimate must beat the price by 7 points |

Positions are held until the market resolves. The bot settles them automatically.

## 4. Going live (only when the paper results justify it)

**Before anything:** Polymarket must allow trading where you live and where you connect from.
The bot checks Polymarket's own access check at start-up and every hour, and refuses or pauses
if you're blocked. Never use a VPN to get around it.

1. **Use a dedicated Polymarket account** funded with only what you can afford to lose.
2. Get your **private key** and **wallet address** from Polymarket (account settings → export
   private key). `POLY_FUNDER` is the wallet address shown on your Polymarket profile.
   `POLY_SIGNATURE_TYPE` is `1` if you log in by email, `2` if you connected a browser wallet.
3. Install the extra library:
   ```
   py -m pip install -r requirements-live.txt
   ```
4. Lower `MAX_STAKE` in config.py to `2` for your first live week.
5. Start it in a terminal (these settings last only for that window, nothing is saved to disk):
   ```
   set POLYBOT_MODE=live
   set POLY_PRIVATE_KEY=0x...
   set POLY_FUNDER=0x...
   set POLY_SIGNATURE_TYPE=1
   set POLYBOT_LIVE_CONFIRM=I accept the risk
   set ANTHROPIC_API_KEY=sk-ant-...
   py run.py
   ```
6. **Watch the first live trade happen** and check it on polymarket.com. The live order code
   could not be tested against the real exchange when it was written, so confirm the first fill
   yourself before leaving it unattended.

Live orders are **fill-or-kill**: they fill completely at the quoted price or not at all.
Winnings from resolved markets are redeemed on polymarket.com.

**Never** put your private key in a file, an email, a screenshot, or a GitHub repo.

## 5. Backtesting

`py backtest.py` replays the **momentum** and **favourites** strategies on markets that already
resolved (last 60 days, 300 busiest), paying a 1c spread on every buy and sell, and opens a report:

- **In-sample vs unseen:** the older 70% of markets vs the newer 30%. Trust the unseen numbers.
- **Momentum (reversed):** bets against every move. If it does as well, direction isn't the edge.
- **Calibration table:** do outcomes priced at 90c really happen 90% of the time?
- `py backtest.py --sweep` also tries other settings and shows how the best ones did on unseen markets.
- `py backtest.py --offline` re-runs instantly on data already downloaded (in `backtest_cache/`).

On GitHub it runs every Monday (or **Actions → backtest → Run workflow**), and the report appears at
`https://<username>.github.io/<repository>/backtest.html`, linked from the dashboard.

News and sports value can't be backtested this way (no historical headlines or bookmaker odds).

## 6. Running on GitHub (laptop can be off)

The `.github/workflows/polybot.yml` file runs one pass of everything every ~10 minutes on
GitHub's servers and publishes the dashboard as a website. **Paper only**: GitHub's servers are
in the US, where Polymarket trading isn't allowed, and a private key must never go on GitHub.

- **API keys:** repository **Settings → Secrets and variables → Actions → New repository secret**,
  name `ANTHROPIC_API_KEY` or a free one (`GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`),
  and `ODDS_API_KEY` for sports. Secrets are hidden, even in a public repository.
- **Website:** **Settings → Pages → Source: GitHub Actions**. The dashboard appears at
  `https://<username>.github.io/<repository>/` and refreshes every 2 minutes.
- **Pause / resume:** **Settings → Secrets and variables → Actions → Variables**, add
  `POLYBOT_PAUSED` = `true` to pause or `false` to resume.
- **Run now:** **Actions → polybot → Run workflow**.
- **Start again at $1,000:** edit the `RESET` file (any new text, e.g. today's date) and commit.
  The next run wipes the paper account; bookmaker odds and today's AI call count are kept.
- The database lives in GitHub's cache between runs, not in the repository, so trades stay
  out of the public code. If the bot doesn't run for 7 days the cache expires and history restarts.
- Cloud runs use a 60-minute news window (runs are ~10 minutes apart and GitHub can start late).

## Files

| File | Job |
|---|---|
| `run.py` | Start here |
| `sports.py` | Bookmaker odds, margin removal, matching games to Polymarket |
| `config.py` | Every setting |
| `engine.py` | Runs strategies on their schedules |
| `strategies.py` | News trading, arbitrage + movers, settlement |
| `broker.py` | Kelly sizing, risk checks, paper + live orders |
| `dashboard.py` | The browser dashboard |
| `data.py`, `news_feeds.py`, `matcher.py`, `ai.py`, `arb_core.py` | Polymarket data, news, matching, Claude, arbitrage maths |
| `db.py` | Storage (`polybot_v2.db`) |
| `test_all.py` | Offline tests |
| `backtest.py`, `test_backtest.py` | Backtester and its tests |
| `.github/workflows/polybot.yml` | The GitHub schedule (section 5) |
