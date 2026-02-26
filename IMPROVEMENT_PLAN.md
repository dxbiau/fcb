# FCB Improvement Plan — From 3 to ≥12 Qualifying Pairs

**Baseline:** 3 pairs (CYS/asia, OPEN/asia, DUSK/london) | 1.41 TPD | 0.507R weighted avg | **325.6 days** to 1000×

## ✅ EXECUTED — ACTUAL RESULTS (2026-02-12)

### Step 1: Relaxed filters + multi-session on top 60 → **10 sessions PASS** (147 days)
### Step 2: Extended scan on top 200 → **22 sessions PASS across 20 unique pairs** (82 days)

| Pair | Session | Trades | WR | Net Exp (R) | Fee_R | TPD | PRR | Prov? |
|------|---------|--------|----|-------------|-------|-----|-----|-------|
| CYS/USDT | asia | 31 | 74.2% | 0.790 | 0.065 | 0.53 | 163 | |
| OPEN/USDT | asia | 78 | 57.7% | 0.378 | 0.064 | 0.51 | 161 | |
| ACU/USDT | asia | 16 | 56.3% | 0.368 | 0.039 | 0.80 | 97 | PROV |
| IRYS/USDT | london | 30 | 56.7% | 0.347 | 0.070 | 0.43 | 175 | |
| MYX/USDT | london | 66 | 54.5% | 0.304 | 0.059 | 0.39 | 148 | |
| 4/USDT | asia | 46 | 54.3% | 0.301 | 0.058 | 0.38 | 145 | |
| STBL/USDT | london | 55 | 54.5% | 0.295 | 0.068 | 0.37 | 170 | |
| IRYS/USDT | ny | 36 | 55.6% | 0.295 | 0.055 | 0.47 | 138 | |
| DUSK/USDT | london | 66 | 54.5% | 0.284 | 0.079 | 0.38 | 199 | |
| NOM/USDT | ny | 41 | 53.7% | 0.280 | 0.073 | 0.32 | 183 | |
| FOGO/USDT | ny | 19 | 52.6% | 0.271 | 0.045 | 0.59 | 111 | PROV |
| BREV/USDT | asia | 21 | 52.4% | 0.264 | 0.045 | 0.51 | 114 | PROV |
| LIT/USDT | asia | 25 | 52.0% | 0.243 | 0.057 | 0.63 | 143 | PROV |
| COLLECT/USDT | london | 18 | 50.0% | 0.205 | 0.045 | 0.44 | 113 | PROV |
| 币安人生/USDT | asia | 51 | 51.0% | 0.203 | 0.072 | 0.46 | 179 | |
| MYX/USDT | asia | 66 | 50.0% | 0.199 | 0.060 | 0.38 | 151 | |
| DOOD/USDT | asia | 76 | 48.7% | 0.187 | 0.059 | 0.44 | 148 | |
| MON/USDT | asia | 43 | 51.2% | 0.180 | 0.068 | 0.35 | 171 | |
| FLUID/USDT | asia | 55 | 49.1% | 0.171 | 0.071 | 0.40 | 177 | |
| SNX/USDT | ny | 53 | 49.1% | 0.164 | 0.062 | 0.31 | 156 | |
| WET/USDT | asia | 33 | 48.5% | 0.156 | 0.056 | 0.54 | 141 | |
| F/USDT | london | 57 | 49.1% | 0.153 | 0.075 | 0.33 | 187 | |

**Portfolio: 22 sessions | 20 unique pairs | 9.95 TPD | 0.285R weighted avg | 82 days to 1000×**
**Confidence: 17 full, 5 provisional**

### Top near-misses (watchlist — blocked by trade count < 15)
| Pair | Session | Net Exp | TPD | Trades | Est. days to 15 trades |
|------|---------|---------|-----|--------|----------------------|
| ACU/USDT | london | 0.976 | 0.26 | 5 | ~38 |
| SPACE/USDT | ny | 0.717 | 0.56 | 10 | ~8 |
| SKR/USDT | ny | 0.640 | 0.53 | 9 | ~11 |
| FOGO/USDT | asia | ~0.45 | ~0.50 | 9 | ~12 |
| SKR/USDT | asia | 0.464 | 0.50 | 10 | ~10 |
| ZAMA/USDT | london | 0.459 | 0.31 | 9 | ~19 |
| SPACE/USDT | london | 0.332 | 0.56 | 9 | ~10 |
| AIA/USDT | asia | 0.314 | 0.50 | 9 | ~12 |

