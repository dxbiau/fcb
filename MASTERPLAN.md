# MASTERPLAN: Reinstating the Winning Edge

## Date: 2026-02-26
## Objective: Reverse-engineer the Feb 25 winning session and rebuild that edge into a superior system

---

## PART 1: THE WINNING SESSION — WHAT ACTUALLY HAPPENED

### Feb 25, 2026 — The Day We Nearly Hit +17%

| Phase | Time (UTC) | Equity | Move |
|-------|-----------|--------|------|
| Asia low | 06:25 | $484.28 | -5.8% drawdown (bad Asia) |
| London recovery | 08:00–16:00 | $484 → $532 | **+$48 (+9.9%)** |
| NY surge | 16:00–21:36 | $530 → $567.28 | **+$37.26 (+7.0%)** — ALL-TIME HIGH |
| NY late decay | 21:36–00:00 | $567 → $551 | -$16 giveback (last 3 trades lost) |
| **Net low→peak** | | **$484 → $567** | **+$83 (+17.1%)** |

### The Winning Trades (NY Session — 12 trades, 7W/5L, 58% WR, +2.86R)

| # | Time | Symbol | Strategy | TF | Exit | PnL R | PnL $ |
|---|------|--------|----------|-----|------|-------|-------|
| 1 | 17:00 | ETH | DONCHIAN | 15m | trail | +0.58R | +$3.48 |
| 2 | 17:03 | SOL | EMA_RIB | 15m | trail | +0.61R | +$4.35 |
| 3 | 17:13 | ZEC | DONCHIAN | 15m | trail | +0.76R | +$3.43 |
| 4 | 17:33 | UNI | BB_BREAK | 1h | trail | +0.87R | +$5.88 |
| 5 | 20:10 | HYPE | DONCHIAN | 1h | sl | -0.60R | -$4.98 |
| 6 | 20:35 | SUI | DONCHIAN | 15m | trail | +1.05R | +$5.13 |
| 7 | 20:58 | SOL | BB_BREAK | 1h | trail | +0.86R | +$6.11 |
| 8 | 21:12 | HYPE | EMA_RIB | 15m | sl | -0.74R | -$4.15 |
| **9** | **21:36** | **ETH** | **BB_BREAK** | **1h** | **trail** | **+2.56R** | **+$18.01** |
| 10 | 22:30 | BCH | TR_PULL | 15m | sl | -1.06R | -$4.45 |
| 11 | 22:52 | SOL | EMA_RIB | 15m | sl | -0.88R | -$7.15 |
| 12 | 23:30 | BNB | EMA_RIB | 15m | sl | -1.15R | -$2.32 |

### The Star: ETH BB_BREAK 1h Long
- Entry: $2,059.86 → Trailed out at +2.56R (+$18.01)
- Hold time: ~4 hours
- Conviction: 96 (A+)
- Sentiment: BULL 1.0 — BTC↑ ETH↑ SOL↑ all hh_hl

---

## PART 2: THE FIVE PILLARS OF THE WINNING EDGE

### Pillar 1: PERFECT MACRO ALIGNMENT
- **BTC, ETH, SOL** all in `hh_hl` structure (higher highs, higher lows)
- **EMA spreads**: BTC +1.39%, ETH +2.39%, SOL +2.53% — all positive
- **Sentiment score**: 0.99–1.00 (BULL) with confidence 1.0
- **All arrows pointing UP**: Not ambiguous, not mixed — unanimous bull

### Pillar 2: LONG-ONLY CONVICTION
- **Zero shorts taken** — every signal was a long
- Not fighting the trend, not hedging, not trying to catch a reversal
- When the macro says UP, you only buy dips

### Pillar 3: 1h TIMEFRAME FOR RUNNERS
- The 3 biggest $ winners: ETH BB_BREAK/1h (+$18.01), SOL BB_BREAK/1h (+$6.11), UNI BB_BREAK/1h (+$5.88)
- **$30.00 from just 3 BB_BREAK/1h trades** — that's 80% of NY profits
- 15m gave quick scalps (+$3-5 each), but the real money was 1h holding through the trend

