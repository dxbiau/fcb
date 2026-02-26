# v13Pro System Review — 7-Phase Architectural Assessment

**Date:** 2025-01-XX  
**Dataset:** 4,770 shadow outcomes (2,747 longs), 737 passed longs  
**Objective:** Transform v13Pro into a self-aware, non-hardcoded trading intelligence  

---

## Phase 1: Hardcoding Risk Diagnosis

### 1.1 Critical Hardcoded Parameters Identified

| Parameter | Location | Current Value | Risk Level |
|-----------|----------|---------------|------------|
| `TP_R` | config.py | 2.75 | **HIGH** — overridden by exit_params per combo but still static per combo |
| `TRAIL_ACTIVATION_R` | config.py | 1.5 | MEDIUM — same trail threshold for all market conditions |
| `TRAIL_DISTANCE_R` | config.py | 0.50 | MEDIUM — doesn't adapt to volatility regime |
| `PROFIT_TIERS` | config.py | 6 fixed tiers | **HIGH** — same progressive SL for all regimes |
| `CONVICTION_MULTIPLIER` | config.py | A+=1.50, A=1.15 etc | **HIGH** — C grade outperforms A+ (see data) |
| Strategy indicators | strategies.py | EMA(8,21,55), BB(20,2.0) etc | LOW — changing these causes regime breaks |
| `EXIT_PARAMS` | registry.py | 12 fixed exit modes | **HIGH** — wrong TPs cost up to 0.557R/trade |
| `DRAWDOWN_THROTTLE` | config.py | 5 stepped tiers | MEDIUM — could be smooth curve |
| `LOSS_STREAK_RISK_MULT` | config.py | 3 discrete steps | LOW — already conservative |
| Session time windows | config.py | Fixed UTC ranges | LOW — already neutralized in regime.py |
| `RISK_CURVE` | config.py | 5 equity steps | LOW — grow slowly, acceptable |

### 1.2 Already Adaptive (Credit)

- **AdaptiveParams (adaptive.py):** OF thresholds, key level min, grade multipliers, DNA cap, pair cooldowns (16 pairs), optimal TP_R (11 combos), conviction multiplier
- **RegimeDetector (regime.py):** Self-calibrating exposure modulation (COOL/WARM/HOT states), unbiased session multipliers from rolling data
- **PerformanceSkill (skill.py):** Self-tuning min_conviction every 10 trades, BayesianLearner per-pair adjustments

### 1.3 Vulnerability Assessment

**Biggest edge leak: TP rigidity.** The data proves it:

| Combo | Actual ExpR | Optimal TP | Optimal ExpR | **Gain/Trade** |
|-------|-------------|------------|-------------|----------------|
| BB_FADE/15m | +0.431R | 2.50R | +0.988R | **+0.557R** |
| IB_BREAK/15m | +0.285R | 3.50R | +0.549R | **+0.264R** |
| BB_BREAK/15m | +0.593R | 3.50R | +0.858R | **+0.265R** |
| DONCHIAN/15m | +0.296R | 3.50R | +0.475R | **+0.179R** |
| TR_PULL/15m | +0.204R | 1.50R | +0.343R | **+0.139R** |
| EMA_RIB/15m | +0.130R | 2.50R | +0.233R | **+0.103R** |

Already near-optimal: BB_FADE/1h (gain=0), TR_PULL/1h (gain=0), EMA_RIB/1h (+0.020R)

---

## Phase 2: Market Lifecycle Modeling

### 2.1 Volatility Regime Analysis

| Quartile | N | WR | ExpR | Avg Stop % |
|----------|---|-----|------|-----------|
| Q1 (tight) | 686 | **59.8%** | **+0.278** | 0.486% |
| Q2 | 687 | 52.5% | +0.163 | 0.731% |
| Q3 | 687 | 51.8% | +0.079 | 1.277% |
| Q4 (wide) | 687 | 53.0% | +0.182 | 4.864% |

**Finding:** Tight-stop environments (low vol) massively outperform. Q1 delivers 3.5× the expectancy of Q3.

### 2.2 Pair Lifecycle Drift (Top 15 by drift magnitude)

| Pair | N | WR | ExpR | Old WR | New WR | **Drift** |
|------|---|-----|------|--------|--------|-----------|
| TAO | 35 | 48.6% | -0.023 | 29.4% | 66.7% | **+37.3%** |
| SUI | 145 | 60.0% | +0.215 | 41.7% | 78.1% | **+36.4%** |
| POWER | 161 | 57.8% | +0.304 | 71.2% | 44.4% | **-26.8%** |
| XAUT | 171 | 59.6% | +0.142 | 70.6% | 48.8% | **-21.8%** |
| SOL | 147 | 57.8% | +0.202 | 47.9% | 67.6% | **+19.6%** |

**Finding:** Pairs are NOT stationary. Lifecycle state changes are massive (±37%). Fixed risk allocation to drifting pairs is a structural vulnerability.

### 2.3 Temporal Non-Stationarity

| Window | WR | ExpR |
|--------|-----|------|
| 0-100 | 63.0% | +0.473 |
| 100-200 | 72.0% | +0.666 |
| 200-300 | 60.0% | +0.450 |
| 300-400 | **42.0%** | **-0.249** |
| 400-500 | 54.0% | +0.091 |
| 500-600 | **79.0%** | **+0.657** |
| 600-700 | 64.0% | +0.379 |

**Finding:** System WR oscillates 42–79% in 100-trade windows. Fixed parameters during the 42% phase are destroying capital.

---

