# FCB Bot — Project Tracker
> **Single source of truth.** Read this FIRST every session.
> **v13pro is the ONLY active bot.** The `live/` directory is the OLD FCB bot — do not use.

## GOAL
$485 → $5,000 (x10) → $150,000 (x1000) via v13pro 12-strategy ensemble on **Bybit** futures (mainnet, real money).

## CURRENT STATUS — LIVE (as of 2026-02-27)

**Bot is LIVE on Bybit mainnet.** Mode: **v13pro multi-strategy + intelligence suite**

| Metric | Value |
|---|---|
| Equity | **$482.66** |
| Peak Equity | **$567.28** |
| Drawdown | **14.9%** |
| All-Time Trades | 218 (72W / 146L) |
| All-Time Win Rate | 33.0% |
| Active Combos | 38 across 28 pairs |
| Timeframes | 15m, 30m, 1h |
| Leverage | 8x (equity-curve scaled) |
| Risk | 2.0% per trade (conviction-weighted) |
| Max Concurrent | 6 portfolio + 5 hunter |
| Margin | Isolated per position |
| Maker TP | ON |
| Maker Entry | ON |

### Intelligence Suite (all data-driven from 4,787 shadow outcomes)

| Module | Status | Key Metric |
|---|---|---|
| **Shadow Trader** | ✅ Active | 4,787 outcomes tracked, 21 strategy/tf combos |
| **Regime Detector** | ✅ COOL (0.70x) | Session mults: london=1.05x, asia=0.76x, ny=1.40x |
| **DirectionalIntel** | ✅ Active | 1,113 outcomes, 14 buckets. BEAR→longs 73% WR, BULL→shorts 51% WR |
| **EdgeRadar** | ✅ Active | Market=COLD, 7 HOT combos, 1 FROZEN (TR_PULL/15m), 7 COLD combos |
| **Burst Engine** | ✅ NORMAL | BCS=0.498, risk/lev/tp at 1.00x |
| **Calibrator** | ✅ Active | Health=0.85, edge_trend=+0.132, risk_mult=0.91x |
| **Sentiment** | ✅ BEAR | score=-0.249, BTC↓ ETH→ SOL→ |
| **OrderFlow** | ✅ Active | L2 orderbook snapshots |
| **Adaptive** | ✅ Active | 4,787 outcomes, 9 smoothed params |
| **Lifecycle** | ✅ Active | 32 pairs scored, 3 expanding, 5 degrading |

### System Configuration
| Setting | Value |
|---|---|
| Exchange | Bybit mainnet (isolated margin, **8x** leverage) |
| Strategies | 12 (EMA_RIB, BB_BREAK, DONCHIAN, RSI_FADE, BB_FADE, STOCH_X, PIN_BAR, IB_BREAK, ENGULF, MTF_RSI, TR_PULL, MOM_SURGE) |
| Timeframes | 15m, 30m, 1h |
| Pairs | 28 portfolio + universe scanning (hunter) |
| Risk | **2.0%** per trade (RISK_CURVE, consistent across all equity levels) |
| TP_R | Adaptive per combo (BB_BREAK/15m=2.3R, BB_FADE/15m=2.0R, etc.) |
| Trail | trl_tight: activates at 1.5R, 0.5R distance |
| Max Positions | 6 portfolio + 5 hunter (separate pools) |
| Skill Min | 75 (conviction threshold) |
| COLD_REGIME_FREEZE | **OFF** — regime multiplier (0.70x) scales risk instead |
| Directional Gate | Adaptive from shadow data (replaces old LONG_ONLY_MODE) |
| EdgeRadar FROZEN block | ON — blocks combos with WR<25% + N>=20 (currently TR_PULL/15m) |

## KEY DECISIONS (chronological)

### Original FCB Phase (2026-02-21)
1. Kill 15m timeframe — 14.3% WR in live
2. 12% risk (Kelly) — DNA analysis + Kelly Criterion
3. Trail mode re-enabled
4. 20x leverage

### v13pro Phase (2026-02-22+)
5. **Migrated to v13pro** — 12-strategy ensemble replacing single FCB
6. **2% risk, 8x leverage** — fixed death spiral from 3% risk + escalating leverage
7. **trl_tight trail params** — 1.5R activation, 0.5R distance (was 1.0R/0.3R catching at sub-1R)
8. **PROFIT_TIERS redesigned** — progressive SL advancement matching trail params
9. **DirectionalIntelligence** — adaptive side filtering from shadow (BEAR→longs 73% WR)
10. **EdgeRadar** — full shadow intelligence: combo heat, market heat, sentiment edge, hot seat detection
11. **COLD_REGIME_FREEZE disabled** — was blocking ALL trades; now regime multiplier scales risk instead
12. **EdgeRadar deadlock fixed** — threading.Lock → RLock (summary() was deadlocking heartbeat)