*Re-scan watchlist command: `python pair_scanner.py --watchlist SPACE,SKR,FOGO,ZAMA,AIA,FIGHT,ELSA,SPORTFUN`*

---

## 1. Quick Wins (< 30 min each, no strategy change)

| #  | Action | Effort | Expected gain |
|----|--------|--------|---------------|
| Q1 | **Lower `F_NET_EXP_MIN` from 0.25 → 0.15** | 1 line edit | +3 pairs immediately (F, DOOD, MON from existing stage-2 data) |
| Q2 | **Lower `F_TRADES_MIN` from 30 → 15** | 1 line edit + add `provisional` flag | +3 more pairs (COLLECT, BREV, LIT) — small samples flagged but tradeable |
| Q3 | **Test ALL 3 sessions per pair, not just best** | ~40 lines changed in `stage2()` | Adds 2–4 extra sessions from CYS, DUSK, COLLECT (see §5) |
| Q4 | **Extend stage-2 scan from top 60 → top 200** | CLI flag only: `--top 200` | 341 untested candidates remain; next 30 have PRR 69–96 (excellent range) |

**Combined Q1+Q2+Q3:** moves from 3 → **~13 qualifying sessions** without downloading a single new pair.

---

## 2. Filter Tuning

### Current hard filters (stage 2)
```
F_PRR_MAX     = 200    → KEEP (anything above = fee-death)
F_FEE_MAX     = 0.10   → KEEP (mathematical ceiling)
F_NET_EXP_MIN = 0.25   → LOWER to 0.15
F_TPD_MIN     = 0.3    → KEEP (below 0.3 = no compounding)
F_TRADES_MIN  = 30     → LOWER to 15 (+ provisional flag)
```

### Current soft pre-filters (stage 1)
```
S1_PRR_MAX      = 180  → KEEP
S1_FEE_MAX      = 0.09 → KEEP
S1_RANGE_MIN    = 0.0035 → LOWER to 0.003 (match MIN_RANGE_PCT exactly)
S1_BREAKOUT_MIN = 0.20   → LOWER to 0.15
```

### Justification for each change

| Filter | Old | New | Data evidence |
|--------|-----|-----|---------------|
| `F_NET_EXP_MIN` | 0.25 | 0.15 | 6 pairs have net_exp in [0.15, 0.25) — positive edge, just smaller. At 0.15R + 3% risk → 0.45% compounding per trade, still profitable. |
| `F_TRADES_MIN` | 30 | 15 | Many pairs are newly listed (< 90 days data). 15+ trades gives 95% CI on net_exp of ±0.42R at WR=55% — wide but directionally reliable. Flag as `provisional`. |
| `S1_RANGE_MIN` | 0.35% | 0.30% | The strategy minimum is 0.3% (`MIN_RANGE_PCT`). Stage-1 was killing pairs that would actually trade. Align the two. |
| `S1_BREAKOUT_MIN` | 20% | 15% | On a 7-day sample, 15% = 1 breakout in 7 sessions. Pairs with lower breakout can still have positive edge if the breakouts that do happen are high-WR. |

### Filters NOT changed (and why)

| Filter | Value | Reason |
|--------|-------|--------|
| `F_PRR_MAX` | 200 | PRR directly determines fee_R. At PRR=200, fee_R=0.08R → only 0.28R net on TP. Loosening further destroys edge. |
| `F_FEE_MAX` | 0.10 | Mathematical ceiling. fee_R > 0.10 means net_TP < 1.40R → WR must exceed 42% just to break even. |
| `F_TPD_MIN` | 0.3 | Below 0.3 tpd = fewer than 1 trade per 3 days. Compounding too slow to matter. |

### Simulated impact on current stage-2 data

| Scenario | Pairs passing | notes |
|----------|---------------|-------|
| Current (strict) | 3 | CYS, OPEN, DUSK |
| `trades >= 15` only | 4 | +BREV |
| `net_exp >= 0.15` only | 6 | +F, DOOD, MON |
| Both relaxed | 9 | +COLLECT, BREV, LIT, F, DOOD, MON |
| `net_exp >= 0.10` + trades ≥ 15 | 15 | 38 total have positive net_exp |

---

## 3. Target Pair List

### Tier 1 — Currently passing (backtest-confirmed)

