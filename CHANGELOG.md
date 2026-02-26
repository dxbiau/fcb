# FCB Bot — Change Log
> **Every code change made by Copilot is logged here.** Read this to understand what changed, why, and what was deleted.
> Entries are newest-first. Each entry includes: date, what changed, files affected, and reason.

---

## 2026-02-25 — v13pro: SHADOW DATA ANALYSIS + 4 FILTER UPGRADES + HUNTER SCALP SYSTEM

### Context
v13pro has been running live for several days collecting shadow data on every signal (traded and rejected).
2,239 shadow outcomes were analysed to find statistically significant filter improvements. The analysis
revealed that hunter signals were being wasted — most were blocked by `risk_gate` (portfolio slot contention),
not by bad conviction. Counter-trend move-depth analysis proved longs-in-BEAR are a goldmine (91% WR at 0.75R TP)
while shorts-in-BULL are not scalp-worthy (median peak only 0.28R).

### Added — New Intelligence Modules
- **v13pro/sentiment.py** (~303 lines) — Real-time BTC/ETH/SOL momentum sentiment gauge
  - EMA-8 vs EMA-21 slope, higher-highs/lower-lows structure, close vs EMA-21
  - Aggregate majority vote with confidence weighting
  - Output: bias (BULL/BEAR/NEUTRAL) + confidence percentage
  - Uses WS data with REST fallback
- **v13pro/orderflow.py** (~200 lines) — L2 orderbook microstructure snapshots
  - Spread (bid-ask) in bps, orderbook imbalance, depth ratio
  - 20-level depth, 5s cache per symbol
  - Logged with every signal for conviction scoring
- **v13pro/shadow.py** (~454 lines) — Passive shadow trader for ALL signals
  - Simulates entries on passed AND rejected signals without real orders
  - Tracks peak_r/trough_r, 1m/5m/15m/60m checkpoints
  - Max 500 concurrent shadow tracks
  - Writes daily JSONL to `v13pro/logs/shadow/`
  - Fixed asyncio deque mutation bug (Lock + snapshot iteration)
- **_analyze_shadow.py** — Basic shadow outcome analysis script
- **_shadow_filters.py** — Comprehensive filter optimisation across shadow data
- **_hunter_countertrend_analysis.py** — Hunter signal + counter-trend move depth analysis

### Changed — 4 Data-Driven Filter Upgrades (from 2,239 shadow outcomes)

#### 1. Trailing Stop Upgrade (+326R improvement in shadow data)
- **v13pro/config.py**:
  - `TRAIL_ACTIVATION_R`: 1.5 → **0.75** (activate sooner, capture more runners)
  - `TRAIL_DISTANCE_R`: 0.30 → **0.40** (wider trail, fewer shakeouts)

#### 2. Sentiment Filter (saves 99R in shadow data)
- **v13pro/bot.py** (`_execute_signal`):
  - Block SHORT signals when sentiment = BULL with confidence ≥ 50%
  - Logged as `sentiment_bull_block` in shadow rejection reasons
  - Data showed shorts-in-BULL have median peak of only 0.28R — not profitable

#### 3. MTF_RSI Strategy Removal (0% TP hit rate across 92 outcomes)
- **v13pro/deploy_combos.json**: Removed 10 MTF_RSI combos (50 → 40 combos)
  - MTF_RSI had 0 TP hits across all shadow data, even best scalp scenario was net negative
  - 38 combos active after 2 additional delisted-pair skips

#### 4. Orderbook Alignment Conviction Boost
- **v13pro/bot.py** (`_execute_signal`):
  - When orderbook imbalance aligns with trade direction: +8 conviction points
  - buy pressure (imbalance > 0.10) on LONG, sell pressure (< -0.10) on SHORT
  - Logged as `OB aligned +8 -> conv=XX` in bot log

### Added — Hunter Scalp System (cherry-pick 0.75R quick profits)

