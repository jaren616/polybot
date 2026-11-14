# Polybot: project guide for Claude Code

A Polymarket **paper-trading** bot (no real money) that runs on GitHub Actions every ~30 minutes
and publishes a dashboard to GitHub Pages: https://jaren616.github.io/polybot/
Owner is new to coding: explain changes in plain English, keep diffs small, and say what to click.

## How it runs
- `.github/workflows/polybot.yml`: cron `*/30`, each run trades for 25 min (`POLYBOT_LOOP_MINUTES`),
  then deploys `site/index.html` to Pages. Manual runs take a `minutes` input.
  The SQLite database (`polybot_v2.db`) lives in the **Actions cache** (`polybot-db-<run_id>`),
  not in git. Ends with a `===== RUN SUMMARY =====` block in the log explaining AI decisions.
- `.github/workflows/backtest.yml`: Mondays + manual; report saved to cache and published as `backtest.html`.
- Cloud mode is **paper only** (`run.py cloud` refuses live). GitHub runners are in the US where
  Polymarket trading isn't allowed, and private keys must never go on GitHub.
- Reset to $1,000: change the text in `RESET` and commit (or `py run.py reset` locally).
- Pause: repo variable `POLYBOT_PAUSED=true`. Choose AI: repo variable `LLM_PROVIDER`.

## Secrets / keys (GitHub → Settings → Secrets and variables → Actions)
`GEMINI_API_KEY` (main AI, free), `ANTHROPIC_API_KEY` (backup, paid), `GROQ_API_KEY`,
`OPENROUTER_API_KEY` (optional free backups), `ODDS_API_KEY` (the-odds-api.com, for sports value).
Never print, log or commit a key. `.gitignore` already excludes databases and caches.

## Code map
| File | Job |
|---|---|
| `run.py` | Entry point: `run` (laptop + local dashboard), `once`, `cloud`, `reset`, `resume` |
| `config.py` | Every setting and strategy on/off switch |
| `engine.py` | Runs strategies on timers (fast tick: news/marks/settle; slow tick: arb, movers, sports, crypto) |
| `strategies.py` | NewsStrategy, SlowScan (arb + movers), SportsStrategy, FastScan (momentum + favourites), mark/settle/exits |
| `ai.py` | Judges news with Gemini / Groq / OpenRouter / Ollama / Claude, with automatic fallback + daily cap |
| `sports.py` | Bookmaker odds (The Odds API), de-vig, Pinnacle-preferred fair prices, team matching |
| `crypto.py` | Crypto price markets priced with option maths (Coinbase price + 7-day volatility); `crypto_backtest.py` checks it |
| `broker.py` | Kelly sizing, risk checks, paper fills; live trading via py-clob-client (laptop only, locked) |
| `data.py`, `news_feeds.py`, `matcher.py`, `arb_core.py` | Polymarket API, 33 RSS feeds, keyword matching, arbitrage maths |
| `db.py`, `dashboard.py` | SQLite storage; local dashboard server + static export for Pages |
| `backtest.py` | Replays momentum + favourites on resolved markets (cached in `backtest_cache/`) |

## Commands
```
python test_all.py        # 129 offline checks, must end ALL TESTS PASSED
python test_backtest.py   # 26 checks, must end ALL BACKTEST TESTS PASSED
python run.py once        # one pass of everything (needs internet)
python backtest.py --sweep
```
Run both test files before every push. Tests are offline (all network calls are faked).

## Decisions so far (don't undo without new evidence)
- **Momentum OFF**: backtest (400 markets, 60 days) lost money on all 12 settings, in-sample and unseen;
  ~9% loss per trade, 23% win rate.
- **Favourites OFF**: 91-93% win rate but negative return in backtest and live paper (needs > entry price to break even).
- **On**: AI news (5-point edge, 55% confidence, 2-point edge at 90c+ with 80% confidence),
  sports value (2-point edge vs bookmakers, 8 leagues checked twice a day, needs ODDS_API_KEY), arbitrage + price-move alerts (alerts only).
- **Crypto value ON, weekdays only (14 Nov)**: `crypto_backtest.py` (21 days, 924 BTC/ETH above/range markets):
  model Brier 0.127 vs market 0.130, +17% per $ over 239 trades with 7-day realised vol (beat Deribit DVOL);
  weekdays +24%, weekends -13%, first half of the period only break-even. "Reach/dip" markets off (not backtested).
  Re-run the backtest after a few weeks; turn it off if live paper results disagree.
- Judge strategies by the dashboard's **By strategy** table and **Brier score vs market** after 50+ settled trades.

- **News matching (14 Nov)**: one AI call per story with up to 12 keyword candidates (near-term markets
  ranked first); the AI picks the affected market or none. Replaced one-call-per-market, after a
  diagnostic showed every pick was "not relevant" (e.g. a single NBA game matched to 2027 Finals futures).

## Open items (Nov 2026)
- Watch the RUN SUMMARY: how many stories get a market picked, and whether any reach the 5-point edge.
- Sports: first 4 paper bets 14 Nov. Sports positions opened before 14 Nov ~10:30 UTC stored the bought side's
  chance as est_prob (now the YES chance), so their Brier scores are off.
- Crypto: watch the first weekday trades (from Mon 16 Nov).
- GitHub starts the 30-minute schedule late (runs roughly hourly).

## Also on the owner's laptop
A copy lives at `C:\Users\jaren\OneDrive\Documents\Polymarket\polybot2` (Windows, `py` command).
The owner currently runs only on GitHub.