## Phase 3: Dynamic TP & Risk Adaptation

### 3.1 Exit Mode Leakage

| Mode | N | WR | ExpR | Avg Peak | Avg Win | **Leak** |
|------|---|-----|------|----------|---------|----------|
| fix2.0 | 464 | 58.2% | +0.287 | 1.20R | 1.04R | 0.16R |
| trl_tight | 228 | 59.6% | +0.298 | 1.26R | 1.02R | **0.24R** |
| fix1.5 | 45 | 57.8% | +0.239 | 1.02R | 0.90R | 0.12R |

**Finding:** Trail exit is paradoxically leaking MORE than fixed TP (0.24R vs 0.16R). Trail activates then gets stopped out on retraces before reaching optimal exit.

### 3.2 Strategy/TF Peak Distribution

| Combo | P25 | P50 | P75 | P90 |
|-------|-----|-----|-----|-----|
| BB_BREAK/15m | 0.74 | 1.52 | 2.27 | 3.20 |
| BB_FADE/15m | 0.65 | 1.10 | 2.22 | 3.00 |
| DONCHIAN/15m | 0.59 | 1.12 | 1.79 | 2.50 |
| EMA_RIB/15m | 0.35 | 0.93 | 1.82 | 2.71 |
| TR_PULL/15m | 0.28 | 0.55 | 0.98 | 1.63 |
| BB_BREAK/1h | 0.38 | 0.85 | 1.24 | 1.80 |
| TR_PULL/1h | 0.23 | 0.45 | 0.86 | 1.12 |

**Finding:** Peak distributions vary enormously across strategy/TF. A single TP for all is provably suboptimal.

### 3.3 Prescription

- Dynamic optimal TP per strategy/TF (already partially in adaptive.py — needs lifecycle modulation)
- Vol-regime TP scaling: contract TP in tight regimes (captures more), expand in volatile regimes
- Risk scaling: reduce risk for degrading pairs, increase for improving pairs

---

## Phase 4: Cross-Sectional Self-Awareness

### 4.1 Loss Clustering

| Metric | Value |
|--------|-------|
| Avg losses in 1hr window | **28.9** |
| Max losses in 1hr window | **40** |
| Pairs with ≥30 passed longs | 12 |

**Finding:** Losses are heavily clustered. When one pair loses, many pairs lose simultaneously. This indicates correlated market events affecting multiple positions.

### 4.2 Prescription

- Track active entry timestamps across all pairs
- When entries cluster in time, reduce subsequent entry sizes (diminishing marginal risk)
- Monitor correlated drawdowns in open positions
- Emergency exposure reduction when simultaneous loss count exceeds threshold

---

## Phase 5: Self-Calibration Engine

### 5.1 Conviction Grade Paradox

| Grade | N | WR | ExpR |
|-------|---|-----|------|
| C | 30 | **63.3%** | **+0.674** |
| B | 121 | 59.5% | +0.247 |
| A | 428 | 58.4% | +0.245 |
| A+ | 179 | 57.5% | +0.335 |

**Finding:** C grade (lowest conviction that passes) has BEST performance. The conviction scoring is either:
- Penalizing setups that actually have edge (false negatives)
- Rewarding complexity over simplicity (overfitting)
- Not weighting the right features for actual outcome prediction

### 5.2 Prescription

- Periodically compute actual grade-vs-performance mapping
- Auto-adjust grade multipliers toward observed performance ratios
- Log calibration drift for visibility
- Detect when conviction score becomes anti-predictive (negative correlation with outcomes)

---

## Phase 6: Shadow Trader Intelligence

### 6.1 Shadow Scale

- 4,770 total outcomes (2,747 longs)
- 737 passed longs (actually traded or would have traded)
- Coverage across 31+ pairs, 3 TFs, 12+ strategies

### 6.2 Prescription

- Track shadow-vs-live performance divergence per combo
- Detect combos where shadow WR >> live WR (execution issues)
- Detect combos where live WR >> shadow WR (lucky streak that will mean-revert)
- Feed shadow edge decay into real-time combo confidence scoring

---

## Phase 7: Anti-Rigidity Design Principles

### 7.1 Rules (Enforced in All New Code)

1. **No magic numbers** — Every threshold must be derivable from shadow data with a config fallback
2. **EWMA decay** — All computed parameters must use exponential weighted moving averages (forgetting old data)
3. **Probabilistic not binary** — Output continuous multipliers (0.3–1.5×), never hard bool gates
4. **Reversible** — Every module can be disabled with a single flag
5. **Modular** — Each module is a standalone class with `set_*`/`get_*` interface
6. **Computationally scalable** — O(1) per pair lookup after periodic refresh (no per-tick computation)
7. **Logged** — All calibration changes logged with before/after values

---

## Implementation Modules

### New modules:
1. **`lifecycle.py`** — Per-pair lifecycle scoring (expanding/compressing/improving/degrading)
2. **`cross_sectional.py`** — Cluster risk detection + temporal entry spacing
3. **`calibrator.py`** — Self-calibration engine (grade recalibration, TP leak detection, stationarity index)

### Enhanced modules:
4. **`adaptive.py`** — Lifecycle-modulated TP, vol-regime risk scaling
5. **`bot.py`** — Wire lifecycle, cross-sectional, calibrator into execution pipeline

### Design constraints met:
- ✅ No structural overhaul
- ✅ All probabilistic and reversible
- ✅ Computationally scalable (periodic refresh, O(1) lookups)
- ✅ Modular overlays only
- ✅ Never block signal flow (only modulate sizing/TP)