| Pair | Session | Trades | WR | Net Exp (R) | TPD | PRR | x1000 days |
|------|---------|--------|----|-------------|-----|-----|------------|
| CYS/USDT | asia | 31 | 74.2% | 0.790 | 0.53 | 163 | 562 |
| OPEN/USDT | asia | 78 | 57.7% | 0.378 | 0.51 | 161 | 1,209 |
| DUSK/USDT | london | 66 | 54.5% | 0.284 | 0.38 | 199 | 2,170 |

### Tier 2 — Unlock with relaxed filters (existing data, needs re-run to confirm)

| Pair | Est. Session | Stage-1 PRR | Stage-1 Fee_R | Blocking filter |
|------|-------------|-------------|---------------|-----------------|
| COLLECT/USDT | asia/ny/london | 42–56 | 0.017–0.022 | trades < 30 |
| BREV/USDT | best | ~55 | ~0.022 | trades < 30 |
| LIT/USDT | best | ~90 | ~0.036 | trades < 30 |
| F/USDT | best | ~100 | ~0.040 | net_exp < 0.25 |
| DOOD/USDT | best | ~75 | ~0.030 | net_exp < 0.25 |
| MON/USDT | best | ~96 | ~0.038 | net_exp < 0.25 |

### Tier 3 — Multi-session adds (needs backtest confirmation)

| Pair | New Session | Stage-1 Med PRR | Expected viability |
|------|------------|-----------------|-------------------|
| CYS/USDT | london | 43 | Excellent — lower PRR than confirmed asia session |
| CYS/USDT | ny | 84 | Good — moderate PRR |
| DUSK/USDT | asia | 59 | Excellent — lower PRR than confirmed london session |
| DUSK/USDT | ny | 86 | Good — moderate PRR |
| COLLECT/USDT | ses2 | 45 | Excellent — 3 sessions all sub-56 PRR |
| COLLECT/USDT | ses3 | 56 | Good |

### Tier 4 — Extended scan targets (untested, best stage-1 scores)

Top 15 untested pairs with lowest stage-1 fee_R:

| Pair | Best Session | Med PRR | Med Fee_R | Breakout% |
|------|-------------|---------|-----------|-----------|
| B/USDT | asia | 82 | 0.033 | 28.6% |
| C98/USDT | asia | 82 | 0.033 | 28.6% |
| IRYS/USDT | ny | 82 | 0.033 | 37.5% |
| ARC/USDT | ny | 83 | 0.033 | 37.5% |
| 4/USDT | ny | 84 | 0.034 | 50.0% |
| ALICE/USDT | ny | 83 | 0.033 | 25.0% |
| ATA/USDT | asia | 85 | 0.034 | 28.6% |
| VELVET/USDT | london | 87 | 0.035 | 28.6% |
| BIRB/USDT | asia | 88 | 0.035 | 57.1% |
| BEAT/USDT | ny | 89 | 0.035 | 50.0% |
| ALLO/USDT | asia | 90 | 0.036 | 57.1% |
| ALCH/USDT | ny | 90 | 0.036 | 50.0% |
| LA/USDT | asia | 90 | 0.036 | 57.1% |
| NOM/USDT | asia | 90 | 0.036 | 57.1% |
| ANIME/USDT | asia | 90 | 0.036 | 57.1% |

*At 15% expected pass rate → ~8 new qualifying pairs from next 200 tested.*

---

## 4. Near-Miss Handling

### Pairs within 1 filter of passing (from stage-2 run)

| Pair | Session | Net Exp | Trades | Blocking filter | Action |
|------|---------|---------|--------|-----------------|--------|
| ACU/USDT | best | 0.976 | 5 | trades=5 (< 15) | **Monitor** — re-scan in 30 days |
| SPACE/USDT | best | 0.717 | 10 | trades=10 (< 15) | **Monitor** — re-scan in 20 days |
| SKR/USDT | ny+london | 0.640 | 9 | trades=9 (< 15) | **Monitor** — re-scan in 15 days |
| RIVER/USDT | ny+asia | 0.400+ | ~12 | trades (< 15) | **Monitor** — close to threshold |
| STABLE/USDT | best | ~0.30 | ~12 | trades (< 15) | **Monitor** |

### Monitoring protocol

