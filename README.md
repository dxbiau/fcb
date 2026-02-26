# FCB — v13pro Multi-Strategy Trading System

## Live Trading Bot for Bybit Perpetual Futures

> **12 strategies. Conviction scoring. DNA profiling. Hunter scalps.**
> Data-driven edge discovery. Every signal tracked. Every filter proven by shadow data.

---

## Table of Contents

1. [What Is This?](#what-is-this)
2. [System Architecture](#system-architecture)
3. [The 12 Strategies](#the-12-strategies)
4. [How It Works — Step by Step](#how-it-works--step-by-step)
5. [Intelligence Modules](#intelligence-modules)
6. [Hunter Scalp System](#hunter-scalp-system)
7. [Guardian — Position Management](#guardian--position-management)
8. [Risk Management](#risk-management)
9. [Configuration Reference](#configuration-reference)
10. [Project Structure](#project-structure)
11. [Installation](#installation)
12. [Quick Start](#quick-start)
13. [Research & Analysis Tools](#research--analysis-tools)
14. [FAQ](#faq)
15. [Change Log](#change-log)

> **All code changes are tracked in [CHANGELOG.md](CHANGELOG.md)** — check there for what was added, modified, or deleted.

---

## What Is This?

A **fully automated live trading bot** running on **Bybit perpetual futures (mainnet)** using a 12-strategy ensemble engine with conviction scoring, DNA profiling, and autonomous pair discovery.

Originally built as a single-strategy FCB (First Candle Breakout) system, v13pro evolved into a multi-strategy, multi-timeframe architecture where every signal is scored, every trade is profiled, and every rejected signal is shadow-tracked for continuous improvement.

### What it does:

| Capability | Description |
|---|---|
| **12-Strategy Ensemble** | EMA_RIB, BB_BREAK, DONCHIAN, RSI_FADE, BB_FADE, STOCH_X, PIN_BAR, IB_BREAK, ENGULF, MTF_RSI, TR_PULL, MOM_SURGE |
| **Conviction Scoring** | 0-100 scale (A+/A/B/C/D/X) via key-level proximity, candle quality, volume context, trend alignment, fee efficiency |
| **DNA Profiling** | 25 raw numeric features per trade → statistical edge discovery for winning conditions |
| **Combo Registry** | 40 curated combos across 28 pairs, 3 timeframes (15m, 30m, 1h) — each with validated PF/WR/R metrics |
| **Hunter Scalp System** | Autonomous scanner discovers signals on non-portfolio pairs, takes quick 0.75R profits with separate slot pool |
| **Shadow Trader** | Tracks ALL signals (traded + rejected) passively — the data goldmine for filter optimisation |
| **Sentiment Gauge** | Real-time BTC/ETH/SOL momentum bias (BULL/BEAR/NEUTRAL) — blocks losing counter-trend trades |
| **Orderflow Intel** | L2 orderbook microstructure (spread, imbalance, depth ratio) — boosts conviction when aligned |
| **Guardian** | Progressive SL tiers + trailing stop + 1m rejection exit + funding rate protection |
| **Pair Hunter** | Scans entire Bybit universe every 5 min for fresh opportunities outside the portfolio |
| **WebSocket Data** | Zero REST polling — multi-TF candle buffers via ccxt.pro batch subscriptions |
| **Watchdog** | System health monitor: network, memory (500MB soft limit), WS staleness, disk, equity |

### Current Live Stats (Feb 27, 2026):

| Metric | Value |
|---|---|
| **Equity** | ~$482.66 |
| **Peak Equity** | ~$567.28 |
| **Drawdown** | 14.9% |
| **All-Time Trades** | 218 (72W / 146L) |
| **Win Rate** | 33.0% all-time |
| **Active Combos** | 38 (from 40, 2 delisted pair skips) |
| **Pairs** | 28 portfolio + universe scanning |
| **Timeframes** | 15m, 30m, 1h |
| **Leverage** | 8x (equity-curve scaled) |
| **Risk** | 2% per trade (conviction-weighted, regime-scaled) |
| **Max Concurrent** | 6 portfolio + 5 hunter (independent pools) |
| **Margin** | Isolated per position |
| **Regime** | COOL (0.70x global mult) |
| **Sentiment** | BEAR (score=-0.249) |

### What it is NOT:

- **Not the original FCB system** — v13pro is a complete rewrite with 12 strategies, not just First Candle Breakout
- **Not a black box** — every rule is explicit, every filter proven by shadow data
- **Not an optimiser** — combos are discovery-validated, not curve-fit
- **Not financial advice** — this is software for educational and research purposes

---

## System Architecture

```
+------------------------------------------------------------------+
|                     v13pro Bot (asyncio)                         |
+------------------------------------------------------------------+
|                                                                  |
|  Entry: python -m v13pro.run --maker --entry                     |
|                                                                  |
|  +-----------+  +------------+  +-----------+  +-------------+   |
|  | WSData    |  | Strategies |  | Registry  |  | Skill/DNA   |   |
|  | (candle   |  | (12 algos  |  | (40 combos|  | (conviction |   |
|  |  buffers) |  |  + ensembl)|  |  + exits) |  |  0-100 pts) |   |
|  +-----------+  +------------+  +-----------+  +-------------+   |
|       |              |              |                |            |
|  +----v--------------v--------------v----------------v------+    |
|  |                    Bot Orchestrator                       |    |
|  |  - Scans combos every candle close                        |    |
|  |  - Scores conviction (key levels, quality, volume, trend) |    |
|  |  - Applies filters (sentiment, OB alignment, DNA)         |    |
|  |  - Routes: portfolio vs hunter risk paths                 |    |
|  +--+-----------+------------+-----------+---+---+----------+    |
|     |           |            |           |   |   |               |
|  +--v---+  +---v----+  +---v----+  +---v-+ | +--v--------+     |
|  |Exch  |  |Guardian|  |Hunter  |  |State| | |Shadow     |     |
|  |(Bybit|  |(SL/TP  |  |(univ   |  |(json| | |(passive   |     |
|  | ccxt)|  | trail  |  | scan   |  | per | | | tracking) |     |
|  +------+  | fund)  |  | scalps)|  | sist| | +-----------+     |
|            +--------+  +--------+  +-----+ |                    |
|                                             |                    |
|  +--v--------+  +-----------+  +-----------+                    |
|  |Sentiment  |  |OrderFlow  |  |Watchdog   |                    |
|  |(BTC/ETH/  |  |(L2 book   |  |(health,   |                    |
|  | SOL bias) |  | snapshots)|  | memory)   |                    |
|  +-----------+  +-----------+  +-----------+                    |
+------------------------------------------------------------------+
```

### Module Responsibilities

| Module | File | Purpose |
|---|---|---|
| **Bot** | `v13pro/bot.py` | Main async orchestrator: combo scanning, conviction scoring, signal routing, order execution |
| **Strategies** | `v13pro/strategies.py` | 12 strategy algorithms + ensemble signal generation |
| **Registry** | `v13pro/registry.py` | Combo registry loader + exit parameter definitions (fix/trail modes) |
| **Skill** | `v13pro/skill.py` | Multi-factor conviction scorer: key levels (30pts), candle quality (25pts), volume (15pts), trend (15pts), fee efficiency (15pts) |
| **DNA** | `v13pro/dna.py` | Setup DNA profiler: 25 raw numeric features → correlation analysis for winning conditions |
| **Exchange** | `v13pro/exchange.py` | Bybit async API wrapper via ccxt |
| **Guardian** | `v13pro/guardian.py` | Position guard: progressive SL tiers, trailing stop, 1m rejection/engulfing exit, funding rate monitor |
| **Hunter** | `v13pro/hunter.py` | Async pair universe scanner: bulk tickers → volume/spread filter → strategy scan → signal callback |
| **State** | `v13pro/state.py` | Thread-safe persistent state (JSON): equity, trades, cooldowns, daily counters, separate `can_trade()` / `can_trade_hunter()` paths |
| **Config** | `v13pro/config.py` | All parameters: risk/leverage/concurrent curves, profit tiers, hunter config, session schedule |
| **WSData** | `v13pro/ws_data.py` | Async WebSocket multi-TF candle buffers: batch subscriptions, event-driven candle close |
| **Sentiment** | `v13pro/sentiment.py` | BTC/ETH/SOL momentum gauge: EMA slopes, structure, aggregate bias + confidence |
| **OrderFlow** | `v13pro/orderflow.py` | L2 orderbook snapshots: spread, imbalance, depth ratio — conviction boost when aligned |
| **Shadow** | `v13pro/shadow.py` | Passive shadow trader: simulates ALL signals, tracks peak/trough/checkpoints, writes JSONL |
| **Watchdog** | `v13pro/watchdog.py` | Health monitor: network, memory (500MB cap), WS staleness, disk, equity snapshots |
| **Indicators** | `v13pro/indicators.py` | Technical indicator calculations (EMA, BB, RSI, Stoch, Donchian, ATR, VWAP, etc.) |
| **Journal** | `v13pro/journal.py` | Trade journal with full context |
| **Trade Logger** | `v13pro/trade_logger.py` | Structured JSONL trade logger |
| **Preflight** | `v13pro/preflight.py` | Pre-flight system checks (exchange, balance, pairs, leverage) |
| **Aftermath** | `v13pro/aftermath.py` | Post-trade analysis tracker |
| **Learner** | `v13pro/learner.py` | Adaptive learning from trade outcomes |
| **Regime** | `v13pro/regime.py` | Self-calibrating regime detector: HOT/WARM/NORMAL/COOL/COLD with session-specific multipliers from rolling shadow data |
| **DirectionalIntel** | `v13pro/directional.py` | Adaptive directional gate: decides which sides (long/short) are profitable per sentiment regime, replacing static LONG_ONLY_MODE |
| **EdgeRadar** | `v13pro/edge_radar.py` | Full shadow intelligence: combo heat (HOT/WARM/COLD/FROZEN), market heat, sentiment edge, hot seat detection — 4 risk multipliers |
| **Adaptive** | `v13pro/adaptive.py` | Data-driven parameter adaptation from shadow outcomes (OF thresholds, grade mults, cooldowns, TP_R) |
| **SignalQuality** | `v13pro/signal_quality.py` | Per-signal quality scoring from shadow outcome data |
| **Calibrator** | `v13pro/calibrator.py` | Self-calibrator: edge health, stationarity, conviction correlation, grade-specific adjustments |
| **Lifecycle** | `v13pro/lifecycle.py` | Per-pair lifecycle tracking: expanding/compressing/drifting states with risk multipliers |
| **CrossSectional** | `v13pro/cross_sect.py` | Cross-sectional awareness: entry clustering + loss clustering detection |
| **Burst** | `v13pro/burst.py` | Burst engine: BCS scoring, BURST/NORMAL/DECAY states with dynamic risk/leverage/TP scaling |
| **BurstOptimizer** | `v13pro/burst_optim.py` | Phase 2A iterative self-tuning of burst engine parameters |
| **ComboPromoter** | `v13pro/combo_promoter.py` | Auto-promotes winning shadow combos to live portfolio |
| **Supervisor** | `v13pro/supervisor.py` | Bot supervisor / restart logic |

---

## The 12 Strategies

v13pro runs a **12-strategy ensemble** where each strategy independently scans candle data and emits directional signals. Signals are then scored by the conviction engine and filtered through sentiment/OB/DNA layers.

| Strategy | Code | Logic |
|---|---|---|
| **EMA Ribbon** | `EMA_RIB` | EMA crossover/alignment patterns |
| **Bollinger Breakout** | `BB_BREAK` | Price breaking out of Bollinger Band |
| **Donchian Channel** | `DONCHIAN` | New high/low channel breakout |
| **RSI Fade** | `RSI_FADE` | RSI extreme reversal (mean reversion) |
| **Bollinger Fade** | `BB_FADE` | Bollinger Band touch reversal |
| **Stochastic Cross** | `STOCH_X` | Stochastic K/D crossover in OB/OS zones |
| **Pin Bar** | `PIN_BAR` | Rejection wick pattern at key levels |
| **Inside Bar Break** | `IB_BREAK` | Inside bar → breakout confirmation |
| **Engulfing** | `ENGULF` | Bullish/bearish engulfing pattern |
| **MTF RSI** | `MTF_RSI` | Multi-timeframe RSI divergence (**removed from live — 0% TP hit rate**) |
| **Trend Pullback** | `TR_PULL` | Trend continuation on pullback to EMA |
| **Momentum Surge** | `MOM_SURGE` | High-momentum breakout with volume confirmation |

### Combo System

Each combo is a unique `{pair, strategy, timeframe, exit_mode}` tuple with validated metrics:

- **40 combos** in [deploy_combos.json](v13pro/deploy_combos.json) (38 active after delisted pair skips)
- **28 unique pairs** across Bybit USDT perpetuals
- **3 timeframes**: 15m, 30m, 1h
- **Exit modes**: fix1.2, fix1.5, fix2.0, fix3.0, trl1.5, trl2.0 (portfolio) + fix0.75 (hunter scalps)
- Each combo has validation/test profit factor, win rate, R-multiple, and average R

---

## How It Works — Step by Step

```
Bot Starts → Preflight checks → WS connects → Candle buffers warm up
|
+-- CONTINUOUS LOOP (every candle close):
|
|   +-- STEP 1: Strategy Scan
|   |   +-- For each active combo: run strategy on latest candle buffer
|   |   +-- Generate signals: {pair, direction, strategy, timeframe}
|   |
|   +-- STEP 2: Conviction Scoring (0-100 → A+/A/B/C/D/X)
|   |   +-- Key level proximity (swing H/L, pivots, round numbers): 0-30 pts
|   |   +-- Signal candle quality (body ratio, wick rejection): 0-25 pts
|   |   +-- Volume context (relative volume, expansion): 0-15 pts
|   |   +-- Trend alignment (EMA slope, structure): 0-15 pts
|   |   +-- Fee efficiency (range vs fee impact): 0-15 pts
|   |   +-- DNA boost: indicator values in winning clusters → +conviction
|   |
|   +-- STEP 3: Filters
|   |   +-- Sentiment: block SHORT when BULL (conf ≥50%) — saves 99R in shadow data
|   |   +-- OB Alignment: +8 conviction when orderbook pressure matches direction
|   |   +-- Min grade: portfolio needs grade B+, hunter scalps accept C+
|   |
|   +-- STEP 4: Risk Routing
|   |   +-- PORTFOLIO signal → can_trade() → full risk checks (max_concurrent,
|   |   |   pair cooldown, consecutive loss cooldown, daily cap)
|   |   +-- HUNTER signal → can_trade_hunter() → separate 5-slot pool
|   |   |   (only checks: same-symbol conflict, daily count, growth cap)
|   |
|   +-- STEP 5: Execution
|   |   +-- Maker entry (limit at bid/ask) with 120s timeout, fallback to taker
|   |   +-- Set SL on exchange, set TP based on exit mode
|   |   +-- Guardian starts monitoring the position
|   |
|   +-- STEP 6: Shadow Recording
|       +-- ALL signals (traded + rejected) logged to shadow JSONL
|       +-- Tracks peak_r, trough_r, 1m/5m/15m/60m checkpoints
|       +-- Used for offline filter analysis and improvement
|
+-- PARALLEL TASKS (async):
    +-- Hunter: scans full Bybit universe every 5 min
    +-- Guardian: polls open positions every 15s (SL tiers, trail, rejection exit)
    +-- Sentiment: refreshes BTC/ETH/SOL bias continuously
    +-- Watchdog: health checks (network, memory, WS, disk)
    +-- Shadow: tracks all simulated positions to resolution
```

---

## Intelligence Modules

### Sentiment Gauge (`v13pro/sentiment.py`)

Real-time market bias from BTC, ETH, SOL on 1h candles:

| Component | Method |
|---|---|
| Short-term momentum | EMA-8 vs EMA-21 slope direction |
| Structure | Last N candles higher-highs / lower-lows |
| Trend position | Close vs EMA-21 |
| Aggregation | Majority vote with confidence weighting |

**Output**: `{bias: "BULL"/"BEAR"/"NEUTRAL", confidence: 0-100%}`

**Live filter**: Shorts blocked when BULL with ≥50% confidence. Shadow data showed shorts-in-BULL have median peak of only 0.28R — not profitable at any TP level.

### Orderflow Intel (`v13pro/orderflow.py`)

L2 orderbook microstructure captured at signal time:

| Metric | Description |
|---|---|
| Spread | Bid-ask spread in basis points |
| Imbalance | Top-20 level bid vs ask volume ratio (-1 to +1) |
| Depth ratio | Buy wall vs sell wall pressure |
| Classification | tight / normal / wide based on spread |

**Live filter**: When imbalance aligns with direction (>0.10 for LONG, <-0.10 for SHORT), conviction gets +8 points. Verified working on live trades (e.g. POWER +8 → conv 94 A+).

### Shadow Trader (`v13pro/shadow.py`)

Passive data collection on every signal the bot sees:

- Simulates entry at signal price
- Tracks 1-minute candle movement for duration
- Records: peak_r, trough_r, hit_tp, hit_sl
- Checkpoints at 1m, 5m, 15m, 60m after entry
- Captures full context: conviction, sentiment, orderflow, source (portfolio/hunter)
- 4,787+ outcomes analysed → drove all filter upgrades + intelligence modules

### DNA Profiler (`v13pro/dna.py`)

Statistical edge discovery from trade history:

- Captures 25 raw numeric indicator values at every entry
- After sufficient trades, runs correlation analysis
- Finds exact indicator RANGES that separate winners from losers
- Example: "When EMA8_slope is 0.3-0.8 AND RSI 45-65, WR jumps from 48% to 72%"
- Boosts conviction when new signal falls inside winning clusters

### Regime Detector (`v13pro/regime.py`)

Self-calibrating market regime detection from rolling shadow data:

- States: HOT (1.30x) → WARM (1.10x) → NORMAL (1.00x) → COOL (0.70x) → COLD (0.45x)
- Session-aware: each session (asia/london/ny) gets independent multiplier
- Currently: COOL (0.70x global), london=1.05x, asia=0.76x, ny=1.40x
- Rolling window of 300 outcomes, EWMA smoothing prevents whiplash

### Directional Intelligence (`v13pro/directional.py`)

Adaptive side filtering per sentiment regime — replaces static LONG_ONLY_MODE:

- Analyses shadow outcomes by side + sentiment bucket (BULL/BEAR/NEUTRAL)
- Key finding: BEAR market → longs 73% WR (+0.59R), shorts 15% WR (-0.65R)
- BULL market → shorts 51% WR (+0.07R), longs 33% WR (-0.13R)
- Blocks sides with no proven edge, boosts risk for proven directions
- 1,113 outcomes across 14 buckets, refreshes every 600s

### EdgeRadar (`v13pro/edge_radar.py`)

Full shadow intelligence exploitation — 4 risk multipliers from 7 unused data dimensions:

- **Combo heat**: per strategy/tf — HOT (1.25x), WARM (1.0x), COLD (0.50x), FROZEN (blocked)
- **Market heat**: avg peak R of recent trades — HOT (1.20x), WARM (1.0x), COLD (0.75x)
- **Sentiment edge**: fine-grained risk scaling by continuous sentiment score per side
- **Hot seat**: when 2+ signals align (market hot + HOT combos + runners), boost 1.15-1.30x
- Currently: 7 HOT combos (BB_BREAK/15m 67% WR, BB_BREAK/1h 83% WR), 1 FROZEN (TR_PULL/15m 17% WR)

### Burst Engine (`v13pro/burst.py`)

Dynamic risk/leverage/TP scaling based on Burst Composite Score (BCS):

- BURST state: increased risk (1.30x), leverage, and TP when edge is strong
- DECAY state: reduced risk (0.65x) and slots when in drawdown
- NORMAL: standard sizing
- BCS computed from combo win rates and equity curve momentum
- Self-tuning optimizer (Phase 2A) adjusts parameters iteratively

---

## Hunter Scalp System

The hunter scans the **entire Bybit USDT-perp universe** (not just the 28 portfolio pairs) for fresh signals. Instead of blocking these signals (which wasted 938 high-conviction opportunities), v13pro now takes quick 0.75R scalp profits.

### How It Works

1. **Scan**: Every 5 min, bulk fetch all tickers → filter by 24h volume (>$3M) + spread (<0.15%)
2. **Analyse**: Run 12 strategies on qualifying pairs
3. **Score**: Conviction scoring, same as portfolio
4. **Filter**: Min grade C, sentiment filter still applies
5. **Route**: `can_trade_hunter()` — independent 5-slot pool, bypasses portfolio max_concurrent
6. **Execute**: 0.75R fixed TP, 35% of portfolio risk (~$3.65-$4.56 per trade)
7. **Guard**: Same guardian with trailing stop (activates at 0.75R, trails 0.40R behind peak)

### Configuration

| Param | Value | Rationale |
|---|---|---|
| `HUNTER_MAX_POSITIONS` | 5 | Independent from portfolio slots |
| `HUNTER_RISK_MULT` | 0.35 | 35% of portfolio risk — quick scalps, small size |
| `HUNTER_MIN_GRADE` | "C" | Lower conviction bar for quick exits |
| `HUNTER_EXIT_MODE` | "fix0.75" | Data-proven: 91% WR on longs-in-BEAR at 0.75R |
| `HUNTER_SCAN_INTERVAL` | 300s | 5 min between universe scans |
| `HUNTER_MIN_VOL_24H` | $3M | Minimum 24h volume |
| `HUNTER_MAX_SPREAD_PCT` | 0.15% | Maximum spread |

### Data Validation

From 2,239 shadow outcomes:
- Hunter LONG: median peak 0.63R, mean 0.94R — plenty for 0.75R scalps
- Hunter SHORT: median peak 0.30R, mean 0.49R — weaker, filtered by sentiment
- Counter-trend LONG in BEAR at 0.75R TP: **91.1% WR, +120.8R total**
- First live scan (Feb 25): 5 trades placed in one cycle, all at high conviction (68A to 94A+)

---

## Guardian — Position Management

The Guardian runs as an async task monitoring every open position:

### Progressive SL Tiers (exchange crash safety net)

| Position Reaches | SL Moves To | Tag |
|---|---|---|
| +0.30R | -0.60R | tier0_early_cut |
| +0.60R | -0.20R | tier1_protect |
| +1.00R | Breakeven | tier2_breakeven |
| +1.50R | +0.60R | tier3_lock060 |
| +2.00R | +1.50R | tier4_lock150 |
| +3.00R | +2.30R | tier5_lock230 |

### Trailing Stop

| Parameter | Value |
|---|---|
| Activation | 1.5R (was 1.0R — raised to prevent sub-1R captures at 0.48-0.90R) |
| Trail distance | 0.50R behind peak (was 0.30R — wider prevents shakeouts) |
| Min move | 0.10R (only updates SL if it moves at least this much) |
| Direction | Only moves forward, never backward |

### Additional Protection

| Feature | Description |
|---|---|
| **1m Rejection Exit** | Detects reversal wicks (>60% wick, <35% body) on 1m candles when position is profitable (>0.5R) |
| **Engulfing Exit** | Detects bearish/bullish engulfing patterns on 1m candles |
| **Funding Rate Monitor** | Checks every 5 min. Max rate: 0.10% per 8h. Force-close at 0.30% |
| **Position Resolution** | Uses Bybit closedPnl endpoint to detect and resolve closed positions |

---

## Risk Management

### Equity-Scaled Risk Curves

Risk and leverage automatically adjust as equity grows:

| Equity | Risk/Trade | Leverage | Max Concurrent |
|---|---|---|---|
| $100 | 2.0% | 8x | 5 |
| $250 | 2.0% | 8x | 6 |
| $500 | 2.0% | 8x | 7 |
| $1,000 | 2.0% | 8x | 8 |
| $5,000 | 1.8% | 5x | 10 |
| $10,000 | 1.5% | 4x | 12 |
| $50,000 | 1.0% | 3x | 15 |
| $100,000 | 0.8% | 2x | 15 |

### Conviction Multiplier

| Grade | Risk Multiplier |
|---|---|
| A+ | 1.25x |
| A | 1.10x |
| B | 1.00x |
| C | 0.75x |
| D | 0.50x |

### Drawdown Throttle

| DD % | Risk Multiplier |
|---|---|
| 0-5% | 1.00x (full) |
| 5-10% | 0.75x |
| 10-15% | 0.50x |
| 15-20% | 0.25x |
| 20-30% | 0.10x |

### Safety Limits

| Parameter | Value |
|---|---|
| Max trades/day | 30 |
| Daily growth cap | 15% |
| Equity floor | 60% of peak |
| Pair cooldown | 60 min after trade |
| Loss cooldown | 2 consecutive losses → 4h cooldown on that pair |
| Margin mode | Isolated (always) |

### Sessions (UTC)

| Session | Hours | |
|---|---|---|
| **Asia** | 00:00 — 08:00 | |
| **London** | 08:00 — 16:00 | |
| **New York** | 16:00 — 24:00 | |

---

## Configuration Reference

### Core Settings (`v13pro/config.py`)

| Parameter | Value | Description |
|---|---|---|
| `MAINNET` | True | Real money trading |
| `LEVERAGE` | 10 | Default (equity-curve scaled) |
| `RISK_PCT` | 0.03 | Base risk 3% (curved from 2%) |
| `FEE_RATE` | 0.00055 | Taker fee rate |
| `MAKER_FEE_RATE` | 0.0002 | Maker fee rate (0.02%) |
| `MAKER_ENTRY_TIMEOUT_SEC` | 120 | Limit order timeout before taker fallback |

### Guardian Settings

| Parameter | Value | Description |
|---|---|---|
| `TRAIL_ENABLED` | True | Trailing stop active |
| `TRAIL_ACTIVATION_R` | 0.75 | Start trailing at 0.75R profit |
| `TRAIL_DISTANCE_R` | 0.40 | Trail 0.40R behind peak |
| `TRAIL_MIN_MOVE_R` | 0.10 | Min SL move increment |
| `GUARDIAN_POLL_SECS` | 15 | Position check interval |
| `EXCHANGE_TP_R` | 10.0 | Safety-net TP on exchange |
| `FUNDING_RATE_MAX_PCT` | 0.10 | Max acceptable funding rate |
| `FUNDING_RATE_EXIT_PCT` | 0.30 | Force-close funding threshold |
| `REJECTION_EXIT_ENABLED` | True | 1m rejection candle exit |

### Hunter Settings

| Parameter | Value | Description |
|---|---|---|
| `HUNTER_ENABLED` | True | Universe scanning active |
| `HUNTER_TRADE_ENABLED` | True | Actually trade hunter signals |
| `HUNTER_MAX_POSITIONS` | 5 | Independent pool (not shared with portfolio) |
| `HUNTER_RISK_MULT` | 0.35 | 35% of portfolio risk per scalp |
| `HUNTER_MIN_GRADE` | "C" | Min conviction grade |
| `HUNTER_EXIT_MODE` | "fix0.75" | 0.75R quick-profit TP |
| `HUNTER_SCAN_INTERVAL` | 300 | Seconds between scans |
| `HUNTER_MIN_VOL_24H` | 3,000,000 | Min 24h volume filter |
| `HUNTER_MAX_SPREAD_PCT` | 0.15 | Max spread percentage |

---

## Project Structure

```
anewBot/
├── v13pro/                       # Core trading system
│   ├── __main__.py               # python -m v13pro entry point
│   ├── run.py                    # CLI runner (--maker --entry flags)
│   ├── bot.py                    # Main async 24/7 orchestrator (1795 lines)
│   ├── config.py                 # All configuration (357 lines)
│   ├── strategies.py             # 12 strategies + ensemble
│   ├── indicators.py             # Technical indicator calculations
│   ├── registry.py               # Combo registry + exit params
│   ├── deploy_combos.json        # 40 active combos (28 pairs, 3 TFs)
│   ├── exchange.py               # Bybit async ccxt wrapper
│   ├── guardian.py               # Position guard: SL tiers + trail + rejection + funding
│   ├── hunter.py                 # Async pair universe scanner
│   ├── state.py                  # Thread-safe JSON state persistence
│   ├── skill.py                  # Multi-factor conviction scorer (0-100)
│   ├── dna.py                    # Setup DNA profiler (25 features → edge discovery)
│   ├── sentiment.py              # BTC/ETH/SOL momentum gauge
│   ├── orderflow.py              # L2 orderbook microstructure
│   ├── shadow.py                 # Passive shadow trader (all signals)
│   ├── ws_data.py                # Async WebSocket multi-TF data engine
│   ├── watchdog.py               # System health monitor
│   ├── journal.py                # Trade journal
│   ├── trade_logger.py           # Structured JSONL logger
│   ├── logger.py                 # Timestamped logging
│   ├── preflight.py              # Pre-flight checks
│   ├── supervisor.py             # Bot supervisor / restart
│   ├── aftermath.py              # Post-trade analysis
│   ├── learner.py                # Adaptive learning
│   ├── regime.py                 # Self-calibrating regime detector (HOT→COLD)
│   ├── directional.py            # Adaptive directional intelligence from shadow
│   ├── edge_radar.py             # Full shadow intelligence (combo/market/sentiment/hot seat)
│   ├── adaptive.py               # Data-driven parameter adaptation
│   ├── signal_quality.py         # Signal quality scoring from outcomes
│   ├── calibrator.py             # Self-calibrator (edge health, stationarity)
│   ├── lifecycle.py              # Per-pair lifecycle tracking
│   ├── cross_sect.py             # Cross-sectional awareness
│   ├── burst.py                  # Burst engine (BCS, BURST/NORMAL/DECAY)
│   ├── burst_optim.py            # Burst optimizer (iterative self-tuning)
│   ├── combo_promoter.py         # Auto-promote shadow combos to live
│   ├── state.json                # Live state (auto-managed)
│   └── logs/                     # Log files + shadow data
│       └── shadow/               # Shadow JSONL daily files (4,787+ outcomes)
│
├── strategy/                     # Legacy FCB core (backtest)
│   └── fcb_core.py
├── backtest/                     # Backtesting infrastructure
│   ├── data_loader.py
│   └── engine.py
├── analysis/                     # Post-trade analysis modules
│   ├── metrics.py, monte_carlo.py, scorecard.py, etc.
│
├── scripts/                      # Organised utility scripts
│   ├── sweeps/                   # Parameter sweep scripts
│   ├── analysis/                 # Analysis & research scripts
│   ├── testing/                  # Test scripts
│   ├── runners/                  # Backtest runners
│   └── tools/                    # Pair scanners, utilities
│
├── _analyze_shadow.py            # Shadow data basic analysis
├── _shadow_filters.py            # Shadow filter optimisation
├── _hunter_countertrend_analysis.py  # Hunter + counter-trend analysis
│
├── dashboard.py                  # Real-time HTTP dashboard
├── watchdog.py                   # Root-level watchdog
├── config.py                     # Backtest configuration
├── requirements.txt              # Python dependencies
├── README.md                     # This file
├── CHANGELOG.md                  # All code changes
├── PROJECT_TRACKER.md            # Project status tracker
└── IMPROVEMENT_PLAN.md           # Research roadmap
```

---

## Installation

### Prerequisites

- **Python 3.10+** (3.12+ recommended)
- **pip** (comes with Python)
- **Bybit account** (mainnet)

### Setup

```bash
# 1. Clone the repository
git clone <repo-url>
cd anewBot

# 2. Create a virtual environment
python -m venv .venv

# 3. Activate it
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt
```

### API Keys

```powershell
# Windows PowerShell:
$env:BYBIT_API_KEY = "your_mainnet_key"
$env:BYBIT_API_SECRET = "your_mainnet_secret"
```

```bash
# Linux/macOS:
export BYBIT_API_KEY="your_mainnet_key"
export BYBIT_API_SECRET="your_mainnet_secret"
```

---

## Quick Start

### Go Live

```bash
# Activate virtualenv, then:
python -m v13pro.run --maker --entry

# Flags:
#   --maker   Enable maker (limit) TP orders (0.02% fee vs 0.055% taker)
#   --entry   Enable maker (limit) entry orders with 120s timeout
```

The bot will:
1. Run preflight checks (exchange, balance, pairs, leverage)
2. Connect WebSocket streams for all portfolio pairs
3. Warm up candle buffers (need 200+ candles for indicators)
4. Start scanning combos on every candle close
5. Start hunter scanning every 5 min
6. Start guardian monitoring open positions
7. Start shadow tracking all signals

### Dashboard

```bash
python dashboard.py
# → Open http://localhost:8080
```

---

## Research & Analysis Tools

### Shadow Data Analysis

```bash
# Basic shadow outcome analysis
python _analyze_shadow.py

# Comprehensive filter optimisation
python _shadow_filters.py

# Hunter + counter-trend move depth analysis
python _hunter_countertrend_analysis.py
```

### Key Findings from Shadow Data (4,787+ outcomes)

| Finding | Impact | Action Taken |
|---|---|---|
| Trail at 1.0R catches at 0.48-0.90R | Miniature profits | Trail raised to 1.5R activation, 0.5R distance |
| BEAR market → longs 73% WR | +0.59R expectancy | DirectionalIntel blocks shorts in BEAR |
| BULL market → shorts 51% WR | +0.07R expectancy | DirectionalIntel blocks longs in BULL |
| BB_BREAK/15m: 67% WR, +0.56R | Strong combo | EdgeRadar marks HOT, 1.25x risk |
| TR_PULL/15m: 17% WR, -0.39R | Dead combo | EdgeRadar marks FROZEN, hard blocked |
| 3% risk + escalating leverage | Death spiral, -11% | Fixed to 2% risk, 8x flat leverage |
| COLD regime freeze blocks all trades | Zero entries | Disabled freeze, let 0.70x mult scale risk |
| EdgeRadar summary() deadlock | Heartbeat hung forever | Fixed threading.Lock → RLock |
| Sentiment longs in bear: 70-79% WR | Best edge in system | Sentiment risk multiplier 1.15-1.25x |
| Market peak_r < 0.6R = no runners | Size down in dead markets | Market heat mult 0.75x when COLD |

---

## FAQ

**Q: How is this different from the original FCB system?**
A: v13pro is a complete architectural rewrite. FCB was a single-strategy (First Candle Breakout) system. v13pro runs 12 strategies simultaneously across 3 timeframes with conviction scoring, DNA profiling, and autonomous pair discovery. The combo registry replaces the session-based pair lists.

**Q: What is the entry point?**
A: `python -m v13pro.run --maker --entry`. Do NOT use `run_live.py` (that's the legacy FCB launcher).

**Q: What are hunter scalps?**
A: The hunter scans the entire Bybit universe for signals on pairs outside the portfolio. Instead of blocking these (which wasted 938 high-conviction signals), the bot now takes quick 0.75R profits with reduced risk (35% of normal) in a separate 5-slot pool.

**Q: Why block shorts in BULL market?**
A: Shadow data (2,239 outcomes) showed shorts when sentiment is BULL have a median peak of only 0.28R — they almost never reach any reasonable TP level. The sentiment filter saves ~99R.

**Q: Why was MTF_RSI removed?**
A: Across 92 shadow outcomes, MTF_RSI had exactly 0 TP hits at any TP level. Even the most generous scalp scenario was net negative. It's a dead strategy.

**Q: What happens if the bot crashes?**
A: All SL/TP orders are set directly on Bybit (exchange-side). Positions are protected by progressive SL tiers even if the bot dies. On restart, the guardian detects and resumes monitoring existing positions.

**Q: Can I modify state.json while the bot is running?**
A: **No.** The bot's in-memory state overwrites the file on every equity update. Stop the bot first, edit the file, then restart.

**Q: How does conviction scoring work?**
A: Each signal gets 0-100 points across 5 factors: key-level proximity (30pts), candle quality (25pts), volume context (15pts), trend alignment (15pts), fee efficiency (15pts). DNA profiler can boost further. Grades: A+ (80+), A (65+), B (50+), C (35+), D (20+), X (<20).

---

## Change Log

All changes tracked in **[CHANGELOG.md](CHANGELOG.md)**.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Disclaimer

This software is provided for **educational and research purposes only**. It is not financial advice. Trading cryptocurrencies involves substantial risk of loss. Past performance does not guarantee future results. Never risk money you cannot afford to lose.
