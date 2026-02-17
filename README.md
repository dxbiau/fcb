# FCB — First Candle Breakout Trading System

## Live Trading Bot + Backtester for Bybit Perpetual Futures

> **Zero indicators. Zero discretion. Zero optimisation.**
> One candle defines the range. One breakout triggers the trade. One retest confirms the entry.

---

## Table of Contents

1. [What Is This?](#what-is-this)
2. [Research & Evolution Log](#research--evolution-log)
3. [The Strategy — First Candle Breakout (FCB)](#the-strategy--first-candle-breakout-fcb)
4. [How The Strategy Works — Step by Step](#how-the-strategy-works--step-by-step)
5. [Live Bot](#live-bot)
6. [Profit Guardian v3 — Trail Intelligence](#profit-guardian-v3--trail-intelligence)
7. [Architecture Overview](#architecture-overview)
8. [Project Structure](#project-structure)
9. [Installation](#installation)
10. [Quick Start](#quick-start)
11. [Usage Guide](#usage-guide)
12. [Configuration Reference](#configuration-reference)
13. [Backtesting & Analysis](#backtesting--analysis)
14. [Research Tools](#research-tools)
15. [Testing](#testing)
16. [FAQ](#faq)

---

## What Is This?

A **complete live trading bot and backtesting framework** for a rule-based cryptocurrency trading strategy called **First Candle Breakout (FCB)**. The system is built end-to-end: from historical validation (1,713 trades backtested) to fully automated live execution on **Bybit perpetual futures**.

> *"Does this simple, mechanical strategy have a statistically validated edge — and will it survive real-world trading?"*

The system does **not** use technical indicators (no RSI, no MACD, no moving averages). It relies purely on **price-action structure**: the first 5-minute candle of each major trading session defines a range, and a breakout-with-retest of that range triggers a trade.

### What it does:

| Capability | Description |
|---|---|
| **Live Trading** | Fully automated bot on Bybit mainnet — 37 pairs, 3 sessions/day, real-time intelligence agent |
| **Demo Trading** | Paper trading on Bybit Demo environment for validation before going live |
| **Guardian Agent** | Pre-entry margin checks, position health monitoring, stale order cleanup, equity anomaly detection |
| **~~Smart TP v1~~ → Profit Guardian v3** | ~~Trail 1.5% behind peak after 1.5R~~ → Trail 0.3R behind peak after 1.0R — no TP cap, winners run to 5-10R+ |
| **C3 Fakeout Detection** | 100% precision early exit — detects reversal candles within 5 minutes, exits fakeouts at 0.3R loss instead of full -1R |
| **Pair Classification** | Dynamic A/B tier system with automatic promotion/demotion based on consecutive wins/losses |
| **Singleton Lock** | PID-based lock file prevents duplicate bot instances |
| **Historical Backtesting** | Downloads 5m OHLCV data via ccxt, replays the FCB strategy candle-by-candle |
| **Multi-Pair Testing** | Tests across 37 Bybit USDT perpetual pairs |
| **Monte Carlo Validation** | Reshuffles trade order 10,000 times to prove the edge is not sequence-dependent |
| **Risk-of-Ruin Modelling** | Computes probability of catastrophic drawdown at each risk level |
| **Compounding Projections** | Maps trades to x2, x5, x10, x100, x1000 capital milestones |
| **GO/NO-GO Scorecard** | Automated pass/fail gate with hard thresholds |
| **Pair Scanner** | Scans all exchange pairs for FCB-compatible candidates |
| **Breakout DNA Analysis** | Research tool analysing what makes breakouts real vs fake |

### What it is NOT:

- **Not a black box** — every rule is explicit, auditable, and tested
- **Not an optimiser** — no curve-fitting, no parameter sweeps, no data snooping
- **Not a signal service** — this is a trading system for a specific, validated strategy
- **Not financial advice** — this is software for educational and research purposes

---

## The Strategy — First Candle Breakout (FCB)

### Core Thesis

Every major trading session (Asian, London, New York) opens with a burst of activity as institutional participants enter the market. The **very first 5-minute candle** of each session captures this initial price discovery. When price subsequently breaks out of and retests this range, it signals directional intent.

### The Rules (Non-Negotiable)

| # | Rule | Detail |
|---|---|---|
| 1 | **Timeframe** | 5-minute candles only |
| 2 | **Range Definition** | High and Low of the first 5m candle after session open |
| 3 | **Breakout** | Close (not wick) above range_high → bullish; close below range_low → bearish |
| 4 | **Retest** | The **immediately next candle** must wick back into the range AND close back beyond it |
| 5 | **Entry** | Market order at retest candle close (full 2% risk position) |
| 6 | **Stop Loss** | Range midpoint (halfway between range_high and range_low) — always |
| 7 | **Take Profit** | ~~Fixed 1.5R~~ → ~~Smart TP v1 (1.5% trail)~~ → **Guardian v3**: trail 0.3R behind peak once R≥1.0. No TP cap. Exchange TP at 10R safety net. |
| 8 | **One trade per session** | Maximum 1 trade per pair per session. If the retest fails, the session is done. |
| 9 | **Three trades per day** | Maximum 3 trades per pair per day (across all 3 sessions) |
| 10 | **No re-entries** | A failed retest means the session is finished. No second chances. |

### Live Configuration

| Parameter | Value | Rationale |
|---|---|---|
| **TP Mode** | **Guardian v3 Trail** | Trail 0.3R behind peak once R≥1.0 — no TP cap, winners run free |
| **Exchange TP** | 10R safety net | Far TP on exchange — trail handles real exit, this is crash protection |
| **Trail Activation** | 1.0R | Start trailing once position reaches +1.0R |
| **Trail Distance** | 0.3R | SL follows 0.3R behind peak R (data-optimal across 12,355 trades) |
| **C3 Fakeout Exit** | Enabled | 100% precision — exits detected fakeouts at ~0.3R loss |
| **Leverage** | 10x | Isolated margin per pair |
| **Risk** | 2% flat | DNA analysis proved A/B pairs perform identically |
| **Entry** | Full market order | Split entry/scale-in disabled (amplified losses 50-80%) |
| **Equity Floor** | $500 | Bot stops trading if equity drops below this |

### Session Schedule (UTC)

| Session | Open | Close | Pairs |
|---|---|---|---|
| **Asia** | 00:00 | 08:00 | 22 |
| **London** | 08:00 | 16:00 | 11 |
| **New York** | 16:00 | 24:00 | 12 |
| **Total Unique** | — | — | **37** |

---

## How The Strategy Works — Step by Step

```
Session Opens (e.g. NY at 16:00 UTC)
|
+-- STEP 1: Capture Range
|   +-- First 5m candle (16:00-16:05): H=105, L=95, Midpoint=100
|
+-- STEP 2: Wait for Breakout
|   +-- Some candle closes ABOVE 105 (long) or BELOW 95 (short)
|   +-- Example: 16:25 candle closes at 107 → Bullish breakout confirmed
|
+-- STEP 3: Confirm Retest (MUST be next candle)
|   +-- 16:30 candle: Low touches 105 (wicks back), Close=108 (holds above)
|   +-- Valid retest → ENTER LONG
|
+-- STEP 4: Entry
|   +-- Market order: full 2% risk position at 108
|   +-- Stop Loss = 100 (midpoint), Exchange TP = 10R safety net
|
+-- STEP 5: Profit Guardian v3 Monitors (every 2 seconds)
|   +-- [C3 Fakeout Check] At ~5 min after entry:
|   |   +-- IF C3 candle body reverses direction (>30% of range)
|   |   +-- AND current R < 0.3 → EXIT AT MARKET (save ~0.7R per fakeout)
|   |
|   +-- [Progressive SL Tiers] Exchange crash safety net:
|   |   +-- +0.50R → SL at -0.25R (cut max loss 75%)
|   |   +-- +0.75R → SL at breakeven
|   |   +-- +1.00R → SL at +0.5R (locked profit)
|   |   +-- +1.20R → SL at +0.8R
|   |
|   +-- [Trail SL] Once R >= 1.0:
|   |   +-- SL = peak_R - 0.3R (always trails 0.3R behind peak)
|   |   +-- SL only moves forward, never backward
|   |   +-- No TP cap — winners run as far as the market takes them
|   |
|   +-- [Health Monitor] Every ~30 seconds:
|   |   +-- Log current R, peak R, tier, trail status
|   |
|   +-- IF price hits SL (midpoint) → EXIT → R = -1.0
|   +-- IF trail SL hit → EXIT → R = 0.7R to 10R+ (market decides)
|   +-- IF C3 fakeout detected → EXIT → R ≈ -0.3 (saved ~0.7R)
|
+-- Session Done for this pair. Wait for next session.
```

### What Makes This Strategy Different

1. **Fully Mechanical**: A computer or a human with a ruler can execute this identically
2. **No Indicators**: Pure price action — the market structure IS the signal
3. **Time-Gated**: Only 3 opportunities per day per pair — prevents overtrading
4. **Asymmetric by Design**: Small stop (midpoint), 1.5R target — risk is always defined
5. **Falsifiable**: The scorecard gives a binary GO/NO-GO — no subjective interpretation
6. **Real-Time Intelligence**: C3 fakeout detection (100% precision) + Guardian v3 trail (0.3R behind peak, no TP cap)

---

## Live Bot

The live trading bot runs 24/7 on **Bybit mainnet**, fully automated.

### How It Works

1. **Boot** → Pre-flight checks (connection, balance, 37 pairs, leverage, order test)
2. **Sleep** → Live countdown timer until next session opens
3. **Session opens** → Capture first 5m candle for all session pairs
4. **Scan** → Monitor for breakout + retest pattern
5. **Entry** → Guardian agent checks margin → market order (full risk, no scale-in)
6. **C3 Check** → At ~5 min post-entry, check for fakeout reversal candle → exit if detected
7. **Guardian v3** → Poll every 2s: progressive SL tiers (safety net) + trail 0.3R behind peak once R≥1.0
8. **Session close** → Debrief, promote/demote pairs, save state
9. **Repeat** → Sleep until next session

### Guardian Agent

The `GuardianAgent` provides 5 layers of protection:

| Layer | What It Does |
|---|---|
| **Pre-entry margin check** | Verifies free margin before every order |
| **Position health monitoring** | Detects missing SL/TP, stale positions, unrealised P&L anomalies |
| **SL/TP healing** | Auto-reattaches stop loss and take profit if exchange drops them |
| **Stale position cleanup** | Force-closes positions that have been open too long |
| **Equity anomaly detection** | Alerts if equity deviates >20% from expected |

### Real-Time Intelligence Agent

The bot runs **Profit Guardian v3** on every open position — a daemon thread polling every 2 seconds:

| Subsystem | What It Does |
|---|---|
| **C3 Fakeout Detection** | At ~5 min post-entry: if C3 body reverses direction (>30% of range) AND current R < 0.3 → exit at market. 100% precision across 15 trades. |
| **Progressive SL Tiers** | Exchange crash safety net. Automatically moves SL as position profits: +0.5R→SL -0.25R, +0.75R→BE, +1.0R→SL +0.5R, +1.2R→SL +0.8R |
| **Trail SL (v3)** | Once R≥1.0: SL trails at peak_R − 0.3R. No TP cap. Winners run to 5-10R+. Proven across 12,355 trades: +1,738R vs +334R fixed TP. |
| **Health Monitor** | Logs current R, peak R, tier, trail status every ~30s. Full visibility without chart-watching. |

### Pair Classification (A/B Tiers)

Pairs are classified as **Class A** (proven) or **Class B** (provisional):

- **Class A**: 30+ backtest trades, positive expectancy
- **Class B**: <30 trades or borderline expectancy
- **Promotion**: 3 consecutive wins → B becomes A
- **Demotion**: 3 consecutive losses → A becomes B
- **Risk**: Flat 2% across both tiers (DNA analysis proved identical performance)

### State Management

All bot state persists in `live/state.json`:
- Equity tracking, trade counts, pending entries
- Per-pair win/loss streaks and classification
- Session trade limits (1 per pair per session, 3 per day)
- Automatic backup before each live start

---

## Profit Guardian v3 — Trail Intelligence

The core exit system. Evolved through 3 versions based on live trading data and backtests across 12,355 trades.

### Evolution: What Worked, What Failed

#### ~~v1: Smart TP (FAILED — Feb 13-16 live)~~

- Trail 1.5% behind peak price, activate at 1.5R, poll every 15s
- Exchange TP set at 10R as safety net
- **Result: -$92.68 loss over 4 days**
- **Root cause:** Trail code existed but **never activated** — 60s poll interval and 1.5R activation threshold meant it missed peaks entirely. Positions hit the fixed 1.5R TP before trail could engage.
- **Post-mortem:** SMART_TP destroyed -1.945R of edge

#### ~~v2: Profit Guardian (5 Systems) (FAILED — backtested)~~

Built 5 intelligence systems to fix v1's problems:

| System | What It Did | Result |
|---|---|---|
| Progressive SL Tiers | Move SL at +0.5R, +0.75R, +1.0R, +1.2R | ✅ Kept (crash safety net) |
| Retrace Detection | Close at market when profit drops from peak | ❌ **KILLED WINNERS** — -37R per 200 trades |
| Momentum Death | Cut losers heading to SL based on velocity | ❌ Added noise, no edge |
| Runner Capture | Extend TP when momentum strong near target | ❌ Replaced by no-cap trail |
| C3 Fakeout | Exit on reversal candle within 5 min | ✅ Kept (100% precision) |

**Retrace detection testing (random 200-trade backtest):**

| Retrace Threshold | Result vs Fixed 1.5R TP |
|---|---|
| 0.50R peak | -37R — closes everything early |
| 1.00R peak | -24R — still kills winners |
| 1.30R peak | -22R — barely better, still net negative |

**Conclusion:** Retrace detection at ANY threshold closes winners before they reach their peak. The cure was worse than the disease.

#### v3: Trail Intelligence (CURRENT — data-proven)

Stripped back to one simple rule: **trail 0.3R behind peak once R≥1.0**.

**Why this works:** Tested across **12,355 FCB trades** (89 pairs, all sessions):

| Exit Strategy | Total R | Win Rate | Profit Factor | vs Baseline |
|---|---|---|---|---|
| Fixed 1.5R TP (baseline) | +334R | 41.2% | 1.05 | — |
| BE only (no TP cap) | -3,709R | — | — | ❌ BE kills edge |
| BE + 0.7R trail | -844R | — | — | ❌ trail too wide |
| Fixed TP + BE at 0.75R | -1,487R | — | — | ❌ BE still kills |
| Trail 0.3R (no activation gate) | +849R | — | — | +515R |
| **Trail 0.3R, activate at 1.0R** | **+1,738R** | **50.3%** | **1.28** | **+1,404R (5.2x)** |
| Trail 0.2R, activate at 1.0R | +2,347R | — | 1.38 | +2,013R (7x, too tight for live) |

**Key discovery: Breakeven moves DESTROY edge.**

| BE Level | R Lost vs No-BE |
|---|---|
| BE at +0.50R | -1,622R |
| BE at +0.75R | -1,026R |
| BE at +1.00R | -514R |

Moving SL to breakeven at +0.75R shakes out 20% of trades that would have become full winners.

**Peak R distribution (12,355 trades):**
- Mean peak: **5.41R** (the 1.5R cap was leaving 3.9R on the table)
- Median peak: 3.66R
- 77.2% of trades peak above 1.5R
- 70.2% peak above 2.0R

### v3 System Design

```
Position Opened → Profit Guardian v3 Thread (polls every 2s)
|
+-- [1] PROGRESSIVE SL TIERS (exchange crash safety net)
|   +-- +0.50R → SL at -0.25R (cut max loss 75%)
|   +-- +0.75R → SL at breakeven (never a loser)
|   +-- +1.00R → SL at +0.5R (locked profit — trail takes over here)
|   +-- +1.20R → SL at +0.8R
|
+-- [2] TRAIL SL (once R >= 1.0)
|   +-- SL = peak_R - 0.3R (in R-terms)
|   +-- If peak was +2.5R, SL sits at +2.2R
|   +-- SL only moves forward, never backward
|   +-- No TP cap — exchange TP is set at 10R as last-resort safety
|   +-- The MARKET decides when you exit, not an arbitrary cap
|
+-- [3] HEALTH LOGGING (every ~30s)
    +-- Current R, peak R, tier, trail status
    +-- Full visibility without chart-watching
```

### x1000 Path Comparison

| Exit Strategy | Days to x1000 ($1K → $1M) |
|---|---|
| Fixed 1.5R TP | ~145 days |
| **Guardian v3 (0.3R trail)** | **~23 days** |

### What We Tested and Rejected

| Feature | Status | Data |
|---|---|---|
| ~~Fixed 1.5R TP~~ | Replaced by trail | Left 3.9R avg on the table |
| ~~Smart TP v1 (1.5% trail, 15s poll)~~ | Failed live | Never activated, -$92.68 |
| ~~Retrace detection~~ | Rejected | -22 to -37R per 200 trades at every threshold |
| ~~Momentum death~~ | Rejected | Added noise, no edge |
| ~~Runner capture/extend TP~~ | Rejected | Replaced by no-cap trail |
| ~~BE at +0.75R~~ | Rejected | Shakes out 20% of winners, costs -1,026R |
| ~~BE at +0.50R~~ | Rejected | Even worse: -1,622R |
| ~~Scale-in (limit at FC boundary)~~ | Rejected | 0/5 fills on winners, amplifies losses 50-80% |
| ~~Scale-out (close 50% at FC)~~ | Rejected | Cuts winners 60%, turned +$29 into -$38 |
| ~~Hybrid entry (skip slip>0.5R)~~ | Rejected | Missed 3 real winners worth +4.5R |
| ~~RECROSS exit~~ | Rejected | 50% precision — fires on 7/7 losers AND 7/8 winners |
| ~~ADVERSE_CANDLE exit~~ | Rejected | 56% precision — barely better than coin flip |
| ~~Trail 0.2R~~ | Too tight for live | Best in backtest (+2,347R) but slippage/spread risk |
| C3 fakeout detection | **KEPT** | 100% precision: 2/7 losers caught, 0/8 winners touched |
| Progressive SL tiers | **KEPT** | Exchange crash safety net |
| Trail 0.3R, activate 1.0R | **ACTIVE** | +1,738R across 12,355 trades (5.2x baseline) |

---

## Architecture Overview

```
+-------------------------------------------------------------+
|                      Entry Points                           |
|   run_live.py    run_demo.py    run_backtest.py             |
|   (mainnet)      (paper trade)  (historical backtest)       |
+-------+-------------+------------------+--------------------+
        |             |                  |
   +----v----+   +----v----+      +------v------+
   | live/   |   | live/   |      |  backtest/  |
   | bot.py  |   | bot.py  |      |  data_loader|
   | exchange|   |         |      |  engine     |
   | guardian|   |         |      +------+------+
   | state   |   |         |             |
   | trades  |   |         |      +------v------+
   +----+----+   +---------+      |  strategy/  |
        |                         |  fcb_core   |
   +----v---------+               |  filters    |
   | live/config  |               +------+------+
   | (37 pairs,   |                      |
   |  sessions,   |               +------v------+
   |  risk tiers) |               |  analysis/  |
   +--------------+               |  metrics    |
                                  |  monte_carlo|
                                  |  scorecard  |
                                  +-------------+
```

### Module Responsibilities

#### Live Trading (`live/`)

| Module | File | Purpose |
|---|---|---|
| **Bot** | `live/bot.py` | Main trading loop — session management, FC capture, breakout detection, entry/exit |
| **Exchange** | `live/exchange.py` | Bybit API wrapper via ccxt — orders, positions, leverage, margin, candles |
| **Guardian** | `live/guardian.py` | Pre-entry checks, position health, SL/TP healing, equity monitoring |
| **Profit Guardian** | `live/profit_guardian.py` | Trail SL daemon — polls every 2s, trails 0.3R behind peak once R≥1.0 |
| **State** | `live/state.py` | Persistent state — equity, trade counts, pair classifications, session limits |
| **Config** | `live/config.py` | All parameters: sessions, pairs, risk tiers, API keys, operational settings |
| **Strategy** | `live/strategy.py` | Live FCB signal logic — breakout/retest detection on candle data |
| **Trades** | `live/trades.py` | Trade CSV logging |
| **Logger** | `live/logger.py` | Timestamped logging with file output |

#### Backtesting (`backtest/`, `strategy/`, `analysis/`)

| Module | File | Purpose |
|---|---|---|
| **Config** | `config.py` | Backtest parameters: pairs, dates, risk levels, filter toggles |
| **Strategy Core** | `strategy/fcb_core.py` | Pure FCB logic — framework-agnostic, processes candles into trades |
| **Filters** | `strategy/filters.py` | Optional volume/time/session filters |
| **Data Loader** | `backtest/data_loader.py` | Downloads and caches 5m OHLCV from exchanges via ccxt |
| **Backtest Engine** | `backtest/engine.py` | Drives the backtest: calls FCBEngine, builds equity curves |
| **Core Metrics** | `analysis/metrics.py` | Win rate, expectancy, profit factor, max drawdown, streaks |
| **Session Stats** | `analysis/session_stats.py` | Per-session and per-direction breakdowns |
| **Monte Carlo** | `analysis/monte_carlo.py` | 10,000-iteration trade-reshuffle simulation |
| **Risk of Ruin** | `analysis/risk_of_ruin.py` | Ruin probability at various risk/DD thresholds |
| **Compounding** | `analysis/compounding.py` | x1000 growth maps, Kelly criterion, time estimates |
| **Scorecard** | `analysis/scorecard.py` | Automated GO/NO-GO gate with hard thresholds |
| **Plots** | `analysis/plots.py` | Equity curves, drawdown, R-distribution, MC fan charts |

---

## Project Structure

```
fcb/
├── run_live.py                 # Launch live bot (Bybit mainnet, real money)
├── run_demo.py                 # Launch demo bot (Bybit paper trading)
├── run_backtest.py             # CLI: download data → backtest → analysis
├── run_analysis.py             # CLI: analyse an existing trade_log.csv
├── config.py                   # Backtest configuration (pairs, dates, risk levels)
├── pair_scanner.py             # Scan exchange for FCB-compatible pairs
├── breakout_dna_discovery.py   # Research: what makes breakouts real vs fake
├── test_mainnet.py             # Pre-deployment mainnet connection test
├── preflight_check.py          # Standalone pre-flight checks
├── requirements.txt            # Python dependencies
├── pyproject.toml              # Packaging metadata
├── README.md                   # This file
│
├── live/                       # Live trading system
│   ├── config.py               # Live config: 37 pairs, sessions, risk tiers, API keys
│   ├── bot.py                  # Main FCB bot: session loop, FC capture, breakout scan
│   ├── exchange.py             # Bybit API wrapper (orders, positions, leverage, candles)
│   ├── guardian.py             # Guardian Agent: margin checks, health, SL/TP healing
│   ├── profit_guardian.py      # Profit Guardian v3: trail SL daemon (0.3R behind peak)
│   ├── state.py                # Persistent bot state (equity, trades, pair classes)
│   ├── strategy.py             # Live FCB signal detection
│   ├── trades.py               # Trade CSV logging
│   ├── logger.py               # Timestamped log output
│   ├── bot.lock                # PID lock file (auto-managed)
│   ├── state.json              # Current bot state (auto-managed)
│   ├── trades.csv              # Trade log (auto-managed)
│   └── logs/                   # Log files
│
├── strategy/                   # Core strategy logic (shared with backtest)
│   ├── fcb_core.py             # Pure FCB engine (framework-agnostic)
│   ├── filters.py              # Volume, time-cutoff, session-pair filters
│   ├── signals.py              # Signal generation
│   └── FCBStrategy.py          # Freqtrade IStrategy wrapper (legacy)
│
├── backtest/                   # Backtesting infrastructure
│   ├── data_loader.py          # ccxt downloader with CSV caching
│   └── engine.py               # Backtest runner + equity curve
│
├── analysis/                   # Post-trade analysis modules
│   ├── metrics.py              # Win rate, expectancy, PF, drawdown, streaks
│   ├── session_stats.py        # Per-session and direction breakdowns
│   ├── monte_carlo.py          # Monte Carlo reshuffling (10K iterations)
│   ├── risk_of_ruin.py         # Ruin probability modelling
│   ├── compounding.py          # x1000 growth projections + Kelly
│   ├── scorecard.py            # GO/NO-GO decision gate
│   └── plots.py                # Matplotlib visualisations
│
├── data/                       # Cached OHLCV data (auto-downloaded)
│   └── *.csv
│
├── results/                    # Backtest output
│   └── trade_log.csv
│
└── tests/                      # Unit tests
    └── test_fcb_core.py        # FCB rule verification
```

---

## Installation

### Prerequisites

- **Python 3.10+** (3.12+ recommended)
- **pip** (comes with Python)
- **Bybit account** (mainnet or demo)
- **Windows Time Service** (for Windows users — see [Clock Sync](#clock-sync) below)

### Setup

```bash
# 1. Clone the repository
git clone <repo-url>
cd fcb

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

For live trading, set your Bybit API keys as environment variables:

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

Demo mode uses hardcoded demo keys in `live/config.py` — no env vars needed.

---

## Quick Start

### Go Live (Real Money)

```bash
# 1. Set API keys (see above)
# 2. In live/config.py, ensure: MAINNET = True, DEMO_MODE = False
# 3. Run pre-flight only:
python run_live.py --preflight

# 4. Run for real:
python run_live.py
# → 7-step pre-flight → type "GO LIVE" → bot starts
```

### Demo Trading (Paper Trade)

```bash
# 1. In live/config.py, set: MAINNET = False, DEMO_MODE = True
# 2. Run:
python run_demo.py
```

### Backtest (Historical)

```bash
# Single pair
python run_backtest.py --pair DOGE/USDT --skip-download

# All pairs, fresh data
python run_backtest.py

# Analyse existing results
python run_analysis.py --file results/trade_log.csv
```

### Scan for New Pairs

```bash
python pair_scanner.py --exchange bybit --extended
```

---

## Usage Guide

### Live Bot (`run_live.py`)

```
python run_live.py [OPTIONS]

Options:
  --preflight     Run pre-flight checks only (no trading)
  --yes           Skip the "GO LIVE" confirmation prompt

Pre-flight checks:
  1. Config verification (MAINNET=True, DEMO_MODE=False)
  2. Exchange connection test
  3. Account balance check
  4. Pair availability (37/37 must resolve)
  5. Leverage configuration (10x, auto-clamp for restricted pairs)
  6. Order test (place + cancel a limit order)
  7. Position/order check (must be clean slate)
```

### Demo Bot (`run_demo.py`)

```
python run_demo.py [OPTIONS]

Options:
  --preflight     Run pre-flight checks only (no trading)
```

### Backtest CLI (`run_backtest.py`)

```
python run_backtest.py [OPTIONS]

Options:
  --pair PAIR           Single pair (e.g. DOGE/USDT)
  --pairs P1 P2 ...    Multiple pairs
  --start YYYY-MM-DD   Start date (default: 2022-01-01)
  --end YYYY-MM-DD     End date (default: 2025-12-31)
  --skip-download       Use cached CSV data only
  --risk FLOAT          Focus on a single risk level (e.g. 0.01)
  --tp FLOAT            TP R-multiple (0.75, 1.0, 1.5, 2.0)
  --leverage INT        Futures leverage
  --mc-iterations INT   Monte Carlo iterations (default: 10,000)
  --initial-capital $   Starting capital (default: 1000)
  --volume-filter       Enable volume filter
  --time-cutoff         Enable 90min time cutoff
```

### Pair Scanner (`pair_scanner.py`)

```
python pair_scanner.py [OPTIONS]

Options:
  --exchange NAME       Exchange to scan (default: bybit)
  --extended            Include extended analysis per pair
```

---

## Configuration Reference

### Live Config (`live/config.py`)

| Parameter | Value | Description |
|---|---|---|
| `MAINNET` | `True/False` | Real money vs demo/testnet |
| `DEMO_MODE` | `True/False` | Use Bybit Demo Trading environment |
| `TP_R` | `1.5` | Base R-multiple (reference only — trail handles exit) |
| ~~`SMART_TP`~~ | `False` | ~~v1 trailing~~ → Replaced by Guardian v3 |
| `TRAIL_ACTIVATION_R` | `1.0` | Start trailing when position reaches 1.0R |
| `TRAIL_DISTANCE_R` | `0.3` | Trail SL 0.3R behind peak R |
| `EXCHANGE_TP_R` | `10.0` | Far TP on exchange (safety net — trail handles exit) |
| ~~`TRAIL_PCT`~~ | `0.015` | ~~v1: trail 1.5% behind peak~~ → Replaced by R-based trail |
| ~~`SMART_TP_INITIAL_R`~~ | `10.0` | ~~v1 param~~ → Now `EXCHANGE_TP_R` |
| ~~`TRAIL_POLL_SECS`~~ | `15` | ~~v1: poll every 15s~~ → Guardian polls every 2s |
| `GUARDIAN_POLL_SECS` | `2` | Guardian v3 poll interval |
| `C3_EXIT` | `True` | Enable C3 fakeout detection |
| `C3_REVERSAL_BODY_PCT` | `0.30` | C3 body must be >30% of range to trigger |
| `C3_MAX_R_TO_EXIT` | `0.3` | Only exit if current R is below 0.3 |
| `HYBRID_ENTRY` | `False` | DISABLED — never skip entries |
| `MIN_RANGE_PCT` | `0.003` | 0.3% minimum first-candle range |
| `FEE_RATE` | `0.0002` | 0.02% maker fee |
| `LEVERAGE` | `10` | 10x isolated margin |
| `MAX_TRADES_SESSION` | `1` | Per pair per session |
| `MAX_TRADES_DAY` | `3` | Per pair per day |
| `SPLIT_ENTRY` | `False` | DISABLED — scale-in amplified losses 50-80% |
| `SCALE_OUT` | `False` | DISABLED — cuts winners 60% |
| `RISK_PCT_A` | `0.02` | 2% risk for all pairs (flat) |
| `RISK_PCT_B` | `0.02` | 2% risk (equalised after DNA analysis) |
| `EQUITY_FLOOR` | `500.0` | Stop trading below this equity |
| `PROMOTE_WINS` | `3` | Consecutive wins to promote B→A |
| `DEMOTE_LOSSES` | `3` | Consecutive losses to demote A→B |

### Sessions (UTC)

| Session | Open | Close | Pairs |
|---|---|---|---|
| `asia` | 00:00 | 08:00 | 22 (20A, 2B) |
| `london` | 08:00 | 16:00 | 11 (9A, 2B) |
| `ny` | 16:00 | 24:00 | 12 (10A, 2B) |

### Backtest Config (`config.py`)

| Field | Default | Description |
|---|---|---|
| `exchange` | `"binance"` | Exchange for historical data |
| `market_type` | `"futures"` | `"spot"` or `"futures"` |
| `start_date` | `"2022-01-01"` | Backtest start |
| `end_date` | `"2025-12-31"` | Backtest end |
| `initial_capital` | `1000.0` | Starting equity in USD |
| `risk_levels` | `[0.005, 0.01, 0.015, 0.02]` | Risk per trade as fraction |
| `monte_carlo_iterations` | `10,000` | MC simulation count |

---

## Backtesting & Analysis

When you run a backtest, the system automatically runs this analysis chain:

### 1. Core Metrics
- **Win Rate (WR)**: Percentage of profitable trades
- **Expectancy E(R)**: Average R-multiple per trade (must be > 0)
- **Profit Factor (PF)**: Gross profit / Gross loss (must be > 1.0)
- **Max Drawdown (R)**: Worst peak-to-trough decline

### 2. Session Breakdown
Performance split by Asia / London / NY and by Long / Short direction.

### 3. Monte Carlo Simulation (10,000 iterations)
Reshuffles trade R-multiples randomly. Proves the edge survives regardless of trade order.

### 4. Risk of Ruin
For each risk level, computes probability of hitting 25%, 50%, or 75% drawdown.

### 5. Compounding Projections
Maps trades-to-target for capital milestones (x2, x5, x10, x100, x1000).

### 6. GO/NO-GO Scorecard

| Check | Threshold |
|---|---|
| Sample Size | >= 50 trades |
| Win Rate | >= 30% |
| Expectancy | >= 0.05R |
| Profit Factor | >= 1.1 |
| Max Drawdown | >= -30R |
| Max Loss Streak | <= 15 |
| Risk of Ruin | <= 5% |
| MC P5 Profitable | > initial capital |

**ALL checks must pass for a GO verdict.**

### Validated Results (1,713 trades)

| Metric | Value |
|---|---|
| Total Trades | 1,713 |
| Win Rate | 52.2% |
| Expectancy | 0.237R |
| Profit Factor | 1.47 |
| Verdict | **GO** |

---

## Research Tools

### Pair Scanner (`pair_scanner.py`)

Scans all USDT perpetual pairs on an exchange, backtests each with FCB, and identifies candidates that meet minimum thresholds (positive expectancy, sufficient trades).

### Breakout DNA Discovery (`breakout_dna_discovery.py`)

Research analysis of 1,713 trades answering:
- What makes a breakout real vs fake?
- What metrics predict hot/cold streaks?

Key findings:
- **Mean reversion is real**: After 3 consecutive losses, next trade WR = 61.3%
- **Volume sweet spot**: FC volume 2-4x pre-session avg → 58.8% WR
- **FC shape**: Dojis/indecisive candles → better WR than strong body candles
- **5-9 day gaps** between trades = pair going cold (44.7% WR)
- **No single magic indicator**: All individual correlations < 0.06 — the edge is structural

### Hybrid TP Analysis (`hybrid_tp_analysis.py`)

Replayed all 15 live trades under 5 scenarios:
- Current (fixed 1.5R TP): +4.27R
- Hybrid entry (skip slip>0.5R): +3.50R (worse — skips winners too)
- Hybrid + Smart TP (trail at 1.5R): +7.44R (**BEST — adopted**)
- Reduced risk on low-vol: +4.52R (marginal)
- Limit at FC boundary: +9.17R but 40% WR (too many misses)

### Real-Time Intelligence Replay (`realtime_intelligence.py`)

1-minute resolution replay of all 15 trades testing 5 fakeout exit signals:
- **RECROSS**: 50% precision — fires on 7/7 losers AND 7/8 winners (KILLS WINNERS)
- **ADVERSE_CANDLE**: 56% precision — barely better than coin flip
- **STALL**: 40% — worse than random
- **R_DRAWDOWN**: 50% — same as RECROSS
- **C3_REVERSAL**: 100% precision — fires on 2/7 losers, 0/8 winners (**ONLY safe signal — adopted**)

Key finding: winners and losers look identical in the first 1-5 minutes. You CANNOT detect fakeouts reliably without killing real breakouts — except C3 reversal.

### Loser Forensics (`loser_forensics.py`)

Deep forensic analysis of all losing trades identifying:
- C2 body ratio, volume ratio, slip as separators between winners and losers
- Entry timing patterns and FC boundary interaction

---

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=strategy --cov=analysis --cov=backtest -v

# Pre-flight checks (live connection test)
python run_live.py --preflight

# Mainnet connection test (13 checks)
python test_mainnet.py
```

---

## FAQ

**Q: Why no indicators?**
A: Indicators are derivatives of price. The first candle's high/low IS the information.

**Q: Why only the first candle?**
A: The first candle captures the initial supply/demand equilibrium at session open.

**Q: Why smart trailing instead of fixed TP?**
A: Fixed 1.5R TP was leaving massive R on the table. Across 12,355 trades, mean peak R = 5.41R and 77% of trades peak above 1.5R. Fixed TP total: +334R. Guardian v3 trail (0.3R behind peak, activate at 1.0R): **+1,738R** — 5.2x improvement. The 1.5R cap was the single biggest drag on performance.

**Q: Why not move to breakeven?**
A: Data proved BE at ANY level hurts. BE at +0.75R shakes out 20% of trades that would have become full winners, costing -1,026R across 12,355 trades. Even BE at +1.0R costs -514R. The progressive SL tiers (T1: -0.25R at +0.5R, T2: BE at +0.75R) exist ONLY as an exchange crash safety net — the trail SL will always be tighter by the time a position reaches those levels.

**Q: What is C3 fakeout detection?**
A: After entry, the bot waits for the 3rd candle (C3) to close (~5 min). If C3's body reverses direction (e.g. bearish body on a long trade) AND the trade is negative (R < 0.3), the bot exits immediately at market. 1-minute resolution replay proved this has 100% precision: fires on 2/7 losers, 0/8 winners. Saves ~0.7R per detected fakeout.

**Q: Why not skip low-quality entries?**
A: 1-minute replay proved that entry quality filters (slip, volume, body ratio) also reject winners. Skipping trades with slip > 0.5R missed 3 real winners worth +4.5R. The x1000 path needs every single trade. Enter everything, exit fakeouts early.

**Q: Why flat 2% risk for all pairs?**
A: DNA analysis of 1,713 trades proved Class B pairs perform identically to Class A. Flat 2% maximises compounding ($1.93M vs $614K in backtest simulation).

**Q: Why not split entry / scale-in?**
A: Forensic analysis of 15 live trades showed scale-in amplified losses 50-80% (limit fills on losers, never fills on winners). DISABLED based on data.

**Q: What exchanges does it support?**
A: Live trading runs on **Bybit** (mainnet and demo). Backtesting downloads data from **Binance** via ccxt. The pair scanner supports any ccxt exchange.

**Q: What happens if equity drops below $500?**
A: The bot stops all trading. This is the equity floor — a hard safety stop.

**Q: Can I run multiple instances?**
A: No. A PID lock file (`live/bot.lock`) prevents duplicate instances. Only one bot can run at a time.

**Q: What if the bot crashes?**
A: All SL/TP orders are set directly on the exchange via `set_trading_stop`. If the bot dies, your positions are still protected by progressive SL tiers (exchange-side). The far 10R TP acts as a last-resort failsafe. When the Guardian v3 thread is running, it moves SL more aggressively (trail 0.3R behind peak) — but if it stops, the exchange tiers still protect you. On restart, the bot detects and cleans up stale orders.

**Q: What about clock drift / timestamp errors?**
A: Bybit rejects API requests when your system clock drifts >5s from their servers (`retCode:10002 invalid request`). The bot sets `recvWindow: 20000` (20s tolerance) to handle minor drift, and `adjustForTimeDifference: True` lets ccxt auto-correct. On Windows, ensure the Windows Time Service is running (see Clock Sync below). A ~1s drift caused trail SL updates to fail on a live position (STBL 2026-02-16), costing ~1R of captured profit.

### Clock Sync

Bybit's API validates request timestamps. If your system clock drifts, API calls will fail with `retCode:10002`. This is critical for the Profit Guardian, which updates SL every 2 seconds.

```powershell
# Windows — fix clock drift (run as Administrator):
net start w32time
w32tm /resync /force

# Verify:
w32tm /query /status
# "Last Successful Sync Time" should be recent
```

```bash
# Linux/macOS:
sudo ntpdate pool.ntp.org
# or: sudo timedatectl set-ntp true
```

The bot also sets `recvWindow: 20000` (20s) and `adjustForTimeDifference: True` as safety margins, but keeping your clock synced is essential for reliable trail execution.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Disclaimer

This software is provided for **educational and research purposes only**. It is not financial advice. Trading cryptocurrencies involves substantial risk of loss. Past performance (including backtested performance) does not guarantee future results. Always do your own research and never risk money you cannot afford to lose.
