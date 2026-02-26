# OBR — Outside Bar Reversal Trading Bot

## Autonomous Live Trading System for Bybit Perpetual Futures

> **Fade the extreme. Catch the reversal.**
> One outside bar traps the crowd. One confirmation candle proves the turn. The bot executes the rest.

---

## Table of Contents

1. [Overview](#overview)
2. [The Strategy — Outside Bar Reversal](#the-strategy--outside-bar-reversal)
3. [System Architecture](#system-architecture)
4. [Module Reference](#module-reference)
5. [Risk Management](#risk-management)
6. [PerformanceSkill — Conviction Scoring](#performanceskill--conviction-scoring)
7. [BayesianLearner — Adaptive Intelligence](#bayesianlearner--adaptive-intelligence)
8. [Guardian — Position Management](#guardian--position-management)
9. [Pair Hunter — Dynamic Discovery](#pair-hunter--dynamic-discovery)
10. [Configuration Reference](#configuration-reference)
11. [Installation & Quick Start](#installation--quick-start)
12. [Usage Guide](#usage-guide)
13. [Logging & Monitoring](#logging--monitoring)
14. [File Reference](#file-reference)
15. [Changelog](#changelog)

---

## Overview

OBR is a fully autonomous crypto trading bot running 24/7 on **Bybit mainnet** (USDT perpetual futures). It detects Outside Bar Reversal patterns on the 5-minute timeframe, scores each setup through a multi-factor conviction engine, and self-tunes its entry threshold using Bayesian learning.

**Key facts:**

| Property | Value |
|---|---|
| Exchange | Bybit (mainnet, USDT perpetual swap) |
| Strategy | Outside Bar Reversal (contrarian fade) |
| Signal TF | 5m |
| Execution TF | 1m polling |
| Leverage | 10x isolated margin |
| Risk per trade | 5% of equity (≥$100) / 10% (<$100) |
| Max concurrent | 5 positions |
| Confirmation | NEXTBAR (candle after OB must confirm direction) |
| TP | Per-pair (1.0R–2.5R), Guardian-managed |
| Daily cap | 15% growth → pause new entries |
| Growth target | $100 → $1,000 (x10) in 30 days |

**Autonomous features:**
- Supervisor auto-healer with exponential backoff restart
- WebSocket candle streaming (zero REST for subscribed pairs)
- Full-market Pair Hunter scanning ~100+ liquid pairs every cycle
- PerformanceSkill conviction scoring (0–100) with agentic recalibration
- BayesianLearner tracking 15 discrete features across all trades
- PairDNA profiling (hot/warm/cold status per symbol)
- Progressive SL tiers + trailing stop via Guardian daemon
- Deposit/withdrawal detection preventing false daily-cap triggers

---

## The Strategy — Outside Bar Reversal

### What Is an Outside Bar?

An **outside bar** is a candle whose range completely engulfs the previous candle — its high is above the prior high AND its low is below the prior low. It represents a moment of extreme expansion where both buyers and sellers are trapped.

### OBR Signal Logic

The strategy fades the extreme close of an outside bar, betting that the move has exhausted itself.

#### LONG Signal (signal = 2)

```
Conditions (ALL must be true):
  1. Current candle is BEARISH (open > close)
  2. Current high > previous high           ← engulfs above
  3. Current low  < previous low            ← engulfs below
  4. Current close < previous low           ← extreme close BELOW prior range

→ Bearish exhaustion detected → Enter LONG (fade the bears)
→ Stop loss = current candle low
```

#### SHORT Signal (signal = 1)

```
Conditions (ALL must be true):
  1. Current candle is BULLISH (open < close)
  2. Current low  < previous low            ← engulfs below
  3. Current high > previous high           ← engulfs above
  4. Current close > previous high          ← extreme close ABOVE prior range

→ Bullish exhaustion detected → Enter SHORT (fade the bulls)
→ Stop loss = current candle high
```

### NEXTBAR Confirmation

After an OB signal fires, the bot waits for the **next candle** to confirm the reversal:

- **LONG confirmation:** Next candle closes bullish (close > open)
- **SHORT confirmation:** Next candle closes bearish (close < open)

If confirmation fails, the signal is discarded.

### Trade Computation

```
Entry    = Market price at execution
SL       = OB candle extreme (low for longs, high for shorts)
Risk/unit = |entry − SL|
TP       = entry ± (tp_r × risk/unit)    [per-pair TP: 1.0R–2.5R]
Size     = (equity × risk_pct) / risk/unit × leverage
Fee est  = (FEE_RATE × 2 × price) / risk/unit   [in R terms]
```

**Safety caps:**
- Margin cap: max notional = (equity × 0.90 / max_positions) × leverage
- Min notional: $5
- Min risk: $0.20
- Min SL distance: 0.1% of price

---

## System Architecture

```
run_obr.py                          ← Entry point (CLI)
  └── OBRSupervisor                 ← Process watchdog + auto-healer
        └── OBRBot                  ← Main trading loop (subprocess)
              ├── config.py         ← Central configuration
              ├── exchange.py       ← Bybit ccxt wrapper (REST + retry)
              ├── strategy.py       ← OBR signal detection (pure functions)
              ├── BotState          ← Persistent state + trade limits
              ├── Guardian          ← Daemon: SL/TP/trail management
              │     └── exchange    ← set_trading_stop, fetch_closed_pnl
              ├── PairHunter        ← Full-market OBR scanner
              │     └── exchange    ← fetch_ohlcv, fetch_tickers
              ├── WSCandleCache     ← ccxt.pro WebSocket streams
              ├── PerformanceSkill  ← Conviction scoring + self-tuning
              │     ├── Key Level Engine
              │     ├── Conviction Scorer (5 dimensions)
              │     └── BayesianLearner
              │           ├── BetaTracker    ← Beta(α,β) per feature
              │           ├── PairDNA        ← Per-symbol profiling
              │           └── extract_features()
              ├── OBRTracker        ← Growth dashboard
              ├── logger            ← 3-channel logging (console/file/audit)
              └── trade_logger      ← JSONL event stream
```

### Main Loop (24/7 Continuous)

```
1. check_new_day()           → Reset daily counters on UTC midnight
2. Check daily growth cap    → Pause if ≥ 15% growth today
3. wait_for_candle_close()   → Align to 5m candle boundary + 2s safety
4. Equity floor check        → Halt if equity < 60% of peak
5. Scan static pairs         → For each: fetch candles → detect OBR → skill score → execute
6. Pair Hunter               → Scan liquid market for additional A+ signals
7. Heartbeat log             → Every 5 minutes
8. Skill status              → Every 12 cycles (~1 hour)
```

---

## Module Reference

### `strategy.py` — Signal Detection

Pure functions with zero side effects. No exchange calls, no state mutation.

| Function | Purpose |
|---|---|
| `detect_outside_bar(prev, current)` | Core OB detection → returns 0, 1 (short), or 2 (long) |
| `check_nextbar_confirmation(signal_type, confirm_candle)` | Validates reversal follow-through |
| `scan_for_signal(symbol, candles, require_confirmation)` | Full scan pipeline → returns `OBRSignal` or `None` |
| `compute_trade(signal, price, equity, ...)` | Calculates entry/SL/TP/size → returns `TradeSignal` |

**Data classes:** `CandleData` (OHLCV + properties), `OBRSignal` (signal details), `TradeSignal` (executable trade)

### `bot.py` — Main Bot (OBRBot)

The 24/7 trading loop. Connects to Bybit, sets up pairs, starts all daemons, then scans continuously.

**Key methods:**

| Method | Purpose |
|---|---|
| `run()` | Startup sequence → infinite scan loop |
| `_connect()` | Create exchange, verify equity |
| `_setup_pairs()` | Set leverage + isolated margin for all static pairs |
| `_scan_pair()` | Fetch candles → signal detection → skill scoring → trade computation |
| `_execute_trade()` | Round precision → place market order → register with Guardian |
| `_process_hunted()` | Handle dynamically discovered pairs from PairHunter |
| `_on_position_closed()` | Callback from Guardian → record outcome → feed learning loop |

### `exchange.py` — Bybit Integration

Authenticated `ccxt.bybit` wrapper for USDT perpetual (swap) trading.

**All functions decorated with `@timed_api`:** Auto-retry on rate limits (exponential backoff: 1s → 2s → 4s), timing logs, error categorisation.

| Function | Purpose |
|---|---|
| `create_exchange()` | Authenticated ccxt.bybit instance |
| `get_equity()` | USDT total balance |
| `set_leverage()` / `set_margin_mode()` | Position setup |
| `fetch_latest_candles()` | N closed candles (drops forming candle) |
| `place_market_order()` | Market order with native SL/TP |
| `set_trading_stop()` | Update SL/TP on existing position |
| `close_position()` | Market close via reduceOnly |
| `fetch_closed_pnl()` | Bybit v5 closed PnL records |
| `get_market_info()` / `round_price()` / `round_qty()` | Precision helpers |

### `state.py` — Persistent State (BotState)

Thread-safe state with atomic JSON writes.

**Trade-gating checks (`can_trade()`):**
- Max concurrent positions (5)
- No duplicate pair
- Daily growth cap (15%)
- Pair cooldown (10 minutes)
- Per-pair daily limit (5 trades)
- Consecutive loss cooldown (2 losses → 4 hour pause)
- Daily total limit (120 trades)

**Deposit detection:** If equity jumps >30% in one check with >$5 absolute delta, automatically resets `day_start_equity` to prevent false daily-cap triggers.

### `ws_cache.py` — WebSocket Candle Cache

Zero REST API calls for subscribed pairs using `ccxt.pro` async WebSocket.

- Background thread with dedicated asyncio event loop
- Thread-safe cache: `symbol → list[candle_dict]` (max 35 candles)
- `add_symbol()` for dynamic subscription of hunted pairs
- Auto-reconnection on WebSocket errors
- Only stores closed candles (drops forming candle)

### `tracker.py` — Growth Tracker

Tracks equity progress toward the x10 target. ASCII dashboard with ANSI colors.

```
╭────────────────────────────────────────────╮
│  🏆 OBR Growth Tracker                    │
├────────────────────────────────────────────┤
│  💸 Start: $100   🎯 Target: $1000  x10   │
│  💎 Equity: $107.96   ⭐ Peak: $107.96    │
│  📈 Growth: +8.0%   📉 DD: 0.0%          │
│  ⏰ Day 0.0/30   🏃 Pace: Day 0          │
│  🔥 Need: 7.7%/day                        │
├────────────────────────────────────────────┤
│  [░░░░░░░░░░░░░░░░░░░░░░░░░] 0.9%        │
├────────────────────────────────────────────┤
│  📊 Trades: 0  ✅ W:0  ❌ L:0  WR:0%     │
│  ⚡ Total R: +0.0                          │
╰────────────────────────────────────────────╯
```

### `trade_logger.py` — JSONL Event Stream

Structured event log for post-session analysis.

**Events:** ENTRY, EXIT, GUARDIAN_UPDATE, TRAIL_ACTIVATE, HEARTBEAT, ERROR

**File:** `obr/logs/events_YYYY-MM-DD.jsonl` (daily rotation)

---

## Risk Management

### Dynamic Risk Tiers

| Equity | Risk % | Rationale |
|---|---|---|
| < $100 | 10% | Aggressive growth phase |
| ≥ $100 | 5% | Capital preservation |

### Position Limits

| Limit | Value |
|---|---|
| Max concurrent | 5 |
| Max trades/day | 120 |
| Max trades/pair/day | 5 |
| Pair cooldown | 10 minutes |
| Consecutive loss pause | 2 losses → 4 hour cooldown |

### Safety Rails

| Rail | Trigger | Action |
|---|---|---|
| Daily growth cap | +15% from day start | Pause new entries |
| Equity floor | Equity < 60% of peak | Halt all trading |
| Min SL distance | < 0.1% of price | Reject trade |
| Min notional | < $5 | Reject trade |
| Min risk | < $0.20 | Reject trade |
| Margin cap | Notional > (equity×0.9/positions)×leverage | Reduce size |

---

## PerformanceSkill — Conviction Scoring

Every signal is scored 0–100 across 5 dimensions before execution. Only signals meeting `min_conviction` (default: 40) are traded.

### Scoring Dimensions

| Dimension | Max Pts | What It Measures |
|---|---|---|
| Key level proximity | 30 | Distance to nearest structural level + confluence count |
| OB candle quality | 25 | Body/range ratio + engulfment magnitude + range % |
| Volume context | 15 | OB volume vs 20-candle moving average |
| Trend alignment (HTF) | 15 | Is the reversal back INTO the prevailing trend? |
| Fee efficiency | 15 | Fee cost as fraction of risk (lower = better) |

### Key Level Engine

Detects structural levels from the same candle data (zero extra API calls):

- **Swing highs/lows** — Local extremes (order=3)
- **Floor pivots** — Classic P, R1, S1, R2, S2 from 1h aggregation
- **Round numbers** — Psychological levels adapted to price magnitude
- **VWAP approximation** — Typical price × volume weighted
- **Session levels** — Previous session high/low/close

**Confluence scoring:** Each nearby level (within 0.25% proximity) adds +1.5 bonus points (cap 5).

### Grades

| Grade | Score | Meaning |
|---|---|---|
| A+ | ≥ 80 | Exceptional — all dimensions strong |
| A | ≥ 65 | High confidence |
| B | ≥ 50 | Standard quality |
| C | ≥ 35 | Below threshold (rejected at default min=40) |
| D | < 35 | Poor quality (always rejected) |

### Agentic Self-Tuning

Every 10 closed outcomes, the skill recalibrates its `min_conviction` threshold:

1. Walk conviction buckets from high → low
2. Find threshold where WR ≥ 45% AND net R is positive
3. Shift `min_conviction` gradually (max ±5 per recalibration)
4. Floor: 25, Ceiling: 75

Memory persisted to `obr/logs/skill_memory.json` (last 500 outcomes, bucket stats, grade distributions).

---

## BayesianLearner — Adaptive Intelligence

Pure-Python Bayesian inference using Beta-distribution conjugate priors. Learns which setup features predict wins from real trade outcomes.

### How It Works

Each discrete feature value gets a `Beta(α, β)` tracker:
- **Prior:** Beta(1, 1) = uniform (no opinion)
- **Win:** α += 1
- **Loss:** β += 1
- **Posterior WR:** α / (α + β)
- **Edge:** posterior − 0.5 (positive = feature predicts wins)

### 15 Tracked Features

| # | Feature | Values |
|---|---|---|
| 1 | direction | long, short |
| 2 | body_ratio | huge, large, medium, small, tiny |
| 3 | ob_range | huge, large, medium, small (vs ATR) |
| 4 | volume | spike, high, avg, low |
| 5 | trend | with_trend, counter_trend, flat, neutral |
| 6 | fee_tier | great, good, fair, poor |
| 7 | key_level_type | swing, pivot, round, session, vwap, none |
| 8 | key_level_prox | touching, near, moderate, far |
| 9 | price_magnitude | micro, sub_dime, sub_dollar, single_digit, tens, hundreds, thousands |
| 10 | confirm_strength | strong, moderate, weak |
| 11 | session | asia, london, overlap, ny, late |
| 12 | hour_block | h00_03, h04_07, h08_11, h12_15, h16_19, h20_23 |
| 13 | day | mon–sun |
| 14 | trend_x_dir | composite (e.g. with_trend_long) |
| 15 | session_x_dir | composite (e.g. london_short) |

### Conviction Adjustment

For each feature of a new signal:
- If confidence ≥ 3 observations: `weight = min(confidence, 20) / 20`
- `weighted_edge = edge × weight`
- Average of all weighted edges → scaled to **−10 to +12 points**

This adjustment is added to the base conviction score from PerformanceSkill.

### PairDNA — Per-Symbol Profiling

Each traded symbol gets its own Beta tracker + performance stats:

| Status | Condition | Adjustment |
|---|---|---|
| Hot | WR > 58% (min 3 trades) | +3 conviction points |
| Warm | Between hot and cold | 0 |
| Cold | WR < 38% (min 3 trades) | −5 conviction points |
| Unknown | < 3 trades | 0 |

**Persistence:** `obr/logs/learner_memory.json`

---

## Guardian — Position Management

Daemon thread polling all open positions every 15 seconds.

### Progressive SL Tiers

| Profit Level | New SL | Effect |
|---|---|---|
| ≥ 0.3R | −0.6R from entry | Reduce max loss |
| ≥ 0.6R | Breakeven (entry) | Risk-free trade |
| ≥ 1.0R | +0.4R from entry | Lock 0.4R profit |
| ≥ 1.5R | +0.8R from entry | Lock 0.8R profit |

### Trailing Stop

- **Activates at:** 1.0R profit
- **Trail distance:** 0.3R behind peak
- **API throttle:** Only updates if move ≥ 0.1R (reduces API calls)
- **Exchange TP:** Set at 10R (far out) — Guardian manages the real exit

### Position Resolution

When a position disappears (closed by exchange SL/TP or Guardian):
1. Wait 1.5s for Bybit settlement
2. Query closed PnL endpoint for actual results
3. Match by entry price (within 0.5%)
4. Fallback: estimate from tracked peak_r

---

## Pair Hunter — Dynamic Discovery

Every 5m cycle, scans ALL liquid Bybit USDT perpetual markets for active OBR signals beyond the static pair list.

### Universe

- Refreshes every 60 minutes
- Bulk ticker fetch (1 API call for ~500 pairs)
- Filters: 24h volume ≥ $3M, spread ≤ 0.15%
- Caps to top 60 candidates by volume

### Quality Gates

| Filter | Threshold |
|---|---|
| OB range | ≥ 0.20% of price |
| Fee cost | ≤ 0.25R |
| NEXTBAR confirmation | Required |

Results sorted by OB range (wider = higher quality). Max 10 results per cycle.

---

## Configuration Reference

All settings in `obr/config.py`:

### Core

| Setting | Default | Description |
|---|---|---|
| `MAINNET` | `True` | Real money mode |
| `SIGNAL_TIMEFRAME` | `"5m"` | OBR signal detection timeframe |
| `TIMEFRAME` | `"1m"` | Execution poll interval |
| `LEVERAGE` | `10` | Isolated margin leverage |
| `FEE_RATE` | `0.0002` | Bybit VIP0 maker fee (0.02%) |
| `TP_R` | `2.0` | Default take-profit in R-multiples |
| `REQUIRE_NEXTBAR_CONFIRM` | `True` | Require confirmation candle |

### Risk

| Setting | Default | Description |
|---|---|---|
| `RISK_PCT` | `0.10` | Risk per trade (equity < $100) |
| `RISK_PCT_ABOVE_100` | `0.05` | Risk per trade (equity ≥ $100) |
| `RISK_TIER_THRESHOLD` | `$100.0` | Tier switch point |
| `MAX_CONCURRENT_POSITIONS` | `5` | Max simultaneous positions |
| `MAX_TRADES_DAY` | `120` | Daily trade limit |
| `DAILY_GROWTH_CAP_PCT` | `15.0` | Pause entries after this daily growth |
| `EQUITY_FLOOR_PCT` | `0.60` | Halt if equity < 60% of peak |

### Guardian

| Setting | Default | Description |
|---|---|---|
| `TRAIL_ENABLED` | `True` | Enable trailing stop |
| `TRAIL_ACTIVATION_R` | `1.0` | Activate trail at this R-profit |
| `TRAIL_DISTANCE_R` | `0.30` | Trail distance behind peak |
| `TRAIL_MIN_MOVE_R` | `0.10` | Min move before API update |
| `EXCHANGE_TP_R` | `10.0` | Far-out exchange-side TP |
| `GUARDIAN_POLL_SECS` | `15` | Position check interval |

### Pairs

| Setting | Default | Description |
|---|---|---|
| `STATIC_PAIRS` | C98, AWE, HBAR, SNX, GRT, JUP | Dedicated scanning pairs |
| `ALWAYS_TRADE` | C98, AWE, HBAR, SNX | Proven winners (skip liquidity check) |
| `HUNTER_ENABLED` | `True` | Full-market scanning |
| `HUNTER_MAX_RESULTS` | `10` | Max hunted signals per cycle |
| `MIN_TURNOVER_USDT` | `$2,000,000` | Min 24h volume for static pairs |

### Sessions

| Session | UTC Hours |
|---|---|
| Asia | 00:00–08:00 |
| London | 08:00–16:00 |
| New York | 16:00–24:00 |

### Growth Target

| Setting | Default | Description |
|---|---|---|
| `START_EQUITY` | `$100.0` | Starting baseline |
| `TARGET_EQUITY` | `$1,000.0` | Target (x10) |
| `TARGET_DAYS` | `30` | Target timeframe |

---

## Installation & Quick Start

### Prerequisites

- Python 3.11+
- Bybit API key with trading permissions
- Internet connection for exchange API

### Setup

```bash
# Clone and enter directory
cd anewBot

# Create virtual environment
python -m venv .venv

# Activate (Windows)
.venv\Scripts\Activate.ps1

# Install dependencies
pip install ccxt     # Exchange API (REST)
pip install ccxt[pro]  # WebSocket support (ccxt.pro)
```

### Environment Variables

```bash
# Set Bybit API credentials
$env:BYBIT_API_KEY = "your_api_key"
$env:BYBIT_API_SECRET = "your_api_secret"
```

### Launch

```bash
# Supervised mode (recommended — auto-restart on crash)
python run_obr.py --yes

# Direct mode (no supervisor, debug output)
python run_obr.py --bare --yes

# Check status
python run_obr.py --status

# View growth tracker
python run_obr.py --tracker

# View error digest (last 24h)
python run_obr.py --errors
```

---

## Usage Guide

### CLI Flags

| Flag | Purpose |
|---|---|
| `--yes` | Auto-confirm startup (skip prompt) |
| `--bare` | Direct mode — no supervisor wrapper |
| `--status` | Print current state and exit |
| `--tracker` | Print growth dashboard and exit |
| `--errors [N]` | Show error digest for last N hours (default: 24) |
| `--backtest` | Run historical backtest |

### Supervised Mode (Default)

The `OBRSupervisor` wraps the bot as a subprocess and provides:

- **Crash detection** — Automatically restarts on unexpected exit
- **Anti-flap** — Exponential backoff: 10s, 30s, 60s, 120s, 300s
- **Heartbeat monitoring** — Detects frozen bot (600s stale → warning, 1200s → force restart)
- **Error classification** — Fatal errors (auth, no funds) block restart; transient errors (network, rate limit) allow restart
- **Position safety** — Checks exchange for open positions before restart
- **Incident logging** — All events in `obr/logs/incidents.jsonl`
- **Max restarts** — 6 per hour anti-flap limit
- **Stable run reset** — If bot runs > 120s, crash counter resets

### State Files

| File | Purpose | Auto-created |
|---|---|---|
| `obr/state.json` | Bot state, equity, trade counters | Yes |
| `obr/tracker.json` | Growth tracker snapshots | Yes |
| `obr/logs/skill_memory.json` | PerformanceSkill memory | Yes |
| `obr/logs/learner_memory.json` | BayesianLearner memory | Yes |

### Resetting

To start fresh:
```bash
# Delete state files
Remove-Item obr/state.json, obr/tracker.json -Force
Remove-Item obr/logs/skill_memory.json, obr/logs/learner_memory.json -Force

# Update config.py START_EQUITY and TARGET_EQUITY as needed
# Restart bot
python run_obr.py --yes
```

---

## Logging & Monitoring

### 3 Log Channels

| Channel | Level | File | Purpose |
|---|---|---|---|
| Console | INFO+ | stdout | Live monitoring with ANSI colors |
| Bot log | DEBUG+ | `obr/logs/bot_YYYYMMDD.log` | Full debug trace (daily rotation) |
| Audit | INFO | `obr/logs/audit_YYYYMMDD.log` | Trades and orders only |

### Additional Logs

| File | Purpose |
|---|---|
| `obr/logs/events_YYYY-MM-DD.jsonl` | Structured trade events (ENTRY, EXIT, GUARDIAN_UPDATE, etc.) |
| `obr/logs/incidents.jsonl` | Supervisor incidents (crashes, restarts, errors) |
| `obr/logs/supervisor.log` | Supervisor process log |
| `obr/trades.csv` | CSV trade log |

### Heartbeat

Every 5 minutes, the bot logs:
```
💓 HEARTBEAT │ Equity=$107.96 │ Open=2 │ Session=london
```

The supervisor monitors this — if no heartbeat for 600s, it warns. At 1200s, it force-restarts.

---

## File Reference

```
obr/
├── __init__.py          # Package marker
├── config.py            # All configuration constants
├── strategy.py          # OBR signal detection (pure functions)
├── bot.py               # Main 24/7 trading loop (OBRBot)
├── exchange.py          # Bybit ccxt wrapper with retry
├── state.py             # Persistent state + trade limits (BotState)
├── guardian.py           # Position management daemon (Guardian)
├── skill.py             # Conviction scoring + self-tuning (PerformanceSkill)
├── learner.py           # Bayesian feature learning (BayesianLearner)
├── ws_cache.py          # WebSocket candle cache (WSCandleCache)
├── pair_hunter.py       # Dynamic pair discovery (PairHunter)
├── tracker.py           # Growth dashboard (OBRTracker)
├── supervisor.py        # Auto-healer process manager (OBRSupervisor)
├── logger.py            # 3-channel ANSI logging
├── trade_logger.py      # JSONL event stream
├── backtest.py          # Historical backtester
├── pair_scanner.py      # Pair quality scanner
├── state.json           # [generated] Bot state
├── tracker.json         # [generated] Tracker snapshots
├── trades.csv           # [generated] Trade log
├── scan_results.json    # [generated] Scanner output
├── a_grade_scan.json    # [generated] A-grade pair scans
├── a_grade_deep.json    # [generated] Deep pair analysis
└── logs/
    ├── bot_YYYYMMDD.log           # Daily bot log (DEBUG+)
    ├── audit_YYYYMMDD.log         # Daily audit log
    ├── events_YYYY-MM-DD.jsonl    # Daily trade events
    ├── incidents.jsonl             # Supervisor incidents
    ├── supervisor.log              # Supervisor log
    ├── skill_memory.json           # PerformanceSkill state
    └── learner_memory.json         # BayesianLearner state
```

---

## Changelog

### 2026-02-22 (x1000 Upgrade)
- **Mod 1:** Dynamic Risk Curve — `get_risk_pct()` walks `RISK_CURVE` table (10%→1.5% as equity grows $100→$100K)
- **Mod 2:** Adaptive Leverage — `get_leverage()` walks `LEVERAGE_CURVE` (10x→2x)
- **Mod 3:** Conviction-Scaled Sizing — `CONVICTION_MULTIPLIER` adjusts risk by grade (A+=1.25×, C=0.80×)
- **Mod 4:** Phased Growth Targets — `GROWTH_PHASES` with per-phase daily caps (20%→6%), shown in tracker dashboard
- **Mod 5:** Regime Detection — new `regime.py` with `classify_regime()` (trending/ranging/volatile), thread-safe `RegimeCache`
- **Mod 6:** Drawdown Throttle — `DRAWDOWN_THROTTLE` reduces risk in drawdown (1.0×→0.10×)
- **Mod 7:** Dynamic TP — `get_dynamic_tp()` adjusts TP by conviction grade + market regime
- **Mod 8:** Withdrawal Milestones — `WITHDRAWAL_MILESTONES` alerts at $500, $1K, $5K, $10K, $25K, $50K, $100K
- **Mod 9:** Enhanced Learner — 3 new features: `market_regime`, `equity_phase`, `drawdown_zone`
- **Mod 10:** Dynamic Max Concurrent — `MAX_CONCURRENT_CURVE` scales positions 5→15 with equity
- **Risk chain:** `base_risk × conv_mult × dd_mult → final_risk (capped 15%)`
- **Target updated:** $100 → $100K (x1000), 365 days
- All mods additive, backwards-compatible, try/except with safe fallbacks

### 2026-02-22
- **Created** OBR README documenting full system
- **Fixed** heartbeat log level (DEBUG → INFO) so supervisor detects it via stdout
- **Fixed** deposit detection in `state.py` — prevents false daily-cap triggers after capital additions
- **Reset** growth tracker: $100 → $1,000 (x10) target

### 2026-02-21
- **Created** `learner.py` — BayesianLearner with Beta-distribution priors, 15 features, PairDNA
- **Integrated** learner into `skill.py` (conviction adjustment) and `bot.py` (banner + outcome recording)
- **Fixed** `_classify_key_level()` to work with actual `detect_key_levels` return structure

### 2026-02-20
- **Created** `skill.py` — PerformanceSkill with key-level engine, 5-dimension conviction scorer, agentic self-tuning

### Earlier
- Core OBR system: strategy, bot, guardian, exchange, state, ws_cache, pair_hunter, tracker, supervisor, logger
- 6 static pairs selected from scanner results
- WebSocket candle caching for zero REST overhead
- PairHunter full-market scanning
- Supervisor auto-healer with exponential backoff

---

> **Note:** This README should be kept updated after any code changes. It serves as the single source of truth for the OBR system architecture and configuration.