### Pillar 4: TRAIL EXITS — NEVER CUT WINNERS
- **100% of winners exited via trail stop** — zero TP exits in the entire NY session
- The trail let ETH run from +0.5R all the way to +2.56R before catching
- With a fixed TP of 2.75R, it would have stopped 19 cents short. Trail captured the move.

### Pillar 5: FRONT-LOADED CONVICTION + LATE-SESSION FADE
- First 4 NY trades (17:00-17:33): **ALL WINNERS** → +$17.14 cushion
- Last 3 NY trades (22:30-23:30): **ALL LOSERS** → -$13.92 giveback
- The session had a clear lifecycle: momentum → exhaustion
- **The winners came early. The losers came late.**

---

## PART 3: CURRENT STATE — WHY WE'RE NOT REPLICATING THIS

### Gate 1: EMA_RIB/15m Is DEAD
- On Feb 25, EMA_RIB/15m produced 6 trades in London, 2 in NY
- **Currently**: EMA_RIB/15m has been DEMOTED to shadow-only (ECS = 0.104, ExpR = -0.030)
- This was a key scalp combo that fed the P&L on alignment days
- **Impact**: Losing our 15m scalp ammunition when macro aligns

### Gate 2: Risk Stacking Crushes Position Size
Current multiplicative chain on a typical trade:
```
base_risk    = 2.0%
× DD mult    = 0.50   (14.9% drawdown)
× regime     = 0.70   (COOL)
× calibrator = 0.85
× streak     = varies (0.25 for many pairs)
= 0.02 × 0.50 × 0.70 × 0.85 × 0.25 = 0.0015 → **0.15% risk**
```
At $482 equity = **$0.72 per trade**. That's DUST. Even +2.56R = $1.84.

On the winning day, risk was ~1.3% effective ($7/trade). That's **9x larger** than current.

### Gate 3: Regime is COOL (0.70x) Instead of WARM/HOT
- The winning day likely ran at NORMAL or WARM (1.0-1.1x)
- Currently stuck at COOL (0.70x) from accumulated losses
- This is a SELF-REINFORCING PROBLEM: small positions → small wins → can't climb out of COOL

### Gate 4: Conviction Inversion
- Adaptive engine has inverted the conviction multipliers:
  - A+ gets 1.09x (config says 1.50x)
  - A gets 0.93x (config says 1.15x)  
  - C gets 1.69x (config says 0.75x)
- On the winning day, all graded trades were A+ (conv 81-105)
- **The system is literally penalizing the grade that won the best session**

### Gate 5: No Macro Alignment Detection
- The bot has sentiment (BULL/BEAR/NEUTRAL) but no concept of "PERFECT ALIGNMENT"
- BTC+ETH+SOL all in hh_hl with positive spreads = **rare event**, maybe 15-20% of the time
- When it happens, it deserves AGGRESSIVE risk, not the same throttled approach
- Currently the sentiment score just feeds into EdgeRadar's sentiment multiplier (0.70-1.25x)

### Gate 6: No Late-Session Fade
- The last 3 trades (22:30-23:30) lost $13.92 — giving back 37% of the NY gains
- There is NO mechanism to reduce risk or stop trading in the final 2 hours of a session
- Every session has a natural lifecycle: momentum → exhaustion → chop
- Trading through exhaustion destroys edge

### Gate 7: DONCHIAN/1h Blocked
- DONCHIAN/1h is in shadow-only (ExpR = -0.111)
- On the winning day, DONCHIAN produced 3 wins in NY (ZEC, SUI, and a London trade)
- However: looking at broader data, DONCHIAN/1h IS weak overall — the Feb 25 wins were alignment-dependent
- **Conditional re-activation** during alignment mode could capture this edge

---

## PART 4: THE MASTERPLAN

### Module 1: MOMENTUM ALIGNMENT DETECTOR (`v13pro/momentum.py`)

