# v13pro System Inventory

> Generated 2026-02-27 — Complete module-by-module inventory of `v13pro/`

---

## Directory Summary

| Metric | Value |
|--------|-------|
| Total `.py` files | **44** |
| Total Python lines | **~12,838** |
| Total `.json` files | **11** |
| Core bot class | `FCBBot` (bot.py, 1777 lines) |
| Strategies | 12 core + 2 lab (ORB, FCB) |
| Deploy combos | **175** entries |
| Current equity | **$522.73** |
| Current regime | **COLD** |
| Total trades | **229** (78W / 151L) |

---

## All Files in `v13pro/`

### Python Files (44)

| # | File | Lines | Purpose |
|---|------|-------|---------|
| 1 | `__init__.py` | 2 | Package init, `__version__ = "1.0.0"` |
| 2 | `__main__.py` | 4 | `python -m v13pro` entry point |
| 3 | `adaptive.py` | 574 | Adaptive Parameter Engine — zero hardcoded magic numbers |
| 4 | `aftermath.py` | 182 | Post-exit price tracker (1m/5m/15m/1h checkpoints) |
| 5 | `bot.py` | 1777 | **Main async 24/7 trading bot orchestrator** |
| 6 | `burst_engine.py` | 773 | Burst Detection & Exploitation Engine |
| 7 | `burst_optimizer.py` | 611 | Iterative Self-Optimization for Burst Engine |
| 8 | `calibrator.py` | 407 | Self-Calibration Engine (grade/TP/stationarity) |
| 9 | `combo_promoter.py` | 224 | Auto-promote/demote strategy combos from shadow |
| 10 | `config.py` | 318 | All configuration (risk, fees, curves, sessions) |
| 11 | `cross_sectional.py` | 151 | Cross-sectional correlated risk awareness |
| 12 | `directional.py` | 460 | Directional Intelligence (long vs short per regime) |
| 13 | `dna.py` | 482 | Setup DNA Profiler (pandas-based edge discovery) |
| 14 | `download_1m_data.py` | 209 | Download 1m OHLCV data from Bybit |
| 15 | `edge_radar.py` | 403 | Full Shadow Intelligence Exploitation |
| 16 | `exchange.py` | 238 | Async Bybit exchange wrapper (ccxt.pro) |
| 17 | `guardian.py` | 519 | Async position guardian (SL tiers, trailing, exits) |
| 18 | `hunter.py` | 154 | Async pair universe scanner |
| 19 | `indicators.py` | 64 | Indicator library (EMA, SMA, ATR, BB, RSI, etc.) |
| 20 | `journal.py` | 365 | Rich trade journal (JSONL + human-readable) |
| 21 | `learner.py` | 395 | Bayesian Feature Learner + Pair DNA Profiler |
| 22 | `lifecycle.py` | 318 | Per-Pair Lifecycle Classifier |
| 23 | `logger.py` | 598 | ANSI-colored console + daily file rotation logging |
| 24 | `micro_tf.py` | 332 | Micro-TF Intelligence (3m/5m cross-TF validation) |
| 25 | `momentum.py` | 364 | Momentum Alignment Detector (BTC/ETH/SOL) |
| 26 | `orderflow.py` | 185 | Order Flow Intelligence (orderbook microstructure) |
| 27 | `preflight.py` | 275 | Pre-launch validation checks |
| 28 | `regime.py` | 503 | Self-Calibrating Regime Detector & Exposure Modulator |
| 29 | `registry.py` | 95 | Combo registry + exit params |
| 30 | `run.py` | 72 | CLI entry point with argparse |
| 31 | `sentiment.py` | 256 | Real-time BTC/ETH/SOL momentum sentiment gauge |
| 32 | `session_lifecycle.py` | 265 | Session Lifecycle Manager (early/peak/late phases) |
| 33 | `shadow.py` | 525 | Shadow Trader (passive data collection for all signals) |
| 34 | `signal_quality.py` | 420 | Adaptive Signal Quality Scoring Engine |
| 35 | `skill.py` | 490 | PerformanceSkill: multi-factor conviction scoring |
| 36 | `state.py` | 292 | Thread-safe bot state with JSON persistence |
| 37 | `strat_orb_fcb.py` | 284 | ORB (Opening Range Breakout) + FCB (First Candle Breakout) |
| 38 | `strategies.py` | 184 | 12 strategies + ensemble scanning |
| 39 | `strategy_lab.py` | 430 | Strategy Laboratory for ORB & FCB graduation |
| 40 | `supervisor.py` | 253 | Process supervisor (heartbeat, restart, backoff) |
| 41 | `thesis.py` | 274 | Thesis Builder / Logger (pair×strategy knowledge base) |
| 42 | `trade_logger.py` | 40 | JSONL trade event logger |
| 43 | `watchdog.py` | 249 | Async health monitor (memory, disk, WS, guardian) |
| 44 | `ws_data.py` | 262 | Async WebSocket multi-TF data engine |

### JSON Files (11)

| File | Lines | Description |
|------|-------|-------------|
| `adaptive_state.json` | 15 | EWMA-smoothed adaptive parameter cache |
| `burst_optim_state.json` | 19 | Burst optimizer current param set |
| `burst_state.json` | 55 | BCS score, burst state, shadow validation |
| `calibrator_state.json` | 31 | Grade adjustments, stationarity, edge trend |
| `combo_promoter_state.json` | 138 | Promotion/demotion history |
| `deploy_combos.json` | 1507 | **175 strategy combos** (pair + strat + tf + exit) |
| `directional_state.json` | 1 | Directional risk multipliers per regime×side |
| `lifecycle_state.json` | 271 | Per-pair lifecycle scores |
| `regime_state.json` | 12 | Current regime (COLD), session multipliers |
| `state.json` | 1862 | **Bot state** (equity, positions, trades) |
| `thesis_state.json` | 6792 | 3400 recorded pair/strategy outcomes |