1. **Weekly re-scan** (stage 2 only on near-miss pairs): `--watchlist ACU,SPACE,SKR,RIVER,STABLE`
2. Flag transitions: when a near-miss crosses `trades >= 15`, auto-promote to Tier 2
3. **New listing scan**: run stage 1 monthly to catch new Binance listings (typically 2–5 per month)

---

## 5. Session Optimisation

### Current problem
`stage2()` picks the **single best session** per pair and discards the rest. This wastes viable secondary sessions.

### Multi-session data (from `scan_all_scores.csv`)

| Pair | Session | Med PRR | Breakout% | Viable? |
|------|---------|---------|-----------|---------|
| **CYS** | asia ✅ | 163 | 43% | CONFIRMED |
| **CYS** | london | 43 | 29% | **YES** — PRR lower than confirmed session |
| **CYS** | ny | 84 | 38% | **YES** — good PRR |
| **DUSK** | london ✅ | 199 | 25% | CONFIRMED |
| **DUSK** | asia | 59 | 29% | **YES** — excellent PRR |
| **DUSK** | ny | 86 | 25% | **Borderline** — PRR OK |
| **OPEN** | asia ✅ | 161 | 35% | CONFIRMED |
| **OPEN** | london | 136 | 29% | **Marginal** — high PRR, test but likely fails |
| **OPEN** | ny | 202 | 25% | **No** — PRR above hard limit |
| **COLLECT** | asia | 42 | 29% | **YES** — exceptional PRR |
| **COLLECT** | ny | 56 | 25% | **YES** — excellent |
| **COLLECT** | london | 45 | 14% | **Maybe** — low breakout rate |

### Expected session additions
- CYS: +2 sessions (london, ny)
- DUSK: +1–2 sessions (asia confirmed viable, ny borderline)
- COLLECT: +2–3 sessions (all three have excellent PRR)
- Other Tier 2 pairs: +1 each on average

**Total multi-session boost: +4 to +8 extra session-slots**

---

## 6. Code Changes

### Change 1: Relax hard filters (pair_scanner.py, lines 49–53)

```python
# BEFORE
F_PRR_MAX     = 200
F_FEE_MAX     = 0.10
F_NET_EXP_MIN = 0.25
F_TPD_MIN     = 0.3
F_TRADES_MIN  = 30

# AFTER
F_PRR_MAX     = 200
F_FEE_MAX     = 0.10
F_NET_EXP_MIN = 0.15        # was 0.25 — 6 pairs in [0.15, 0.25) have positive edge
F_TPD_MIN     = 0.3
F_TRADES_MIN  = 15           # was 30 — flag as provisional, many new listings have < 90 days
```

### Change 2: Relax stage-1 soft filters (pair_scanner.py, lines 56–59)

```python
# BEFORE
S1_PRR_MAX       = 180
S1_FEE_MAX       = 0.09
S1_RANGE_MIN     = 0.0035
S1_BREAKOUT_MIN  = 0.20

# AFTER
S1_PRR_MAX       = 180
S1_FEE_MAX       = 0.09
S1_RANGE_MIN     = 0.003     # was 0.0035 — align with MIN_RANGE_PCT
S1_BREAKOUT_MIN  = 0.15      # was 0.20 — allow 1-in-7 breakout on sample
```

### Change 3: Test ALL qualifying sessions per pair (replace stage2 best-session logic)

In `stage2()` (around line 490), replace the best-session-only logic with per-session independent evaluation:

```python
# BEFORE (lines 483-496)
        best = None
        best_sess = None
        for sess in SESSIONS:
            trades = backtest_pair_session(pair, df, sess)
            m = compute_metrics(trades)
            if m is None:
                continue
            if best is None or m["net_exp"] > best["net_exp"]:
                best = m
                best_sess = sess

        if best is not None:
            best["pair"] = pair
            best["session"] = best_sess
            all_results.append(best)

# AFTER
        for sess in SESSIONS:
            trades = backtest_pair_session(pair, df, sess)
            m = compute_metrics(trades)
            if m is None:
                continue
            m["pair"] = pair
            m["session"] = sess
            all_results.append(m)
            status = "PASS" if (
                m["prr"] < F_PRR_MAX and m["avg_fee"] < F_FEE_MAX and
                m["net_exp"] >= F_NET_EXP_MIN and m["tpd"] >= F_TPD_MIN and
                m["total"] >= F_TRADES_MIN
            ) else "fail"
            prov = " [PROVISIONAL]" if m["total"] < 30 else ""
            print(f"  {sess}: netR={m['net_exp']:.3f} feeR={m['avg_fee']:.3f} "
                  f"PRR={m['prr']:.0f} tpd={m['tpd']:.2f} [{status}{prov}]")
```