**Purpose**: Detect when BTC, ETH, SOL are all in unanimous trend alignment — the condition that produced the winning session.

**Signals**:
- `alignment_score`: 0.0 (conflicted) → 1.0 (perfect alignment)
- Inputs: structure (hh_hl/ll_lh), EMA spread direction, sentiment per coin, price vs VWAP
- Thresholds:
  - **ALIGNED** (score ≥ 0.85): All 3 leaders agree in direction + structure
  - **PARTIAL** (score 0.50–0.84): 2 of 3 agree, or structure mixed
  - **CONFLICTED** (score < 0.50): No clear macro direction

**Risk modifiers when ALIGNED**:
- `alignment_risk_mult`: **1.50x** — boost risk by 50% (recovering the crushing from DD/regime)
- Override regime floor to 0.85 (prevent COOL from killing aligned trades)
- Override conviction to use CONFIG values (A+=1.50, A=1.15) not adaptive-crushed values
- Enable EMA_RIB/15m and DONCHIAN/1h as LIVE combos (alignment-conditional promotion)

**Risk modifiers when CONFLICTED**:
- `alignment_risk_mult`: **0.60x** — reduce further (the current throttle is correct for chop)
- Keep adaptive conviction values (they're calibrated for non-aligned conditions)

**Implementation**: ~200 lines. Uses existing sentiment data (already have per-coin bias, score, structure in shadow). New function: `compute_alignment()` called every 60s in heartbeat. Exposes `alignment_score`, `alignment_state`, `alignment_risk_mult`.

---

### Module 2: SESSION LIFECYCLE MANAGER (`v13pro/session_lifecycle.py`)

**Purpose**: Track per-session performance in real-time and fade risk as the session ages.

**State Machine**:
```
EARLY (first 3h)  →  PEAK (middle 3h)  →  LATE (last 2h)
  1.0x risk             1.0x risk            0.50x risk (FADE)
```

**Per-session tracking**:
- Wins/losses in current session
- Running PnL in current session (R)
- Peak PnL in current session (R)
- Giveback from peak (R)

**Front-loading detection**:
- If first 3 trades are winners → `momentum_early = True` → add 1.15x momentum boost
- If session PnL exceeds +3.0R → `session_hot = True` → enable 1.25x hot session boost
- If giveback from peak exceeds 2.0R → `session_fatigued = True` → hard 0.40x or STOP SIGNAL

**Late-session rules**:
- Last 2 hours of any session: risk × 0.50
- If session is already negative, last 2 hours: risk × 0.30
- This would have saved $10+ on Feb 25 (3 late losers were $13.92 giveback)

**Risk impact**: Adds `session_lifecycle_mult` to the effective_risk chain.

**Implementation**: ~150 lines. Hooks into `_heartbeat_loop()` for state transitions. Resets on session boundary.

---

### Module 3: ALIGNMENT-CONDITIONAL COMBO PROMOTION

**Purpose**: Some combos (EMA_RIB/15m, DONCHIAN/1h) are weak in chop but strong in alignment. Promote them conditionally.

**Rules**:
- When `alignment_state == ALIGNED`:
  - Promote EMA_RIB/15m to LIVE (was the #1 frequency combo on Feb 25 London)
  - Promote DONCHIAN/1h to LIVE (3 NY winners on alignment day)
  - These combos ONLY fire during alignment — shadow-only otherwise
- When `alignment_state != ALIGNED`:
  - Demote back to shadow-only
  - No trades placed

**Implementation**: Add `ALIGNMENT_COMBOS` set to config. In `_execute_signal()`, check alignment state before the LIVE_COMBOS gate for these specific combos.

---

### Module 4: CONVICTION RESET DURING ALIGNMENT

**Purpose**: The adaptive engine has crushed A+/A conviction multipliers based on overall data. But during alignment, A+ conviction IS the edge.

**Rules**:
- When `alignment_state == ALIGNED`:
  - Use CONFIG conviction multipliers: A+=1.50, A=1.15, B=1.00, C=0.75, D=0.50
  - Ignore adaptive overrides (they're calibrated on non-aligned data)
- When `alignment_state != ALIGNED`:
  - Keep adaptive conviction multipliers (they're correct for mixed conditions)

**Why this is safe**: Alignment mode is ~15-20% of the time. The adaptive engine IS correct for the other 80-85%. We're not throwing it away — we're context-switching conviction scoring based on macro state.

**Implementation**: ~30 lines in `_execute_signal()`. Check alignment state, pick conviction source accordingly.

---

### Module 5: DRAWDOWN RECOVERY ACCELERATOR

**Purpose**: At 14.9% DD with 0.50x throttle, the bot needs 2x the edge just to maintain its current positions. The self-reinforcing trap: small positions → small wins → can't climb out → regime stays COOL → repeat.

**Current problem**: At $0.72/trade risk, even a perfect session like Feb 25 would produce ~$8 profit instead of $83. The DD throttle is correct in principle but too aggressive at this equity level.

**Proposed fix — Tiered DD Recovery**:
```python
DRAWDOWN_THROTTLE = [
    (5,   1.00),   # 0-5% DD: full risk
    (10,  0.85),   # 5-10%: slight reduction (was 0.75)
    (15,  0.65),   # 10-15%: moderate (was 0.50) ← WE ARE HERE
    (20,  0.40),   # 15-20%: significant (was 0.25)
    (30,  0.15),   # 20-30%: survival mode (was 0.10)
]
```

**Why this is safe**: 
- At $482 equity from $567 peak = 14.9% DD
- Current: 0.50x → effective risk ~$4.82/trade (before other mults)
- Proposed: 0.65x → effective risk ~$6.27/trade
- Still well within 2% risk per trade, just less aggressive throttling
- Combined with alignment detection, this means aligned+recovery = meaningful positions

**Additional**: During ALIGNED state, override DD throttle floor to 0.55x:
- Even at 20% DD + ALIGNED → risk stays at 55%, not 40%
- Alignment is the BEST time to recover — don't throttle recovery during the best conditions

---

### Module 6: BB_BREAK/1h PRIORITY QUEUE

**Purpose**: BB_BREAK/1h was responsible for 80% of NY profits ($30 from 3 trades). It deserves priority treatment.

**Rules**:
- When BB_BREAK/1h fires during ALIGNED state:
  - Reserve 1 position slot exclusively for BB_BREAK/1h (can't be displaced by 15m scalps)
  - Apply `priority_mult`: 1.30x risk (on top of alignment 1.50x)
  - Extended trail: activation at 2.0R, distance 0.60R (give 1h room to run)
- When BB_BREAK/1h fires outside alignment:
  - Standard treatment (ECS 0.790 already supports it)
  - Standard trail settings

**Why**: The ETH BB_BREAK held for 4 hours and captured 2.56R. But with activation at 1.5R and distance at 0.5R, a brief pullback at 1.6R would have stopped it out at 1.1R. Wider settings for 1h combos = more room to capture the big moves.

**Implementation**: ~40 lines. In `_execute_signal()`, detect BB_BREAK/1h + aligned, apply overrides. In `registry.py`, add BB_BREAK_1H_ALIGNED trail params.

---

## PART 5: EXECUTION PRIORITY

### Phase A — Quick Wins (can be done NOW, high-impact, low-risk)

| # | Change | Impact | Risk | LOE |
|---|--------|--------|------|-----|
| A1 | Soften DRAWDOWN_THROTTLE curve | Immediate: positions go from dust to meaningful | Low — still capped at 2% | Config change |
| A2 | Late-session fade (last 2h → 0.50x) | Prevents late-session giveback pattern | Zero — only reduces risk | ~50 lines |
| A3 | BB_BREAK/1h wider trail during alignment | Captures more of the big 1h runners | Low — only affects 1h exits | Config + 20 lines |

### Phase B — Core Intelligence (needs careful implementation)

| # | Change | Impact | Risk | LOE |
|---|--------|--------|------|-----|
| B1 | Momentum Alignment Detector | HIGH — unlocks alignment-conditional behavior | Medium — new signal source | ~200 lines |
| B2 | Alignment-conditional combo promotion | Re-activates EMA_RIB/15m + DONCHIAN/1h when they have edge | Low — only fires during alignment | ~30 lines |
| B3 | Conviction reset during alignment | Uses proven A+ values instead of adaptive-crushed | Medium — overrides adaptive | ~30 lines |

### Phase C — Session Intelligence (after B is validated)

| # | Change | Impact | Risk | LOE |
|---|--------|--------|------|-----|
| C1 | Session Lifecycle Manager | Systematizes the front-load + fade pattern | Low — only modifies risk sizing | ~150 lines |
| C2 | Session momentum detection | Boost when session is hot, cut when fatigued | Low — multiplicative with existing | Included in C1 |

---

## PART 6: EXPECTED IMPACT

### Current State (per session, using Feb 25-like conditions):
- Risk per trade: ~$0.72 (dust)
- A +2.56R ETH runner: +$1.84
- 7 winners at avg +0.90R: +$4.54
- 5 losers at avg -0.89R: -$3.20
- **Net: +$1.34 per session** ← barely moves the needle

### After Masterplan (per session, same conditions):
- Risk per trade: ~$6.50 (alignment 1.50x × softened DD 0.65x × regime override 0.85x)
- A +2.56R ETH runner (wider trail → maybe 2.8R): +$18.20
- 7 winners at avg +0.90R: +$40.95
- 5 losers at avg -0.89R: -$28.93
- Late-session fade prevents last 3 losers: save ~$12
- **Net: +$30+ per session** ← MEANINGFUL compounding

### Adding the session lifecycle protection:
- Cut the 3 late losers ($13.92 saved → more like $12 with reduced late-session sizing)
- **Net with protection: +$42 per session** ← that's +8.7% equity growth per session

---

## PART 7: WHAT WE DO NOT CHANGE

1. **Base 2% risk** — proven correct, never oversize
2. **8x leverage** — death spiral lesson learned
3. **Trail activation 1.5R / distance 0.5R** — working as intended for 15m/30m
4. **EdgeRadar** — combo heat, market heat, sentiment edge all stay
5. **DirectionalIntelligence** — BEAR→longs, BULL→shorts validated
6. **Shadow tracking** — every signal still shadow-tracked
7. **Auto-promote/demote** — just adding ALIGNMENT as a conditional dimension
8. **Profit tiers** — the guardian SL lockup tiers are correct

---

## PART 8: RISK CONTROLS

| Risk | Mitigation |
|------|-----------|
| Alignment detection false positive | Require ALL 3 leaders (BTC, ETH, SOL) to agree + minimum 30min sustained |
| Over-sizing during alignment | Still capped at 2% base × all other mults. Alignment adds 1.50x but DD/regime/streak still constrain |
| Late-session fade kills a runner | Only affects NEW entries in last 2h. Open positions keep their trail intact |
| Combo promotion during fake alignment | Alignment combos still need conviction ≥ threshold + all other gates pass |
| Regime stuck at COOL forever | Softened DD throttle + alignment override breaks the self-reinforcing trap |

---

## SUMMARY

The winning Feb 25 session had **5 things we don't currently systematize**:

1. ✅ **Macro alignment detection** → Module 1
2. ✅ **Late-session risk reduction** → Module 2
3. ✅ **Conditional combo activation** → Module 3  
4. ✅ **Context-aware conviction scoring** → Module 4
5. ✅ **DD recovery acceleration** → Module 5
6. ✅ **1h priority for BB_BREAK runners** → Module 6

Total new code: ~400-500 lines across 2 new files + config changes.

**The core insight**: The winning session wasn't luck. It was a SPECIFIC MARKET CONDITION (perfect macro alignment) that our current system can detect but doesn't ACT on differently. We process aligned and non-aligned markets with the same gates, same risk, same combos. That's leaving 80% of the edge on the table.