### Other

| File | Description |
|------|-------------|
| `burst_optim_history.jsonl` | Optimization iteration audit trail |
| `logs/` | Directory for journals, shadow data, events |

---

## Module Details

---

### 1. `__init__.py` (2 lines)

```python
"""v13pro -- FCB v13 PRO: Async multi-strategy 24/7 portfolio bot."""
__version__ = "1.0.0"
```

- **Classes:** None
- **Functions:** None
- **Constants:** `__version__`
- **Internal imports:** None

---

### 2. `__main__.py` (4 lines)

```python
"""Allow running as `python -m v13pro`."""
from v13pro.run import main
```

- **Classes:** None
- **Functions:** None
- **Internal imports:** `v13pro.run`

---

### 3. `adaptive.py` (574 lines)

> Adaptive Parameter Engine — ZERO hardcoded magic numbers. Every parameter computed from rolling shadow outcome data. EWMA-smoothed, O(1) lookups.

**Class: `AdaptiveParams`**
- `__init__()`, `refresh()`, `maybe_refresh()`
- `_load_shadow_outcomes()`, `_recompute()`
- `_compute_of_params()`, `_compute_key_level_params()`, `_compute_grade_multipliers()`
- `_compute_dna_cap()`, `_compute_pair_cooldowns()`, `_compute_tp_r()`
- `_smooth()`, `_load_state()`, `_save_state()`
- Properties: `of_block_threshold`, `of_boost`, `of_penalty`, `min_key_level_score`
- Properties: `conviction_multiplier()`, `dna_boost_cap`, `pair_cooldown()`, `optimal_tp_r()`
- `log_status()`, `get_summary()`

**Top-level:** `print_report()`

**Constants:** `MIN_SAMPLES=15`, `MIN_BUCKET=5`, `REFRESH_INTERVAL=1200`, `EWMA_ALPHA=0.3`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 4. `aftermath.py` (182 lines)

> Async post-exit price tracker. Checks price at 1m/5m/15m/1h after exit to detect premature TP, unnecessary SL, trail leaks.

**Class: `AftermathTracker`**
- `__init__()`, `start()`, `stop()`
- `schedule()`, `stats`
- `_loop()`, `_process_pending()`, `_build_checkpoint()`, `_finalize()`, `_get_price()`

**Constants:** `CHECKPOINTS_MIN=[1,5,15,60]`, `MAX_PENDING=200`

**Internal imports:** `v13pro.config`, `v13pro.logger`, `v13pro.journal`

---

### 5. `bot.py` (1777 lines) ⭐ CORE

> Main async 24/7 trading bot. Orchestrator tying together WS data, strategy scanning, combo matching, order execution, position management, pair discovery, risk management.

**Class: `FCBBot`**
- `__init__(dry_run, once)`
- `run()` — main entry point
- `shutdown()`
- `_heartbeat_loop()`, `_main_loop()`
- `_scan_all_tfs()`, `_scan_tf()`
- `_process_hunter_signals()`
- `_execute_signal()` — the trade execution path
- `_monitor_limit_fill()`
- `_on_position_closed()` — exit handler
- `_recover_positions()`
- `_build_subscriptions()`
- `_warmup()`
- `_heartbeat()`
- `_check_daily_summary()`

**Internal imports (20+):**
- `v13pro.config`, `v13pro.logger`, `v13pro.exchange`, `v13pro.trade_logger`, `v13pro.journal`
- `v13pro.state.BotState`, `v13pro.registry.ComboRegistry`
- `v13pro.strategies.scan_last_bar`, `v13pro.strategies.ensemble_signals`, `v13pro.strategies.Signal`, `v13pro.strategies.STRATEGIES`
- `v13pro.strat_orb_fcb.NEW_STRATEGIES`
- `v13pro.ws_data.WSDataEngine`, `v13pro.guardian.Guardian`, `v13pro.hunter.PairHunter`
- `v13pro.skill.PerformanceSkill`, `v13pro.aftermath.AftermathTracker`
- `v13pro.watchdog.Watchdog`, `v13pro.dna.SetupDNA`, `v13pro.dna.extract_features`
- `v13pro.sentiment.SentimentGauge`, `v13pro.orderflow.OrderFlowIntel`
- `v13pro.shadow.ShadowTrader`, `v13pro.signal_quality.SignalQualityEngine`
- `v13pro.combo_promoter.promotion_loop`, `v13pro.adaptive.AdaptiveParams`

---

### 6. `burst_engine.py` (773 lines)

> Evolutionary overlay: detects edge strengthening and exploits burst windows aggressively. Uses ECS (Edge Confidence Score), BCS (Burst Composite Score). Shadow-validated, asymmetric scaling.

**Class: `EdgeConfidence`**
- `__init__()`, `to_dict()`

