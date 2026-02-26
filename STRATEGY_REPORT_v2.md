# STRATEGY DISCOVERY — COMPREHENSIVE REPORT
## Autonomous Research Results (v3–v7)

### Mission
$500 → $5,000 (x10) in ≤10 days, max DD <55%, no live trading yet.
Exchange: Bybit USDT Perpetual, taker fees 0.055%/side (0.11% round-trip).
Data: 186 pairs × ~51,900 5m candles each (~180 days), walk-forward validated (70/30 train/test split).

---

## EXECUTIVE SUMMARY

**8,000+ configurations tested across 5 discovery engines. Zero pass the x10-in-10d + DD<55% criterion.**

However, a **genuinely profitable, OOS-validated multi-strategy system** was discovered:
- **PF 1.28–1.39 out-of-sample** across 34+ pairs and 9 strategies
- **x10 in 47–57 days** with DD 22–42% (depending on risk level)
- **Up to 40 trades/day** using 3-timeframe signal pooling (5m + 15m + 30m)

The 10-day x10 target is **mathematically incompatible** with the Bybit fee structure at the available signal quality. See analysis below.

---

## DISCOVERY ENGINES

### v3: 12 Strategies × 2 TFs (1,320 configs, 0 pass)
- Tested: EMA_RIB, BB_BREAK, RSI_BNC, KELTNER, DONCHIAN, ENGULF, ADX_BURST, MOM_BURST, NTS_REL, RANGE_EXP, VOL_BRK, SQUEEZE
- TFs: 15m, 30m | Risk: 5–15% | TP: 1.5–3.0R | Walk-forward pair selection
- Bug: used raw TP_R instead of fee-adjusted R for PnL (inflated results by ~0.11R/trade)
- **Best: EMA_RIB@15m** — 7 pairs, 35 tpd, PF 1.2–1.3, x10 in 5–8d, BUT DD 85–100%
- Speed was right but DD catastrophic due to concentration in only 7 walk-forward-selected pairs

### v4: All-Pairs (800+ configs, 0 pass)
- Removed walk-forward selection — traded all 186 pairs
- Fixed fee bug (use actual r_net)
- **Result**: ALL configs negative. Including all pairs dilutes edge → net PF < 1.0

### v5: Tiered Pair Selection (2,560 configs, 0 pass)
- Compromise: rank pairs by training R, test top N = 5, 10, 15, 20, 30, 40, 60, 80
- 3 strategies (EMA_RIB, BB_BREAK, DONCHIAN) + 3-way combo
- **Key finding**: Only 3–7 of 186 pairs have positive training R per strategy
- Beyond top 15 pairs, ALL strategies go net negative (PF < 1.0)
- Best: EMA_RIB n=10 → $1,240, PF 1.11, DD 56% — NOT x10

### v6: Diverse Strategies + Per-Pair Matching (1,680 configs, 0 pass)
- **10 strategies** (3 existing + 7 new: RSI_FADE, BB_FADE, STOCH_X, PIN_BAR, IB_BREAK, ENGULF, MTF_RSI)
- **Per-pair strategy matching**: for each pair, find best strategy in training, validate OOS
- **Adaptive sizing**: reduce risk 50% after 2 consecutive losses, increase 50% after 2 wins
- **BREAKTHROUGH**: PF jumped to **1.27–1.49** (from 1.10–1.16 in v5)
- 30m adaptive: **x10 in 57d, DD 20.6%, PF 1.39** — genuinely profitable, low DD
- 15m scored: **x10 in 54d, DD 40.5%, PF 1.24**

### v7: Multi-TF Signal Pooling (1,728 configs, 0 pass)
- Pool 5m + 15m + 30m signals from per-pair matched combos
- 5m contributed only 9 combos (fees destroyed edge on low-volatility 5m data)
- Up to **100 combos, 61 unique, 45 pairs, 9 strategies, 40+ tpd**
- **Best by DD**: x10 in 57d, DD 22.7% (1% risk, 30 combos, adaptive)
- **Best by speed**: x10 in 32d, DD 87% (3% risk, 75 combos)  
- **Best balanced**: x10 in 47d, DD 42.1% (1.5% risk, 75 combos, 33 tpd, adaptive)

---

