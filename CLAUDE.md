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
| `engine.py` | Runs strategies on timers (fast tick: news/marks/settle; slow tick: arb, movers, sports) |
| `strategies.py` | NewsStrategy, SlowScan (arb + movers), SportsStrategy, FastScan (momentum + favourites), mark/settle/exits |
| `ai.py` | Judges news with Gemini / Groq / OpenRouter / Ollama / Claude, with automatic fallback + daily cap |
| `sports.py` | Bookmaker odds (The Odds API), de-vig, Pinnacle-preferred fair prices, team matching |
| `broker.py` | Kelly sizing, risk checks, paper fills; live trading via py-clob-client (laptop only, locked) |
| `data.py`, `news_feeds.py`, `matcher.py`, `arb_core.py` | Polymarket API, 33 RSS feeds, keyword matching, arbitrage maths |
| `db.py`, `dashboard.py` | SQLite storage; local dashboard server + static export for Pages |
| `backtest.py` | Replays momentum + favourites on resolved markets (cached in `backtest_cache/`) |

## Commands
```
python test_all.py        # 95 offline checks, must end ALL TESTS PASSED
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
  sports value (3-point edge vs bookmakers, needs ODDS_API_KEY), arbitrage + price-move alerts (alerts only).
- Judge strategies by the dashboard's **By strategy** table and **Brier score vs market** after 50+ settled trades.

## Open items (Nov 2026)
- No trades overnight 13-14 Nov: read the RUN SUMMARY in recent logs to see whether the AI marks stories
  "already priced in", "not relevant", or finds no matches.
- `ANTHROPIC_API_KEY` showed empty in runs after the owner said they added it: check the secret name.
- `ODDS_API_KEY` not added yet, so sports value is idle.
- GitHub starts the 30-minute schedule late (runs roughly hourly).

## Also on the owner's laptop
A copy lives at `C:\Users\jaren\OneDrive\Documents\Polymarket\polybot2` (Windows, `py` command).
The owner currently runs only on GitHub.