**Class: `BurstEngine`**
- `__init__(lifecycle, cross_sect)`, `set_lifecycle()`, `set_cross_sectional()`
- `update_equity()`
- `risk_multiplier()`, `leverage_multiplier()`, `tp_multiplier()`
- Properties: `bcs`, `burst_state`, `shadow_validated`
- `get_combo_ecs()`, `get_pair_ecs()`
- `record_outcome()`, `maybe_refresh()`, `refresh()`
- `_refresh_combo_ecs()`, `_refresh_pair_ecs()`, `_decay_weighted_ecs()`
- `_validate_shadow()`, `_compute_bcs()`, `_update_burst_state()`
- `_compute_risk_mult()`, `_compute_leverage_mult()`, `_compute_tp_mult()`
- `_f_drawdown()`, `max_positions_multiplier()`
- `_ewma_update()`, `_load_shadow_outcomes()`, `_save_state()`, `_load_state()`
- `summary()`, `log_status()`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 7. `burst_optimizer.py` (611 lines)

> Iterative self-optimization for Burst Engine. Grid search + walk-forward evaluation + Monte Carlo confidence. Only promotes when statistically validated.

**Class: `ParamSet`** (dataclass)
- `weights_sum()`, `normalize_weights()`

**Class: `SimResult`** (dataclass)

**Class: `BurstOptimizer`**
- `__init__(burst_engine)`, `set_burst_engine()`
- `maybe_optimize()`, `summary()`, `log_status()`
- `_run_optimization()`, `_simulate()`, `_generate_candidates()`
- `_promote_params()`, `_apply_to_engine()`
- `_load_outcomes()`, `_save_state()`, `_load_state()`, `_record_iteration()`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 8. `calibrator.py` (407 lines)

> Self-Calibration Engine. Detects drift: grade miscalibration, TP leaks, temporal non-stationarity, edge decay. Read-only analysis — outputs adjustment multipliers.

**Class: `CalibrationState`**
- `__init__()`

**Class: `SelfCalibrator`**
- `__init__()`, `refresh()`, `maybe_refresh()`
- `grade_adjustment()`, `stationarity_index()`, `edge_trend()`, `health_score()`, `risk_multiplier()`
- `_calibrate_grades()`, `_calibrate_tp_leak()`, `_calibrate_stationarity()`
- `_calibrate_edge_trend()`, `_calibrate_conviction_correlation()`
- `_compute_health_score()`, `_ewma()`
- `_load_shadow_outcomes()`, `_save_state()`, `_load_state()`
- `summary()`, `log_status()`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 9. `combo_promoter.py` (224 lines)

> Auto-promote/demote strategy combos based on rolling shadow expectancy. Hourly review. Modifies `cfg.LIVE_COMBOS` in-memory.

**Classes:** None

**Functions:**
- `_load_shadow_longs()` — load/filter shadow outcomes
- `compute_combo_stats()` — WR, ExpR, N per combo
- `_load_state()`, `_save_state()`
- `review_combos()` — evaluate all combos
- `apply_review()` — execute promotions/demotions
- `promotion_loop()` — async background task
- `print_status()`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 10. `config.py` (318 lines) ⭐ CORE

> All configuration. Merged from obr/config.py × v13 fee model + guardian params. Self-contained.

**Classes:** None

**Functions:**
- `current_session_name(hour)`, `get_risk_pct(equity)`, `get_leverage(equity)`
- `get_current_phase(equity)`, `get_drawdown_multiplier(equity, peak_equity)`
- `get_max_concurrent(equity)`, `get_loss_streak_cooldown_hours(consecutive_losses)`
- `get_loss_streak_risk_mult(consecutive_losses)`

**Key Constants:**
- `MAINNET=True`, `LEVERAGE=8`, `RISK_PCT=0.02`, `FEE_RATE=0.00055`
- `START_EQUITY=500.0`, `TARGET_EQUITY=5000.0`
- `LONG_ONLY_MODE=True`, `MAX_CONCURRENT_POSITIONS=6`
- `TP_R=2.75`, `TRAIL_ACTIVATION_R=1.5`, `TRAIL_DISTANCE_R=0.50`
- `HUNTER_ENABLED=True`, `HUNTER_TRADE_ENABLED=True`
- `LIVE_COMBOS` — set of 18 (strategy, tf) tuples allowed for live trading
- `PROFIT_TIERS` — 5 progressive SL tiers
- `RISK_CURVE`, `LEVERAGE_CURVE`, `DRAWDOWN_THROTTLE`, `CONVICTION_MULTIPLIER`
- `SESSIONS` — asia (0-8), london (8-16), ny (16-24)
- `HUNTER_EXIT_MAP` — per-strategy exit mode routing

**Internal imports:** None (self-contained)

---

### 11. `cross_sectional.py` (151 lines)

> Cross-sectional awareness: detects correlated risk across active positions. Temporal entry spacing, loss clustering detection, emergency throttle.

**Class: `CrossSectionalAwareness`**
- `__init__()`, `record_entry()`, `record_loss()`
- `risk_multiplier()`, `summary()`, `log_status()`

**Constants:** `ENTRY_CLUSTER_WINDOW_SEC=3600`, `CLUSTER_RISK_DECAY=0.10`, `LOSS_CLUSTER_THRESHOLD=3`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 12. `directional.py` (460 lines)

> Directional Intelligence Engine. Discovers which market regimes favour which direction/timeframe from shadow data. Answers: "should we go long, short, or sit?"

