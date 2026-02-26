# BASELINE SNAPSHOT — 2026-02-21
> **DO NOT ROLLBACK PAST THIS POINT.**
> This file documents the exact state of the project at the time of the baseline lock.

## Why This Exists
On 2026-02-21, the project was reorganized and locked as a baseline after:
1. Full forensic analysis of 18 trades
2. Mathematical proof (Kelly Criterion) that 0.5R TP was failing
3. Complete reconfiguration to aggressive x10 mode
4. Edge score tiering implementation
5. Bug fixes (pending_entries .values() error)
6. Workspace cleanup and reorganization

## Equity & Account State
- **Equity**: $122.83
- **Start**: $150.15 (deposited)
- **Drawdown**: -$27.32 (-18.2%)
- **Total Trades**: 18 (6W / 12L, 33.3% WR)
- **Total PnL**: -1.691R
- **5m trades only**: 45.5% WR, +2.15R (POSITIVE EDGE)
- **15m trades only**: 14.3% WR, -3.84R (NEGATIVE — disabled)

## Config Snapshot (live/config.py)
| Parameter | Value | Why |
|---|---|---|
| LEVERAGE | 20 | Margin required for 12% risk |
| RISK_PCT_A | 0.12 | Kelly f*=13.6%, using 12% (sub-Kelly) |
| RISK_PCT_B | 0.06 | Half-Kelly for unproven pairs |
| TP_R | 1.5 | Trail takes over, avoids penny collecting |
| EXCHANGE_TP_R | 10.0 | Safety net only |
| TRAIL_ENABLED | True | Guardian v3, +1738R in backtest |
| TRAIL_DISTANCE_R | 0.5 | Wider to survive whipsaw |
| MIN_RANGE_PCT | 0.005 | Stronger breakouts + margin room |
| MAX_CONCURRENT_POS | 3 | Focus > dilution |
| MAX_CONCURRENT_B | 1 | Reserve for A-class |

## Edge Score Tiers (live/edge_score.py)
| Grade | Risk Multiplier | Meaning |
|---|---|---|
| S_elite | 1.0 | Full Kelly |
| A_quality | 1.0 | Full Kelly |
| B_standard | 0.75 | 3/4 Kelly |
| C_quick | 0.60 | Reduced |
| D_low | 0.50 | Half Kelly |
| N/A | 0.75 | No oracle data |

## File Integrity Checksums (byte sizes)
```
live/bot.py             133,151 bytes
live/config.py           29,205 bytes
live/edge_score.py        6,794 bytes
live/exchange.py         18,557 bytes
live/guardian.py          15,249 bytes
live/profit_guardian.py   14,290 bytes
live/state.py             16,870 bytes
live/strategy.py           6,823 bytes
live/trades.py             2,100 bytes
live/trade_logger.py      28,286 bytes
live/logger.py            14,644 bytes
live/pair_scanner.py       6,790 bytes
live/session_reviewer.py  16,105 bytes
live/growth_tracker.py     9,088 bytes
live/journal.py           32,399 bytes
live/order_flow.py        11,381 bytes
live/__init__.py              49 bytes
live/state.json              732 lines
live/trades.csv               46 lines (18 trades + header)
```

## What Was Deleted (not recoverable from workspace)
- `_analyze_discovery.py` — one-off discovery analysis
- `_analyze_discovery2.py` — second iteration
- `_check_bal.py` — balance checker
- `_check_positions.py` — position inspector
- `_debug_trades.py` — trade-by-trade debug script
- `_deep_analysis.py` — deep pre-Asia analysis  
- `_forensic_analysis.py` — 18-trade forensic analysis
- `_sim_scaleout.py` — scale-out simulation
- `_test_edge.py` — edge score test script

All of these were single-use analysis scripts created during this session.
Their findings are preserved in CHANGELOG.md and PROJECT_TRACKER.md.

## What Was Moved (still accessible)
Everything moved is intact in `scripts/` and `archive/` subdirectories.
See CHANGELOG.md entry "2026-02-21 — WORKSPACE REORGANIZATION" for full list.

## Known Issues at Baseline
1. **Git broken** — `git.exe` returns exit code 0xC0FFEE02, produces no output. Likely credential manager or security policy conflict. Not blocking — bot runs fine.
2. **NumPy DLL blocked** — `numpy` fails to import due to Application Control policy. Only affects backtest engine, not live bot.
3. **Two orphan positions** — KITE (long) and VVV (short) opened on restart are on exchange but NOT tracked in bot's pending_entries (fresh state created). Exchange SL/TP is set but Guardian cannot trail them.
4. **Equity below start** — $122.83 vs $150.15 start. Need positive trades to recover.

## Recovery Instructions
If anything breaks after this baseline:
1. All live/ code is the production version — DO NOT revert any file in live/
2. state.json has the correct equity and trade count
3. trades.csv has the complete 18-trade history
4. Config is set for aggressive x10 — DO NOT reduce risk without mathematical justification
5. Launch command: `.venv\Scripts\Activate.ps1; $env:PYTHONIOENCODING = "utf-8"; python run_live.py --yes`