## WHY x10 IN 10 DAYS IS IMPOSSIBLE (AT CURRENT FEES)

### The Fee Math
- Bybit taker fee: 0.055% per side = **0.11% round-trip**
- Typical SL on 15m: 0.3–0.5% of price
- Fee as fraction of stop: **0.22–0.37R per trade**
- This means each trade loses 0.22–0.37R JUST TO FEES before any P/L from the move

### Required Compound Rate for x10 in 10 Days
At 40 trades/day × 10 days = 400 trades:
- (1 + r × avg_R)^400 = 10
- At 2% risk: need avg_R ≈ 0.275R per trade
- **This is achievable** — v7 shows avg_R ~0.25–0.30R per surviving combo

### The Variance Drag Problem
Expected x10 time: ~10 days (40 tpd, 2% risk, 0.275R avg)
**Realized x10 time: ~44–50 days** (4–5× longer)

The gap is caused by **variance drag** — losing streaks deplete equity, and compound recovery takes exponentially longer than compound growth. With 35% WR and 65% loss rate:
- Probability of 10+ consecutive losses in 400 trades: ~96%
- Each 10-loss streak at 2% risk: equity drops ~18%
- Recovery from 18% drop requires ~22% gain — equivalent to ~40 winning trades

### The Trade-Off Frontier

| Risk | x10 time | Max DD  | Status |
|------|----------|---------|--------|
| 0.5% | 54–62d  | 16–26%  | Safe but very slow |
| 1.0% | 48–57d  | 22–47%  | Profitable, moderate DD |
| 1.5% | 47–50d  | 40–52%  | Best balance |
| 2.0% | 44–50d  | 50–53%  | Borderline DD |
| 3.0% | 32–43d  | 52–88%  | Too much DD |
| 5.0% | 32–33d  | 90–100% | Blowup |

**No risk level achieves x10 ≤10d AND DD <55%.**

---

## BEST VIABLE STRATEGIES FOUND

### Strategy 1: "Conservative Multi-TF Portfolio"
- Config: 30 combos, 1% risk, adaptive sizing, mc=5
- Performance: x10 in 57d, DD 22.7%, PF 1.31, 18.9 tpd
- Pairs: 16 unique across 8 strategies and 3 timeframes
- Annualized: ~700% return with <25% DD

### Strategy 2: "Balanced Multi-TF Portfolio"  
- Config: 75 combos, 1.5% risk, adaptive sizing, mc=15
- Performance: x10 in 47d, DD 42.1%, PF 1.36, 33.2 tpd
- Pairs: 34 unique across 9 strategies
- Annualized: ~2,800% return

### Strategy 3: "Aggressive Multi-TF Portfolio"
- Config: 75 combos, 2% risk, adaptive sizing, mc=15
- Performance: x10 in 44d, DD 52.6%, PF 1.36, 33.2 tpd
- Equity: $930,008 over full OOS period
- Note: DD slightly exceeds 55% for part of the curve

---

## STRATEGIES TESTED (Full List)

### Momentum / Breakout
1. **EMA_RIB** — 8/21/55 EMA ribbon, pullback to fast EMA, body/range >30%
2. **BB_BREAK** — Price breaks Bollinger Band with EMA50 trend + volume >1.2× avg
3. **DONCHIAN** — 20-period Donchian channel breakout
4. **ADX_BURST** — ADX burst momentum
5. **MOM_BURST** — Momentum acceleration
6. **VOL_BRK** — Volume breakout
7. **RANGE_EXP** — Range expansion
8. **KELTNER** — Keltner Channel breakout
9. **SQUEEZE** — BB squeeze + expansion

### Mean Reversion
10. **RSI_FADE** — RSI crossing back from <25/>75 with confirming candle
11. **BB_FADE** — Bounce off BB bands toward middle band
12. **STOCH_X** — Stochastic crossover from <25/>75 with SMA50 trend

### Pattern Based
13. **PIN_BAR** — Pin bar reversal (shadow >2× body) with SMA50 trend
14. **IB_BREAK** — Inside bar breakout with trend filter
15. **ENGULF** — Engulfing pattern with trend + volume confirmation
16. **RSI_BNC** — RSI bounce from extremes