**Class: `DirectionalIntelligence`**
- `__init__()`, `record_outcome()`, `_load_from_shadow()`, `_read_shadow_outcomes()`
- `maybe_refresh()`, `_recompute()`
- `is_side_allowed()`, `should_allow_shorts()`, `side_risk_multiplier()`
- `preferred_timeframes()`, `best_direction()`, `get_regime_direction_summary()`
- `log_status()`, `_save_state()`, `_load_state()`, `stats`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 13. `dna.py` (482 lines)

> Setup DNA Profiler: captures ~25 raw numeric indicator values per trade. Uses pandas to find exact indicator RANGES separating winners from losers.

**Function: `extract_features(candles, direction, entry_price, stop_dist)`** — ~25 raw features

**Class: `SetupDNA`**
- `__init__()`, `_load()`, `_save_csv()`, `_save_edges()`
- `record_entry()`, `record_outcome()`
- `_discover_edges()`, `get_conviction_boost()`
- `stats`, `get_edge_report()`, `get_full_analysis()`

**Constants:** `MIN_TRADES_FOR_ANALYSIS=15`, `EDGE_MIN_WR_LIFT=0.10`, `MAX_CONVICTION_BOOST=12`

**Internal imports:** `v13pro.config`, `v13pro.logger`, `v13pro.indicators`

---

### 14. `download_1m_data.py` (209 lines)

> Downloads 1-minute OHLCV candle data from Bybit for all portfolio pairs.

**Functions:**
- `_sanitize_pair()`, `_csv_path()`
- `get_all_bybit_pairs()`, `get_portfolio_pairs()`, `get_existing_5m_pairs()`
- `download_1m()`, `main()`

**Internal imports:** None (standalone script, uses ccxt directly)

---

### 15. `edge_radar.py` (403 lines)

> Full Shadow Intelligence Exploitation. Taps into ALL shadow data: checkpoint momentum, rolling peak_r, strategy×TF heat map, sentiment scoring, trough depth.

**Class: `EdgeRadar`**
- `__init__()`, `_load_from_files()`, `_ingest()`
- `_recalc()`, `_recalc_combo_heat()`, `_recalc_market_heat()`, `_recalc_hot_seat()`
- `combo_risk_multiplier()`, `combo_label()`, `is_combo_blocked()`
- `market_heat_multiplier()`, `market_heat_label()`
- `sentiment_risk_multiplier()`
- `is_hot_seat()`, `hot_seat_boost()`, `hot_combos()`, `cold_combos()`
- `record_outcome()`, `maybe_refresh()`
- `log_status()`, `summary()`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 16. `exchange.py` (238 lines)

> Async Bybit exchange wrapper using ccxt.pro. All exchange calls are async.

**Functions (all async unless noted):**
- `create_exchange()`, `close_exchange()`
- `get_equity()`, `get_available_balance()`
- `set_leverage()`, `set_margin_mode()`, `set_position_mode()`
- `get_market_info()` (sync), `round_qty()` (sync), `round_price()` (sync)
- `fetch_ohlcv()`, `fetch_latest_candles()`
- `place_market_order()`, `place_limit_order()`
- `fetch_order()`, `cancel_order()`
- `get_open_positions()`, `close_position()`, `partial_close_position()`
- `set_trading_stop()`, `fetch_closed_pnl()`
- `fetch_tickers()`, `fetch_funding_rate()`, `fetch_funding_rates_batch()`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 17. `guardian.py` (519 lines)

> Async position guardian. Progressive SL tiers, trailing stop, 1m rejection/engulfing exit, funding rate monitor, closed position resolution.

**Class: `Guardian`**
- `__init__(exchange, state, ws_data, on_position_closed)`
- `set_burst_engine()`, `start()`, `stop()`
- `track_position()`, `untrack_position()`, `tracked_count`, `tracked_symbols`
- `_loop()`, `_poll_all()`, `_poll_one()`
- `_check_1m_rejection()`, `_check_burst_partial_tp()`
- `_check_funding_rates()`, `_resolve_closed()`
- `_infer_reason()` (static)

**Function:** `_is_better_sl(direction, new_sl, current_sl)`

**Internal imports:** `v13pro.config`, `v13pro.logger`, `v13pro.journal`, `v13pro.ws_data`

---

### 18. `hunter.py` (154 lines)

> Async pair universe scanner. Scans ALL liquid Bybit USDT-perp pairs for fresh opportunities.

**Class: `PairHunter`**
- `__init__(exchange, registry, on_signals)`, `start()`, `stop()`
- `_loop()`, `_scan_universe()`, `_scan_pair()`
- `stats`

**Internal imports:** `v13pro.config`, `v13pro.logger`, `v13pro.strategies`, `v13pro.registry`

---

### 19. `indicators.py` (64 lines)

> Indicator library (exact match to discovery v13).

**Functions:**
- `ema(arr, period)`, `sma(arr, period)`, `atr(h, l, c, period)`
- `bollinger_bands(c, period, mult)`, `donchian_channels(h, l, period)`
- `rsi(c, period)`, `stochastic(h, l, c, k_period, k_smooth, d_smooth)`

**Internal imports:** None (uses numpy only)

---

### 20. `journal.py` (365 lines)

> Rich trade journal with timestamps for post-trade research. JSONL + human-readable formats.

**Functions:**
- `_ensure_dir()`, `_today()`, `_now_iso()`, `_now_ts()`
- `_write_jsonl()`, `_write_human()`
- `log_signal()`, `log_entry()`, `log_guardian_action()`, `log_exit()`
- `log_aftermath()`, `log_daily_summary()`, `log_event()`
- `read_journal()`, `read_all_exits()`, `read_all_aftermath()`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 21. `learner.py` (395 lines)