#### Analysis Finding
- Hunter had 1,900/1,901 rejections as `risk_gate` — portfolio fills concurrent slots
- 938 signals actually passed conviction (132 A+, 498 A)
- Longs-in-BEAR at 0.75R TP: **91.1% WR, +120.8R** — goldmine being thrown away
- Shorts-in-BULL at 0.75R TP: 53% WR, +3.5R — not worth it (correctly blocked by sentiment filter)

#### Implementation
- **v13pro/registry.py**: Added `fix0.5` and `fix0.75` to EXIT_PARAMS
  - `{'type': 'fixed', 'tp_r': 0.5}` and `{'type': 'fixed', 'tp_r': 0.75}`
- **v13pro/config.py** — Hunter section updated:
  - `HUNTER_MAX_POSITIONS`: 3 → **5** (independent slots from portfolio)
  - `HUNTER_RISK_MULT`: 0.5 → **0.35** (35% of portfolio risk per scalp ≈ $3.65-$4.56)
  - `HUNTER_MIN_GRADE`: "B" → **"C"** (lower bar — quick scalps don't need A+)
  - `HUNTER_EXIT_MODE`: "fix2.0" → **"fix0.75"** (data-proven: 91% WR at 0.75R on longs)
- **v13pro/state.py**: Added `can_trade_hunter(pair)` method
  - Bypasses portfolio `max_concurrent` — hunter gets its own 5 slots
  - Only checks: not already trading same symbol, daily count < MAX_TRADES_DAY, growth cap
- **v13pro/bot.py** (`_execute_signal`):
  - Separate hunter risk path: checks `hunter_positions < HUNTER_MAX_POSITIONS` then `can_trade_hunter()`
  - Portfolio signals still use full `can_trade()` with all risk checks
  - Shadow logs distinct rejection reasons: `hunter_slots_full`, `hunter_risk_gate`

### Fixed
- **v13pro/shadow.py** — asyncio deque mutation during iteration (Lock + snapshot)
- **v13pro/watchdog.py** — Memory usage 1705MB (aggressive GC + ccxt cache trimming)
- **v13pro/ws_data.py** — Session variable reference fix
- **v13pro/bot.py** — `_process_hunter_signals` removed redundant max position pre-check

### Verified Live — 2026-02-25 09:53 UTC
Hunter scalp system fired **5 trades in first scan cycle**:
- SHORT XRP/USDT [TR_PULL] conv=80 A+ risk=$4.56
- LONG PIPPIN/USDT [PIN_BAR] conv=68 A risk=$4.01
- LONG POWER/USDT [BB_BREAK] conv=94 A+ risk=$4.56 (OB aligned +8)
- LONG HYPE/USDT [EMA_RIB] conv=81 A+ risk=$4.56
- SHORT RIVER/USDT [DONCHIAN] conv=75 A risk=$4.01

All using fix0.75 exit mode. Duplicate-symbol prevention working. Sentiment filter active (BULL, 100% conf).

---

## 2026-02-24 — v13pro ARCHITECTURE (complete rewrite from FCB)

### Context
The entire bot was rewritten from the FCB (First Candle Breakout) single-strategy system to v13pro —
a multi-strategy, multi-timeframe, conviction-scored architecture. This is no longer the FCB bot;
it is a 12-strategy ensemble engine with DNA profiling, skill scoring, and pair hunting.

### Architecture Changes
- **Entry point**: `python -m v13pro.run --maker --entry` (NOT `run_live.py`)
- **12 strategies**: EMA_RIB, BB_BREAK, DONCHIAN, RSI_FADE, BB_FADE, STOCH_X, PIN_BAR, IB_BREAK, ENGULF, MTF_RSI, TR_PULL, MOM_SURGE
- **Combo registry**: 40 combos across 28 pairs, 3 timeframes (15m, 30m, 1h)
- **Conviction scoring**: 0-100 scale with grades (A+/A/B/C/D/X) via PerformanceSkill
- **DNA profiler**: 25 raw numeric features per trade → correlation analysis for winning conditions
- **WebSocket data**: Zero REST polling, multi-TF candle buffers via ccxt.pro
- **8x leverage, 2% risk** (scaled by conviction and equity curves)
- **Isolated margin** on all positions

### New Modules (v13pro/)
| Module | Purpose |
|--------|---------|
| bot.py | Main async 24/7 orchestrator |
| strategies.py | 12 strategies + ensemble scanning |
| registry.py | Combo registry + exit parameter definitions |
| config.py | Full configuration (risk curves, leverage curves, growth phases) |
| state.py | Thread-safe bot state with JSON persistence |
| exchange.py | Bybit async API wrapper |
| guardian.py | Position guardian: progressive SL tiers + trailing stop + rejection exit + funding rate monitor |
| hunter.py | Async pair universe scanner for non-portfolio pairs |
| skill.py | Multi-factor conviction scorer (key levels, candle quality, volume, trend, fee efficiency) |
| dna.py | Setup DNA profiler (statistical edge discovery via pandas) |
| ws_data.py | Async WebSocket multi-TF data engine |
| watchdog.py | System health monitor (network, memory, WS health, disk, equity) |
| indicators.py | Technical indicator calculations |
| journal.py | Trade journal |
| trade_logger.py | Structured JSONL trade logger |
| logger.py | Timestamped logging |
| preflight.py | Pre-flight system checks |
| supervisor.py | Bot supervisor/restart logic |
| aftermath.py | Post-trade analysis tracker |
| learner.py | Adaptive learning from trade outcomes |

---

## 2026-02-21 — WORKSPACE REORGANIZATION & BASELINE LOCK

### Purpose
Reorganize entire workspace so we never rollback past this point. All code, config, and trade data
from the aggressive x10 session is preserved. Temp files cleaned, scripts organized, state backups archived.

### Moved to `scripts/sweeps/`
- agentic_sweep.py, sweep_configs.py, sweep_fine.py, sweep_micro_filters.py
- sweep_15m_pairs.py, fcb_sweep.py, mega_sweep.py, param_sweep.py, mega_upgrade_sim.py

### Moved to `scripts/analysis/`
- analyze_sweep.py, analyze_sweep2.py, analyze_trades.py, contra_analysis.py
- entry_analysis.py, entry_analysis_live.py, entry_rr_analysis.py, hybrid_tp_analysis.py
- loser_forensics.py, peak_analysis.py, research_winner_loser.py, smart_entry_analysis.py
- wr_deep_dive.py, x1000_analysis.py, x1000_hunter.py, x10_planner.py
- breakout_dna_discovery.py, fakeout_dna.py, edge_finder.py, guardian_replay.py
- projection_model.py, skill_oracle.py, realtime_intelligence.py, volume_hunt.py
- audit_checkpoints.py, audit_optimization.py

### Moved to `scripts/testing/`
- test_15m_tp.py, test_connection.py, test_filters_h2h.py, test_full_stop.py
- test_limit_sweep.py, test_mainnet.py, test_partial_tp.py, test_risk_levels.py
- test_risk_levels_bybit.py, test_tp_variants.py, test_trailing.py
- truth_test.py, truth_test2.py, verify_edge.py, probe_testnet.py

### Moved to `scripts/runners/`
- run_aggressive_backtest.py, run_analysis.py, run_backtest.py, run_demo.py
- run_dual_tf_backtest.py, run_full_backtest.py, run_live_backtest.py, run_x10.py
- mass_backtest_bybit.py

### Moved to `scripts/tools/`
- check_new_pairs.py, check_peaks.py, compare_exchanges.py, compare_gate.py
- compare_scan.py, discover_bybit_pairs.py, download_missing.py, download_sweep_pairs.py
- find_55_wr.py, pair_scanner.py, scan_new_pairs.py, screen_all_candles.py
- screen_session_opens.py, quick_scan.py

### Moved to `archive/state_backups/`
- ~50 state_demo_backup_*.json files from live/
- state_pre_oracle.json, trades_pre_oracle.csv, trades_pre_reset_20260218.csv

### Moved to `archive/output_logs/`
- dual_tf_err.txt, dual_tf_results.txt, dual_tf_results_v2.txt, full_backtest_results.txt
- micro_filter_results.txt, micro_filter_results_backup.txt, peak_output.txt
- sweep_15m_err/out/results.txt, x10_out.txt, x10_output.txt, x10_results.txt

### Moved to `archive/old_deploys/`
- fcb-bot-deploy.tar.gz, fcb-bot-deploy_20260217_135500.zip

### Deleted (temp session files)
- _analyze_discovery.py, _analyze_discovery2.py, _check_bal.py, _check_positions.py
- _debug_trades.py, _deep_analysis.py, _forensic_analysis.py, _sim_scaleout.py, _test_edge.py
- __pycache__/ directories, .pytest_cache/

### Kept at root (essential)
- run_live.py, config.py, dashboard.py, monitor_trade.py, quick_live_test.py
- watchdog.py, preflight_check.py
- All markdown docs, Docker files, requirements, setup files

### Updated
- **PROJECT_TRACKER.md** — complete rewrite reflecting current aggressive x10 state
- **CHANGELOG.md** — this entry

---

## 2026-02-21 — BUG FIX: pending_entries .values() error

### Fixed
- **live/bot.py** (line ~1053) — `self.state.pending_entries.values()` → `self.state.pending_entries`
  - `pending_entries` is a **list** not a dict (defined in state.py line 62)
  - Caused tick error: `'list' object has no attribute 'values'`
  - Was in B-class concurrent slot cap check code
- **live/bot.py** — Reverted DOM `order_flow` import that was temporarily added

---

## 2026-02-21 — EDGE SCORE TIERED RISK SIZING

### Changed
- **live/edge_score.py** — Upgraded from no-op (always risk_mult=1.0) to tiered risk:
  - S_elite: risk_mult=1.0 (full Kelly)
  - A_quality: risk_mult=1.0 (full Kelly)
  - B_standard: risk_mult=0.75
  - C_quick: risk_mult=0.60
  - D_low: risk_mult=0.50
  - N/A (no oracle): risk_mult=0.75
- Updated docstring from "TP=0.5R" to trail mode context
- format_score() shows risk% and TRAIL tag

---

## 2026-02-21 — ORDER FLOW DOM MODULE (RESEARCH ONLY)

### Added
- **live/order_flow.py** (~230 lines) — DOM order book intelligence
  - Bid/ask imbalance, wall detection, depth ratio analysis
  - NOT integrated into bot.py — agent assessed it does NOT fit FCB strategy
  - Wrong timeframe, spoofed books, not backtestable
  - Kept as standalone research/logging tool

---

## 2026-02-21 — AGGRESSIVE X10 MODE (Kelly + Trail)

### Mathematical Basis
- **Axiom**: At 30-50% WR, payoff asymmetry (b = avg_win/avg_loss) determines survival
- 0.5R TP gave b=0.65 — BELOW breakeven at any WR under 61%. Penny collecting.
- Trail mode gives avg_win ~1.28R, avg_loss ~0.77R → b=1.66 → positive at 40%+ WR
- Kelly Criterion at WR=45%, b=1.66: **f* = 13.6%**. Using 12% (sub-Kelly).
- Growth/trade = 1.023 → x10 in ~100 trades (~12 days at 8 tpd)

### Changed
- **live/config.py** — FULL aggressive overhaul (12 parameter changes):
  - `RISK_PCT_A`: 0.02 → **0.12** (12%, Kelly-optimized)
  - `RISK_PCT_B`: 0.02 → **0.06** (6%, half-Kelly for unproven)
  - `TP_R`: 0.5 → **1.5** (REVERTED — asymmetry > frequency)
  - `EXCHANGE_TP_R`: 0.5 → **10.0** (safety net, trail handles real exit)
  - `TRAIL_ENABLED`: False → **True** (runners are everything)
  - `TRAIL_DISTANCE_R`: 0.3 → **0.5** (wider to survive whipsaw)
  - `LEVERAGE`: 10 → **20** (needed for margin at 12% risk sizing)
  - `MIN_RANGE_PCT`: 0.003 → **0.005** (wider ranges = stronger breakouts + margin room)
  - `MAX_CONCURRENT_POSITIONS`: 8 → **3** (focus > dilution, margin-limited)
  - `MAX_CONCURRENT_B`: 3 → **1** (reserve slots for A-class)
  - `PROFIT_TIERS`: Accelerated BE at +0.5R (was +0.75R), removed T4 (trail handles it)
  - Updated docstring header, backtest baseline, risk comments

### Deleted / Reverted
- **Oracle 0.5R TP mode** — mathematically proven to collect pennies. b=0.65 needs 61%+ WR.
- **T4 tier** (at +1.35R → SL +1.0R) — trail handles this zone now
- **Conservative 2% risk** — 1/7th of Kelly optimal, guaranteed glacial growth

### Risk Assessment
- P(5 consecutive losses) at 55% loss rate: 5.0% → 29% drawdown ($151→$107)
- P(10 consecutive losses): 0.25% → 47% drawdown ($151→$80)
- With positive expectancy, Kelly sizing guarantees long-term growth (never reaches zero)

---

## 2026-02-21 — Session Reorganisation & Change Tracking

### Added
- **CHANGELOG.md** (this file) — persistent log of all code changes made by Copilot
  - Prevents confusion about what was modified/deleted
  - Must be updated with every change going forward

### Current State Summary
The bot has been live since ~2026-02-18. Key stats at this point:
- **Equity:** $200.71 (started $150, +33.8%)
- **Trades:** 18 (6W / 12L, 33% WR vs 47% backtest)
- **Total R:** -1.691R (saved by large winners: +1.59R, +1.46R, +1.39R)
- **Oracle mode:** Queued to start at trade 19 (0.5R TP, 2% risk)
- **Bot status:** Not running (stopped ~00:49 UTC Feb 21)

### Files Map (as of this date)
```
Root scripts:
  run_live.py           — Launch live bot
  run_backtest.py       — Run backtest + analysis
  dashboard.py          — Real-time HTTP dashboard (localhost:8080)
  config.py             — Backtest parameters
  quick_live_test.py    — Quick manual FCB trade scanner
  monitor_trade.py      — Standalone trade monitor
  watchdog.py           — Bot watchdog
  preflight_check.py    — Pre-flight check runner

Live system (live/):
  bot.py                — Main trading loop
  config.py             — All live parameters (pairs, sessions, risk, API keys)
  exchange.py           — Bybit ccxt wrapper
  guardian.py           — Pre-entry margin checks, position health
  profit_guardian.py    — SL/TP daemon (progressive tiers + fixed TP)
  state.py              — Persistent bot state
  strategy.py           — Live FCB signal logic
  trades.py             — Trade CSV logger
  trade_logger.py       — Structured JSONL logger
  logger.py             — Timestamped logging
  edge_score.py         — Edge scoring system
  growth_tracker.py     — Growth tracking
  journal.py            — Trade journal
  session_reviewer.py   — Session review logic
  pair_scanner.py       — Live pair scanner

Strategy core:
  strategy/fcb_core.py  — Core FCB engine (framework-agnostic)

Backtest:
  backtest/engine.py    — Backtest runner + equity curves

Analysis:
  analysis/scorecard.py — 8-check GO/NO-GO gate

Docs:
  README.md             — Full project documentation
  PROJECT_TRACKER.md    — Single source of truth for status
  IMPROVEMENT_PLAN.md   — Pair expansion research
  CHANGELOG.md          — This file (change log)
```

---

<!-- TEMPLATE — copy this for each new entry:

## YYYY-MM-DD — Short Description

### Added
- **file.py** — description of new file/feature

### Changed
- **file.py** — what was modified and why
  - Old behavior: X
  - New behavior: Y

### Deleted
- **file.py** — why it was removed

### Fixed
- **file.py** — bug description and fix

### Notes
- Any context or decisions made

-->