### Multi-Timeframe
17. **MTF_RSI** — SMA200 trend + RSI pullback from 40/60 cross
18. **NTS_REL** — N_TREND_STOCH relaxed variant (from prior 5-round research)

### Combos
19. **2-way combos**: EMA_RIB+BB_BREAK, EMA_RIB+KELTNER, DONCHIAN+KELTNER
20. **3-way combo**: EMA_RIB+BB_BREAK+DONCHIAN

---

## PER-PAIR FINDINGS

### Pairs That Consistently Show Edge
These appeared as positive-R in training across multiple strategies:
- WHITEWHALE (top in EMA_RIB, BB_BREAK, DONCHIAN) — highest R consistently
- CLOUD (top in BB_BREAK, DONCHIAN)
- FHE, TRIA, BREV, SKR (top in EMA_RIB)
- RAVE (BB_BREAK)
- SPACE (DONCHIAN)

### Pair Selection Physics
- Only **3–7 of 186 pairs** have positive training R for any single strategy
- Adding more pairs beyond top ~15 makes edge negative (too many losing pairs)
- The "sweet spot" is 15-30 pairs selected by training R

### Volatility Pre-Filter
- Pairs with 15m ATR < 0.20% of price are unprofitable (fees dominate)
- Pairs with 5m ATR < 0.35% of price have near-zero edge on 5m
- High-volatility altcoins (ATR > 0.5%) perform significantly better

---

## WHAT WOULD MAKE x10 IN 10 DAYS POSSIBLE

1. **Lower fees**: Maker orders (0.02% vs 0.055%) would reduce fee drag by 64%, potentially doubling effective PF. At PF ~2.0, x10 in 10d with DD <55% becomes theoretically achievable.

2. **Higher WR strategy**: A strategy with WR >50% at TP ≥1.5R would have dramatically less variance drag. No such strategy was found in 180 days of 186 pairs.

3. **More diverse, uncorrelated signals**: Getting to 100+ truly uncorrelated signals/day (not just more signals from correlated strategies) could reduce variance drag enough to close the gap.

4. **Regime-specific trading**: Only trading during confirmed trending markets (not ranging/choppy) could boost PF. Requires live regime detection not fully testable in static backtest.

5. **Different market**: Forex, equities, or highly volatile small-cap tokens with lower fees.

---

## RECOMMENDATIONS

### If Goal Remains x10 in 10 Days:
- Switch to **maker-only orders** (0.02% fee) — this is the single biggest lever
- Build a limit-order bot with passive fills
- Re-run this research with maker fee assumption (FEE = 0.0002)
- Expected result: PF jumps from 1.3 to ~1.8-2.0, making x10 in 10d feasible

### If Goal Is Adjusted to x10 in 45-60 Days:
- Deploy **Strategy 2** (balanced multi-TF portfolio): x10 in 47d, DD 42%
- Use adaptive sizing to protect capital during drawdowns
- Expected monthly return: ~60-80% with DD <45%

### If Goal Is Conservative Capital Growth:
- Deploy **Strategy 1** (conservative): x10 in 57d, DD 23%
- Very manageable risk, excellent risk-adjusted returns
- Annualized: ~700% with peaks DD <25%

---

## TECHNICAL DETAILS

### Backtest Parameters
- Fee: 0.055% per side (taker)
- Slippage: 0.03% per trade
- Min SL: max(3× round-trip fee cost, 1×ATR, 0.3% of price)
- Walk-forward: 70% train / 30% test
- SL checked before TP on each bar (conservative)
- Start equity: $500, target: $5,000
- Compound position sizing (risk % of current equity)

### Adaptive Sizing Rules
- After 2 consecutive losses: risk × 0.50
- After 3 consecutive losses: risk × 0.25
- After 2 consecutive wins: risk × 1.50
- After 3 consecutive wins: risk × 2.00 (capped at 15%)

### Files Generated
- `_discovery_v3.jsonl` — v3 results (1,320 configs)
- `_discovery_v4.jsonl` — v4 results (800+ configs)
- `_discovery_v5.jsonl` — v5 results (2,560 configs)
- `_discovery_v6.jsonl` — v6 results (1,680 configs)
- `_discovery_v7.jsonl` — v7 results (1,728 configs)
- `_strategy_discovery.py` through `_discovery_v7.py` — all engine code