> Bayesian Feature Learner + Pair DNA Profiler. Beta-distribution conjugate priors for binary outcomes.

**Function:** `extract_features(...)` — extracts ~18 discrete features per trade

**Class: `BetaTracker`**
- `__init__(alpha, beta)`, `update()`, `posterior`, `edge`, `confidence`
- `to_dict()`, `from_dict()` (classmethod)

**Class: `PairDNA`**
- `__init__(symbol)`, `update()`, `status`, `to_dict()`, `from_dict()` (classmethod)

**Class: `BayesianLearner`**
- `__init__()`, `_load()`, `_save()`, `_hydrate()`
- `compute_adjustment()`, `get_pair_adjustment()`, `get_pair_status()`
- `get_hot_pairs()`, `get_cold_pairs()`
- `store_pending()`, `update()`, `get_insights()`, `log_status()`, `total_updates`

**Constants:** `MAX_BONUS=12.0`, `MAX_PENALTY=-10.0`, `PAIR_HOT_WR=0.58`, `PAIR_COLD_WR=0.38`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 22. `lifecycle.py` (318 lines)

> Per-Pair Lifecycle Classifier. Continuous lifecycle scores (0.0–1.0): expanding, compressing, improving, degrading, stable.

**Class: `PairLifecycle`**
- `__init__()`, `tp_multiplier`, `risk_multiplier`, `to_dict()`

**Class: `LifecycleTracker`**
- `__init__()`, `get_lifecycle()`, `tp_multiplier()`, `risk_multiplier()`
- `maybe_refresh()`, `refresh()`, `summary()`, `log_status()`
- `_compute_pair()`, `_ewma()`, `_load_shadow_outcomes()`, `_save_state()`, `_load_state()`

**Constants:** `REFRESH_INTERVAL=1200`, `EWMA_ALPHA=0.25`, `MIN_TRADES=10`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 23. `logger.py` (598 lines)

> Logging for v13pro. ANSI colored console + daily file rotation.

**Class: `C`** — ANSI color constants

**Class: `_ColorFmt`** (logging.Formatter) — `format()`

**Functions:**
- `_get_file_handler()`, `_rotate_if_needed()`
- `debug()`, `info()`, `warning()`, `error()`, `critical()`, `log_exception()`
- `header()`, `divider()`, `banner_box()`
- `position_opened()`, `position_closed()`
- `heartbeat()`, `dashboard()`

**Internal imports:** `v13pro.config`

---

### 24. `micro_tf.py` (332 lines)

> Micro-TF Intelligence Engine. Tracks 3m/5m shadow trades for real-time reliability. Cross-validates higher TF signals.

**Class: `MicroTFIntelligence`**
- `__init__()`, `record_outcome()`, `record_signal()`
- `cross_tf_multiplier()`, `cross_tf_conviction_boost()`
- `market_barometer()`, `is_micro_validated()`, `strategy_reliability()`
- `all_strategy_stats()`, `_has_fresh_micro_signal()`
- `_maybe_refresh()`, `_refresh_stats()`
- `summary()`, `log_status()`

**Constants:** `MICRO_TFS={"3m","5m"}`, `MACRO_TFS={"15m","30m","1h"}`, `MICRO_WINDOW=40`

**Internal imports:** None (uses `logging.getLogger("v13pro")`)

---

### 25. `momentum.py` (364 lines)

> Momentum Alignment Detector. Detects when BTC, ETH, SOL are unanimously aligned. ALIGNED → risk ×1.40, CONFLICTED → risk ×0.65.

**Class: `MomentumAlignment`**
- `__init__(sentiment_gauge, micro_tf)`, `set_sentiment()`, `set_micro_tf()`
- `update()`, `_compute_alignment()`
- Properties: `state`, `score`, `direction`, `is_aligned`, `is_conflicted`
- `risk_multiplier()`, `dd_floor()`, `should_use_config_conviction()`
- `is_combo_promoted()`, `bb_break_priority()`, `side_filter()`
- `summary()`, `log_status()`

**Constants:** `ALIGNMENT_COINS=["BTC","ETH","SOL"]`, `ALIGNED_THRESHOLD=0.80`

**Internal imports:** `v13pro.config`

---

### 26. `orderflow.py` (185 lines)

> Order Flow Intelligence. Captures orderbook microstructure at entry: spread, imbalance, depth ratio. Data collection for analysis.

**Class: `OrderFlowIntel`**
- `__init__(exchange)`, `stats`
- `snapshot()`, `_empty()`, `format_dashboard()`

**Constants:** `OB_DEPTH=20`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 27. `preflight.py` (275 lines)

> Pre-launch validation checks: imports, config sanity, combo file, exchange connectivity, market availability, state file, disk space, strategies, WS readiness.

**Class: `CheckResult`** (dataclass)
**Class: `PreflightResult`** (dataclass) — `add()`

**Functions:**
- `check_imports()`, `check_config()`, `check_api_keys()`, `check_combos()`
- `check_strategies()`, `check_state_file()`, `check_disk_space()`, `check_log_dir()`
- `check_exchange_connectivity()` (async), `check_markets()` (async)
- `run_sync()`, `run_full()` (async), `print_report()`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 28. `regime.py` (503 lines)

> Self-Calibrating Regime Detector. HOT/WARM/NORMAL/COOL/COLD regime from rolling shadow outcomes. Exposure multiplier 0.40x to 1.40x. Session-aware, Bayesian, EWMA smoothed.