## ARCHITECTURE
```
v13pro/ (ACTIVE — all live trading code):
├── run.py              # Entry point: python -m v13pro.run --maker --entry
├── bot.py              # Main async orchestrator (1795 lines)
├── config.py           # All parameters (risk curves, leverage, sessions, etc.)
├── strategies.py       # 12 strategy algorithms
├── registry.py         # Combo registry + exit parameters
├── skill.py            # Multi-factor conviction scorer (0-100)
├── dna.py              # 25-feature DNA profiler
├── exchange.py         # Bybit async API (ccxt)
├── guardian.py         # Position guard: SL tiers, trail, 1m rejection, funding
├── hunter.py           # Universe scanner for non-portfolio scalps
├── state.py            # Thread-safe persistent state
├── ws_data.py          # Async WebSocket multi-TF candle buffers
├── sentiment.py        # BTC/ETH/SOL momentum gauge
├── orderflow.py        # L2 orderbook microstructure
├── shadow.py           # Passive shadow trader (ALL signals tracked)
├── watchdog.py         # System health monitor
├── regime.py           # Self-calibrating regime detector (HOT/WARM/NORMAL/COOL/COLD)
├── directional.py      # Adaptive directional intelligence from shadow
├── edge_radar.py       # Full shadow intelligence (combo heat, market heat, sentiment edge, hot seat)
├── adaptive.py         # Data-driven parameter adaptation
├── signal_quality.py   # Signal quality scoring from shadow outcomes
├── calibrator.py       # Self-calibrator (edge health, stationarity, risk adjustment)
├── lifecycle.py        # Per-pair lifecycle tracking (expanding/compressing/drifting)
├── cross_sect.py       # Cross-sectional awareness (entry/loss clustering)
├── burst.py            # Burst engine (BCS scoring, BURST/NORMAL/DECAY states)
├── burst_optim.py      # Burst optimizer (Phase 2A iterative self-tuning)
├── combo_promoter.py   # Auto-promote shadow combos to live
├── indicators.py       # Technical indicators (EMA, BB, RSI, Stoch, etc.)
├── logger.py           # Colored console + file logging with rich dashboard
├── journal.py          # Trade journal
├── trade_logger.py     # Structured JSONL trade logger
├── aftermath.py        # Post-trade analysis
├── learner.py          # Adaptive learning from outcomes
├── preflight.py        # Pre-flight system checks
├── supervisor.py       # Bot supervisor / restart logic
└── logs/               # Log files, shadow JSONL, state files

live/ (OLD FCB bot — DO NOT USE):
└── bot.py, config.py, etc.  # Legacy single-strategy system
```

## COMPLETED MILESTONES
- [x] Core v13pro 12-strategy ensemble engine
- [x] Conviction scoring (0-100, 5 factors, DNA boost)
- [x] Shadow trader (passive tracking of ALL signals → 4,787 outcomes)
- [x] Regime detector (self-calibrating from shadow, session-aware)
- [x] DirectionalIntelligence (adaptive side filtering per sentiment regime)
- [x] EdgeRadar (combo heat, market heat, sentiment edge, hot seat)
- [x] Burst engine + optimizer (BCS scoring, BURST/NORMAL/DECAY)
- [x] Self-calibrator (edge health, stationarity, risk adjustment)
- [x] Lifecycle tracker (per-pair expansion/compression/drift)
- [x] Cross-sectional awareness (entry/loss clustering)
- [x] Adaptive parameter engine (data-driven from shadow)
- [x] Hunter scalp system (separate pool, 0.75R TP)
- [x] WebSocket data (event-driven, zero REST polling)
- [x] Guardian position management (trail, SL tiers, funding, 1m exit)
- [x] Rich terminal dashboard (heartbeat every 60s)
- [x] Trail fix: 1.5R activation, 0.5R distance
- [x] Risk/leverage death spiral fix: 2%/8x consistent
- [x] COLD regime freeze → disabled, regime multiplier handles scaling

## NEXT STEPS
1. **Monitor live performance** — validate EdgeRadar + DirectionalIntel impact
2. **Track win rate by combo** — compare live vs shadow predictions
3. **Regime transition** — when shadow WR improves, regime will auto-warm
4. **Scale risk** — once regime hits WARM/HOT, risk multiplier increases automatically
5. **Target $5,000** — at 2% risk with 8x leverage, ~50 winning trades

## RISK AWARENESS
- At 2% risk per trade, each loss costs ~$9.65 at current equity
- Risk is further scaled by regime (0.70x), calibrator (0.91x), session, directional
- Effective risk per trade: ~1.27% after all multipliers in COOL regime
- ~50 consecutive full losses needed to reach ruin — statistically impossible
- All SL/TP orders are exchange-side; positions protected even if bot dies

## LAUNCH COMMAND
```powershell
.\.venv\Scripts\python.exe -u -m v13pro.run --maker --entry
```

**DO NOT use `run_live.py`** — that launches the old FCB bot.