### Change 4: Add `--extended` flag and `--watchlist` support

```python
# In CLI section, add:
parser.add_argument("--extended", action="store_true",
                    help="Run stage 2 on top 200 pairs instead of 60")
parser.add_argument("--watchlist", type=str, default="",
                    help="Comma-separated pair tickers to re-scan (e.g. ACU,SPACE,SKR)")

# In main:
if args.extended:
    args.top = 200

if args.watchlist:
    # Direct stage-2 on specific pairs
    pairs = [p.strip() + "/USDT:USDT" for p in args.watchlist.split(",")]
    # ... download and backtest each
```

### Change 5: Add provisional flag to output

In the results printout and CSV, add a `provisional` column:
```python
r["provisional"] = r["total"] < 30
```

---

## 7. Projected Outcome

### Scenario model

| Scenario | Sessions | Total TPD | Weighted Avg Exp | Days to 1000× |
|----------|----------|-----------|-----------------|----------------|
| **A: Current** | 3 | 1.41 | 0.507R | **325.6** |
| **B: +Multi-session only** | 5 | 2.21 | 0.441R | **238.3** |
| **C: +Relaxed filters only** | 9 | 3.31 | 0.315R | **222.0** |
| **D: C + Multi-session** | 13 | 4.66 | 0.296R | **168.0** |
| **E: D + Extended scan (+8)** | 21 | 7.46 | 0.252R | **122.9** |

### Sensitivity: how many extended-scan pairs to reach target days

Starting from Scenario D (13 sessions) and adding pairs at estimated 0.18R net:

| Target | Extra pairs needed | Total sessions |
|--------|-------------------|----------------|
| 200 days | 0 | 13 |
| 150 days | 3 | 16 |
| 120 days | 9 | 22 |
| **100 days** | **15** | **28** |
| 80 days | 24 | 37 |

If extended-scan pairs average 0.25R instead:

| Target | Extra pairs needed | Total sessions |
|--------|-------------------|----------------|
| 150 days | 2 | 15 |
| 120 days | 7 | 20 |
| **100 days** | **11** | **24** |
| 80 days | 18 | 31 |

### Achievability assessment

| Milestone | Probability | Time to execute |
|-----------|-------------|-----------------|
| **≥ 12 sessions** (Scenario D) | **90%** — filter relaxation + multi-session on existing data | 30 min |
| **≥ 20 sessions** (Scenario E) | **60%** — requires extended scan finding ~8 new pairs from 341 untested | 2–3 hours |
| **Sub-120 days** | **50%** — needs 22 sessions at current avg exp | 3 hours |
| **Sub-100 days** | **25%** — needs 28 sessions (or fewer at higher avg exp) | 3–4 hours + lucky scan results |
| **Sub-80 days** | **< 10%** — needs 37 sessions; unlikely from current universe | Would require different approach |

### Honest constraint

The weighted average net expectancy **dilutes** as we add more pairs. The current 3 pairs average 0.507R because CYS is exceptional (0.79R). Every marginal pair added has ~0.15–0.20R, pulling the portfolio average down. This is the fundamental tension:

> **More pairs = more TPD but lower avg exp. The compounding math fights you.**

To break sub-100, we need either:
1. **28+ sessions at 0.18R avg** — brute-force volume approach
2. **24+ sessions at 0.25R avg** — requires finding several more CYS-quality outliers
3. A **qualitatively different lever** not available under frozen config (wider timeframe, trailing TP, etc.)

### Recommended execution order

1. **Apply code changes 1–3** (filter relaxation + multi-session) → re-run `--stage2 --top 60` → confirm ≥12 sessions → **30 min**
2. **Apply code changes 4–5** (extended + watchlist) → run `--stage2 --top 200` → find 5–12 extra → **2–3 hours**
3. **Set up weekly watchlist re-scan** for near-misses → ongoing
4. **Evaluate** whether sub-100 target is worth pursuing or whether ~120–150 days is the practical floor under frozen config

---

*Generated from scan data: 551 pairs scored (stage 1), 59 pairs backtested (stage 2), 641 candidate sessions analysed.*
*Projection model: `projection_model.py` — deterministic compounding at 3% risk per trade.*