**Class: `RegimeDetector`**
- `__init__()`, `record_outcome()`, `_load_from_shadow()`, `_read_shadow_outcomes()`
- `maybe_refresh()`, `_recompute()`, `_recompute_sessions()`
- `_ewma()`, `_session_ewma()`
- `exposure_multiplier()`, `regime`, `regime_mult`, `session_mult()`
- `stats`, `summary()`, `log_status()`
- `_load_state()`, `_save_state()`

**Top-level:** `print_report()`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 29. `registry.py` (95 lines)

> Combo registry. Loads deploy_combos.json, normalizes pairs/TFs, provides lookups.

**Constants:**
- `EXIT_PARAMS` — dict of 12 exit modes (fix0.5 through trl_tight)
- `_BYBIT_REMAP` — 1000X denomination mapping (BONK, FLOKI, PEPE, etc.)
- `_SKIP_PAIRS` — `{'OM'}`

**Functions:** `_normalise_tf()`, `_normalise_pair()`

**Class: `ComboRegistry`**
- `__init__(combo_file)`, `_load()`
- `get_combos(pair, tf)`, `get_pairs_for_tf(tf)`
- Properties: `all_pairs`, `all_tfs`, `n_combos`, `combos`

**Internal imports:** `v13pro.config`

---

### 30. `run.py` (72 lines)

> CLI entry point. Parses args (--dry-run, --once, --maker, --entry, --supervised, --demo, --combos).

**Function:** `main()`

**Internal imports:** `v13pro.config`, `v13pro.bot`, `v13pro.supervisor`, `v13pro.preflight`

---

### 31. `sentiment.py` (256 lines)

> Real-time BTC/ETH/SOL momentum sentiment gauge. EMA-8 vs EMA-21 slope, structure analysis, trend position. Outputs: bull/bear/neutral with confidence.

**Functions:** `_ema()`, `_coin_bias()`

**Class: `SentimentGauge`**
- `__init__(ws_data, exchange)`
- `get_sentiment(force)` (async), `get_cached()`
- `_get_ohlcv()` (async)

**Constants:** `_SENTIMENT_SYMBOLS` (BTC, ETH, SOL), `_EMA_FAST=8`, `_EMA_SLOW=21`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 32. `session_lifecycle.py` (265 lines)

> Session Lifecycle Manager. Tracks per-session performance. Phases: EARLY (3h), PEAK (3h), LATE (2h). Risk modulation and TP adjustments.

**Class: `SessionTracker`**
- `__init__()`, `record_trade()`
- `risk_multiplier()`, `tp_multiplier()`, `should_stop_trading()`
- `phase`, `session_name`
- `_check_session_reset()`, `_update_phase()`, `_evaluate_conditions()`
- `summary()`, `log_status()`

**Constants:** `EARLY_MULT=1.00`, `PEAK_MULT=1.00`, `LATE_BASE_MULT=0.50`

**Internal imports:** `v13pro.config`

---

### 33. `shadow.py` (525 lines)

> Shadow Trader. Simulates entries on ALL signals without real orders. Tracks TP/SL hits, checkpoint prices, orderflow/sentiment snapshots. The DATA GOLDMINE.

**Functions:** `_ensure_dir()`, `_today()`, `_write_shadow()`

**Class: `ShadowTrader`**
- `__init__(exchange, ws_data, ...)`, `set_thesis_logger()`, `set_regime_detector()`
- `set_directional()`, `set_edge_radar()`, `set_micro_tf()`, `set_strategy_lab()`
- `start()`, `stop()`, `stats`
- `record_signal()` — the main entry point
- `_loop()`, `_process_pending()`, `_build_checkpoint()`, `_finalize()`
- `_get_price()` (async)

**Constants:** `CHECKPOINTS_MIN=[1,5,15,60]`, `MAX_SHADOW=500`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 34. `signal_quality.py` (420 lines)

> Adaptive Signal Quality Scoring Engine. Per-(strategy, tf) statistics across 4 dimensions: 1m confirmation, sentiment, grade, OF alignment. Produces quality_multiplier (0.5x–2.0x).

**Class: `SignalQualityEngine`**
- `__init__()`, `reload()`, `_maybe_refresh()`
- `_load_outcomes()`, `_compute_stats()`
- `_calc_wr()`, `_calc_avg_r()`
- `_compute_1m_buckets()`, `_compute_sentiment_buckets()`, `_compute_grade_buckets()`, `_compute_of_buckets()`
- `score_signal()`, `get_1m_stats()`
- `_classify_sentiment()`, `_classify_of()`
- `get_stats_summary()`

**Constants:** `MIN_SAMPLES=8`, `REFRESH_INTERVAL=600`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 35. `skill.py` (490 lines)

> PerformanceSkill: multi-factor conviction scoring (0–100) + agentic self-tuning filter. Key-level engine (swing H/L, pivots, round numbers, VWAP, session levels).

**Functions (key-level engine):**
- `_find_swing_highs()`, `_find_swing_lows()`, `_aggregate_period()`
- `_classic_pivots()`, `_round_numbers()`, `_vwap_approx()`, `_session_levels()`
- `detect_key_levels()`

**Functions (scoring):**
- `_score_key_level()`, `_score_signal_quality()`, `_score_volume()`, `_score_trend()`, `_score_fee()`
- `score_setup()` — the main scorer

**Class: `PerformanceSkill`**
- `__init__()`, `set_adaptive()`
- `_load_memory()`, `_save_memory()`, `evaluate()`, `record_outcome()`
- `_recalibrate()`, `min_conviction`, `stats`, `log_status()`, `learner`

**Constants:** `RECAL_INTERVAL=10`, `MIN_CONVICTION_DEFAULT=55`

**Internal imports:** `v13pro.config`, `v13pro.logger`, `v13pro.learner`

---

### 36. `state.py` (292 lines)

> Thread-safe bot state with JSON persistence.

**Class: `BotState`**
- `__init__()`, `set_adaptive()`
- `_load()`, `_default()`, `_save()`
- Properties: `equity`, `peak_equity`, `pending_entries`, `pending_count`
- Properties: `daily_growth_pct`, `pnl_today_r`, `pnl_today_usd`, `wins_today`, `losses_today`, `entries_today`
- `update_equity()`, `check_new_day()`
- `can_trade()` — main gate check (cooldowns, limits, etc.)
- `can_trade_hunter()`
- `record_entry()`, `get_consecutive_losses()`, `record_outcome()`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 37. `strat_orb_fcb.py` (284 lines)

> ORB (Opening Range Breakout, 15m, NY-only) + FCB (First Candle Breakout, 5m, all sessions). Shadow-only until graduation.

**Functions:**
- `S_orb(o,h,l,c,v,a,mk)` — ORB strategy
- `S_fcb(o,h,l,c,v,a,mk)` — FCB strategy
- `get_confirmations(strategy, o,h,l,c,v,a, bar_idx)` — rich confirmation metadata

**Constant:** `NEW_STRATEGIES = {"ORB": S_orb, "FCB": S_fcb}` (registered into STRATEGIES by bot.py)

**Internal imports:** `v13pro.indicators`, `v13pro.strategies.Signal`, `v13pro.strategies.msl`

---

### 38. `strategies.py` (184 lines)

> 12 strategies + ensemble (exact match to discovery v13).

**Class: `Signal`** — `(bar, side, entry, stop_dist, strategy, pair, tf)`

**Functions:** `msl(price, atr_val, maker)` — minimum stop loss

**12 Strategy Functions:**
1. `S_ema_rib()` — EMA Ribbon pullback
2. `S_bb_break()` — Bollinger Band breakout
3. `S_donchian()` — Donchian channel breakout
4. `S_rsi_fade()` — RSI fade (mean reversion)
5. `S_bb_fade()` — BB fade (mean reversion)
6. `S_stoch_x()` — Stochastic crossover
7. `S_pin_bar()` — Pin bar reversal
8. `S_ib_break()` — Inside bar breakout
9. `S_engulf()` — Engulfing pattern
10. `S_mtf_rsi()` — Multi-TF RSI
11. `S_tr_pull()` — Trend pullback
12. `S_mom_surge()` — Momentum surge

**Functions:**
- `scan_last_bar()` — scan all strategies on last bar
- `ensemble_signals()` — combine multi-strategy agreement

**Constant:** `STRATEGIES` — dict mapping name → function

**Internal imports:** `v13pro.indicators`

---

### 39. `strategy_lab.py` (430 lines)

> Strategy Laboratory for ORB & FCB. Tracks confirmation power, optimal SL, session/pair affinity, leverage readiness, graduation criteria.

**Class: `StrategyLab`**
- `__init__()`, `record_outcome()`
- `is_graduated()`, `graduation_report()`, `get_stats()`
- `leverage_recommendation()`, `confirmation_power()`
- `summary()`, `_refresh_stats()`, `_save_state()`, `_load_state()`, `write_report()`

**Constants:** `LAB_STRATEGIES={"ORB","FCB"}`, `GRAD_MIN_TRADES=50`

**Internal imports:** `v13pro.config`

---

### 40. `supervisor.py` (253 lines)

> Process supervisor. Heartbeat monitoring, anti-flap backoff, log watching, position guard, PID lockfile, incident logging.

**Class: `LogWatcher`**
- `__init__(log_dir)`, `update()`, `has_fatal`, `reset()`, `close()`

**Class: `Supervisor`**
- `__init__(bot_args)`, `run()`, `_start_bot()`, `_stop_bot()`, `_monitor_bot()`

**Functions:** `_log_incident()`, `_has_open_positions()`, `_write_pid()`, `_remove_pid()`

**Constants:** `HEARTBEAT_STALE_SECS=600`, `MAX_RESTART_PER_HOUR=6`, `BACKOFF_SCHEDULE=[10,30,60,120,300]`

**Internal imports:** None (self-contained, reads state.json directly)

---

### 41. `thesis.py` (274 lines)

> Thesis Builder/Logger. Captures which pair+strategy+side combinations win. Builds knowledge base over time.

**Class: `ThesisLogger`**
- `__init__()`, `_load_state()`, `_save_state()`, `_log_event()`
- `record_outcome()`, `maybe_print_summary()`
- `get_best_strategy()`, `get_pair_affinity()`, `save()`

**Top-level:** `print_report()`

**Constants:** `SUMMARY_INTERVAL=1800`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 42. `trade_logger.py` (40 lines)

> JSONL trade event logger. Lightweight append-only event log.

**Functions:**
- `_ensure()`, `_path()`, `_append()`
- `log_entry()`, `log_exit()`, `log_guardian()`, `log_signal()`

**Internal imports:** `v13pro.config`

---

### 43. `watchdog.py` (249 lines)

> Async health monitor. Network, memory (2GB soft limit), WS health, guardian heartbeat, disk space, equity snapshots, log retention.

**Class: `Watchdog`**
- `__init__(bot, ws_data, guardian, state)`, `start()`, `stop()`
- `is_healthy()`, `stats`
- `_loop()`, `_run_checks()`
- `_check_memory()`, `_check_ws_health()`, `_check_guardian_health()`, `_check_disk()`
- `_equity_snapshot()` (async), `_cleanup_old_logs()`, `_alert()`

**Constants:** `CHECK_INTERVAL=60`, `MEMORY_SOFT_LIMIT_MB=2000`, `LOG_RETENTION_DAYS=14`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

### 44. `ws_data.py` (262 lines)

> Async WebSocket multi-TF data engine. Maintains live candle buffers for ALL portfolio pairs across ALL timeframes.

**Class: `WSDataEngine`**
- `__init__(max_candles)`, `start(subscriptions)`, `stop()`
- `_prefetch_history()` (async), `_watch_tf()` (async)
- `get_candles()`, `get_arrays()`, `get_1m_candles()` (all async)
- `drain_closes()`, `wait_for_close()` (async)
- `stats`, `is_ready`

**Constants:** via config: `WS_CANDLE_BUFFER=220`

**Internal imports:** `v13pro.config`, `v13pro.logger`

---

## JSON Data Snapshots

### `deploy_combos.json` — 175 entries

First 3 entries:
```json
[
  {"pair": "DOGE", "strat": "RSI_FADE", "tf": "15m", "exit": "fix1.2", "val_wr": 100.0, "test_wr": 80.0, "avg_r": 0.6685},
  {"pair": "FLOKI", "strat": "RSI_FADE", "tf": "15m", "exit": "fix1.2", "val_wr": 100.0, "test_wr": 25.0, "avg_r": -0.5238},
  {"pair": "HBAR", "strat": "RSI_FADE", "tf": "15m", "exit": "fix1.2", "val_wr": 100.0, "test_wr": ...}
]
```
Fields per entry: `pair`, `pair_raw`, `strat`, `tf`, `exit`, `val_pf`, `val_wr`, `val_R`, `test_pf`, `test_wr`, `test_R`, `avg_r`

### `state.json` — Current Bot State

```
equity:           $522.73
peak_equity:      $567.28
date:             2026-02-27
day_start_equity: $482.81
entries_today:    11
wins_today:       6  |  losses_today: 5
pnl_today_r:      +3.17R
pnl_today_usd:    +$2.26
total_trades:     229  (78W / 151L = 34.1% WR)
pending_entries:  [ENSO, RIVER, ...]
```

### `regime_state.json`

```
regime:       COLD
regime_mult:  0.45
session_mults: london=0.89, ny=0.72, asia=0.72
```

---

## Internal Dependency Map

```
bot.py ─────────────┬── config.py (standalone)
(orchestrator)      ├── logger.py ← config
                    ├── exchange.py ← config, logger
                    ├── trade_logger.py ← config
                    ├── journal.py ← config, logger
                    ├── state.py ← config, logger
                    ├── registry.py ← config
                    ├── indicators.py (standalone, numpy)
                    ├── strategies.py ← indicators
                    ├── strat_orb_fcb.py ← indicators, strategies
                    ├── ws_data.py ← config, logger
                    ├── guardian.py ← config, logger, journal, ws_data
                    ├── hunter.py ← config, logger, strategies, registry
                    ├── skill.py ← config, logger, learner
                    │     └── learner.py ← config, logger
                    ├── aftermath.py ← config, logger, journal
                    ├── watchdog.py ← config, logger
                    ├── dna.py ← config, logger, indicators
                    ├── sentiment.py ← config, logger
                    ├── orderflow.py ← config, logger
                    ├── shadow.py ← config, logger
                    ├── signal_quality.py ← config, logger
                    ├── combo_promoter.py ← config, logger
                    ├── adaptive.py ← config, logger
                    ├── regime.py ← config, logger
                    ├── burst_engine.py ← config, logger
                    │     └── burst_optimizer.py ← config, logger
                    ├── calibrator.py ← config, logger
                    ├── lifecycle.py ← config, logger
                    ├── cross_sectional.py ← config, logger
                    ├── directional.py ← config, logger
                    ├── edge_radar.py ← config, logger
                    ├── micro_tf.py (logging only)
                    ├── momentum.py ← config
                    ├── session_lifecycle.py ← config
                    ├── strategy_lab.py ← config
                    └── thesis.py ← config, logger

run.py ← config, bot, supervisor, preflight
supervisor.py (standalone — reads files directly)
download_1m_data.py (standalone script)
```

---

## Architecture Summary

**44 Python modules, ~12,838 lines total.** A fully async (asyncio) 24/7 crypto trading bot for Bybit USDT perpetuals, with:

- **12+2 strategies** scanning across 15m, 30m, 1h timeframes
- **Shadow trading** on ALL signals (including rejected) for continuous learning
- **7 intelligence overlays**: Regime, Burst, Adaptive, Calibrator, Lifecycle, Edge Radar, Directional
- **4 risk modulators**: Cross-sectional, Session lifecycle, Momentum alignment, Micro-TF
- **Self-optimizing**: Bayesian learning, DNA profiling, burst optimization, combo auto-promotion
- **Production infrastructure**: Supervisor, watchdog, preflight checks, journal system
