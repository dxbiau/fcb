"""
live/config.py — Live trading configuration for FCB bot on Bybit.

DYNAMIC HYBRID X10 MODE (activated 2026-02-25):
  Mathematical basis: Kelly Criterion + Asymmetric Payoff + Trail + Dynamic Engine.
  At WR>=45%, payoff ratio b=1.28/0.77=1.66, Kelly optimal f*=11.9%.
  Using 8% A / 4% B (sub-Kelly) at 10x leverage for the x10 journey.
  DynamicEngine adapts risk in real-time: bankroll phase, heat management,
  session momentum, market regime, and market-wide breakout quality.
  Guardian v3 trail: +1,738R across 12,355 trades, PF 1.28.
  Progressive SL tiers cap avg loss at ~0.77R.
  At 8% risk: growth/trade = 1.016 → x10 in ~145 trades (~18 days at 8 tpd).
  Slower but SAFER. Dynamic engine boosts when conditions are optimal.

STRATEGY PARAMETERS:
  - Exit = Guardian v3 trail (0.5R behind peak from +1.0R) | 10R safety TP
  - SL = range midpoint | Progressive tiers accelerate BE
  - Fee assumption = 0.02% maker
  - Min range = 0.5%
  - Max 2 trades per pair per session, 6 per pair per day
  - Risk = 8% A-class / 4% B-class | 10x leverage
  - Max 2 concurrent positions (margin-safe at 10x)
  BYBIT_API_SECRET

For Demo Trading, set DEMO_MODE = True and MAINNET = False.
"""

import os
from typing import Dict, List

# ─── Trading Mode ───
# MAINNET=True  → real money on mainnet
# MAINNET=False, DEMO_MODE=True  → Bybit Demo Trading (api-demo.bybit.com)
# MAINNET=False, DEMO_MODE=False → Bybit Testnet (api-testnet.bybit.com)
MAINNET   = True
DEMO_MODE = False  # LIVE — switched to mainnet 2026-02-14

# ─── Bybit API ───
# In demo mode, use demo keys directly.  For mainnet, use env vars.
if DEMO_MODE and not MAINNET:
    API_KEY    = "HDkU9y98rWH2ShxZXz"
    API_SECRET = "crEi1i53oSnY4aLjVpgvE3J80oi1rVRFbnOc"
else:
    API_KEY    = os.environ.get("BYBIT_API_KEY", "")
    API_SECRET = os.environ.get("BYBIT_API_SECRET", "")

# ─── Frozen FCB Strategy ───
# AGGRESSIVE X10: TP=1.5R with Guardian trail for runners past +1R.
# Axiomatic: at 30-50% WR, you MUST have payoff ratio b > (1-WR)/WR.
# 0.5R TP gives b=0.65 (BELOW breakeven at any WR < 61%). Death by math.
# 1.5R TP + trail gives avg_win ~1.28R, avg_loss ~0.77R → b=1.66 → positive at 40%+ WR.
TP_R            = 1.5       # take-profit in R-multiples (REVERTED from 0.5 — asymmetry > frequency)
MIN_RANGE_PCT   = 0.003     # 0.3% min FC range (research: +62% trades, Sharpe 0.072→0.173)
FEE_RATE        = 0.0002    # 0.02% maker fee
LEVERAGE        = 10        # 10x leverage — safer margin for x10 journey (halved from 20x)
                            # At 10x: margin = position_value / 10, so each trade uses more margin.
                            # Dynamic engine modulates risk within session based on real-time data.
MAX_TRADES_SESSION = 2      # per pair per session (was 1 — doubles opportunities)
MAX_TRADES_DAY     = 6      # per pair per day (was 3 — 2x per session × 3 sessions)
MAX_CONCURRENT_POSITIONS = 2 # reduced from 3 — margin-safe at 10x leverage with 8% risk
                             # 2 positions × 8% risk × 10x = manageable margin usage
MAX_CONCURRENT_B         = 1 # B-class slot cap — reserves 1 slot for A-class entries
BREAKOUT_WINDOW_5M = 60     # minutes to keep scanning for 5m breakouts after FC
                            # (backtest checks every candle — live was only checking C2)
API_DELAY_SECS  = 0.15      # delay between API calls in scan loops (prevents Bybit rate-limit)
SPLIT_ENTRY     = False      # DISABLED — scale-in amplifies losses 50-80%, never fills on wins (0/5)
SCALE_OUT       = False      # DISABLED — cuts wins 60%, BE SL move triggers instantly on retrace, turned +$29 session into -$38
SCALE_OUT_PCT   = 0.50       # Fraction of position to close at FC boundary

# ─── Profit Guardian v3 (Trail Intelligence) ───
# Data-driven across 12,355 FCB trades:
#   Fixed 1.5R TP:            +334R total, 41.2% WR, PF 1.05
#   Guardian v3 (0.3R trail): +1,738R total, 50.3% WR, PF 1.28
#
# HOW IT WORKS:
#   1. Enter every breakout at market (candle 2 close)
#   2. Exchange TP set at 10R (safety net — trail handles real exit)
#   3. Once R >= 1.0, trail SL at (peak - 0.3R)
#   4. Progressive SL tiers are the crash safety net
#
# DEPRECATED SYSTEMS (all proven harmful across 12,355 trades):
#   - Retrace detection: closed winners too early (-37R/200 trades)
#   - Momentum death: added noise, no edge
#   - Runner capture: replaced by trail (no cap needed)
#   - BE at +0.75R: shook out 20% of eventual winners
#   - SMART_TP: trail never activated, lost -1.945R of edge

# Legacy compat — some analysis scripts may still import these
SMART_TP              = False
SMART_TP_INITIAL_R    = 10.0
TRAIL_PCT             = 0.015
TRAIL_POLL_SECS       = 15

GUARDIAN_POLL_SECS   = 2            # Poll every 2 seconds

# Trail parameters (validated: act=1.0 trail=0.3 → +1,738R across 12,355 trades)
# AGGRESSIVE X10: Trail ENABLED. Runners are the ONLY path to x10.
# Guardian v3 backtest: +1,738R across 12,355 trades (vs +334R with fixed TP).
# Trail catches 2-5R runners while progressive SL tiers protect the downside.
TRAIL_ENABLED        = True          # RE-ENABLED — runners are everything
TRAIL_ACTIVATION_R   = 0.95         # Start trailing once R >= 0.95 (research: best PF 1.554, Kelly 0.142)
TRAIL_DISTANCE_R     = 0.20         # Trail 0.20R behind peak (widened from 0.15R — honest backtest shows
                                    # 0.15R gets whipsawed by 2s polling intra-bar noise. Backtest at bar-close
                                    # is optimistic about tight trails. 0.20R is a safer live equivalent.)
EXCHANGE_TP_R        = 10.0          # Safety net only — trail handles real exit

# Progressive SL tiers: (trigger_R, new_SL_R, label)
# Exchange crash safety net — if bot dies, these SLs protect you.
# AGGRESSIVE tiers: faster breakeven, then trail takes over at +1.0R.
# Data-driven: tier protection cuts avg loss from 1.0R → 0.77R (live proven).
PROFIT_TIERS = [
    (0.25,  -0.50, "T0.5: FAST ENGAGE"),    # +0.25R → SL -0.5R  (cap loss early)
    (0.50,   0.00, "T1: BREAKEVEN"),         # +0.5R  → SL at entry (accelerated from +0.75R)
    (0.75,   0.25, "T2: LOCK +0.25R"),       # +0.75R → SL +0.25R (small profit locked)
    (1.00,   0.50, "T3: LOCK → TRAIL"),      # +1.0R  → SL +0.5R  (TRAIL ACTIVATES HERE)
]

# ─── Micro-Filters (data-driven from 13,276-trade sweep on 128 pairs) ───
# param_sweep.py grid-searched 486 filter combos across ALL cached Bybit pairs.
# Best edge-score config: c2_body>=0.50, fc_counter=YES, no max cap.
# This gives 1,742 trades, 42.1% WR, +0.051R E(R), 1.088 PF.
# Previously was overfitting to 45 live trades — now backed by 13K+ samples.
MICRO_FILTER_ENABLED  = True        # Master switch for micro-filters
MIN_C2_BODY_RATIO     = 0.50        # Breakout candle body must fill >=50% of range
FC_COUNTER_5M         = True        # 5m ONLY: FC must lean opposite the breakout

# ─── Volume Filter (breakout candle vol vs FC vol) ───
# 13,276-trade sweep: vol_long>=1.0 adds +0.011R, vol_short>=0.25 is optimal.
# Longs need strong buying pressure; shorts work with less volume.
VOL_FILTER_ENABLED    = True        # Master switch for volume filter
MIN_VOL_RATIO_LONG    = 1.0         # Longs: breakout vol must >= FC vol
MIN_VOL_RATIO_SHORT   = 0.25        # Shorts: breakout vol must >= 25% of FC vol (was 0.50 — too tight)

# ─── Funding Rate Bias Filter ───
# Positive funding = market over-leveraged long → shorts have edge, longs are traps.
# Negative funding = market over-leveraged short → longs have edge, shorts risky.
# Skip trades that go WITH the crowd when funding is extreme.
# Live data: all 4 5m winners were shorts, every long except UB lost.
FUNDING_FILTER_ENABLED = True
FUNDING_EXTREME_RATE   = 0.0005     # 0.05% — skip longs when funding >= this (crowd is long)
FUNDING_EXTREME_NEG    = -0.0005    # -0.05% — skip shorts when funding <= this (crowd is short)

# ─── Max Body Ratio Filter (FOMO spike trap) ───
# 13,276-trade sweep result: c2_body<=0.75 HURTS E(R) by -0.003 vs no cap.
# The 45-trade "0.75+ = losers" was pure overfit — across 13K trades, no edge.
# Best configs all use max=1.00 (disabled). Removing this filter entirely.
# Old live data (3 trades) was noise, not signal.
MAX_C2_BODY_RATIO     = 1.00        # DISABLED — sweep proved no edge (was 0.75, overfit to 45 trades)

# ─── Liquidity Filter (SL slippage protection) ───
# Live data: RAVE -1.79R, OPEN -1.53R, 1000TOSHI -1.36R, FHE -1.16R
# All exceeded -1R due to thin order books on micro-caps.
# Spread check catches thin books in real-time; turnover catches dead pairs.
SPREAD_FILTER_ENABLED = True        # Master switch for liquidity filter
MAX_SPREAD_PCT        = 0.15        # Max bid-ask spread as % of mid price
MIN_TURNOVER_USDT     = 2_000_000   # Min 24h USDT volume ($2M)

# ─── C3 Retest Gate (CRITICAL — doubles edge vs no-retest) ───
# Honest backtest proved: WITHOUT retest, bot takes 9,133 trades at 38.1% WR, +0.12R avg.
# WITH retest, bot takes 1,933 trades at 39.7% WR, +0.23R avg — nearly DOUBLE the edge.
# The 7,200 trades that fail retest average only +0.076R — they dilute the real edge.
# Retest requirement: after C2 breakout, C3 must wick back to FC boundary AND
# close on the breakout side (confirming support/resistance holds).
C3_RETEST_REQUIRED   = True         # ENABLED — the single most impactful filter

# ─── Hybrid Entry (Entry Quality Filter) ───
# 1-minute replay of 15 real trades proved:
#   RECROSS fires on 7/7 losers AND 7/8 winners (50% precision = useless)
#   ADVERSE_CANDLE: 56% precision — barely better than coin flip
#   Skipping trades with slip>0.5R missed 3 real winners (1.5R each)
# CONCLUSION: NEVER SKIP. Enter every breakout. x1000 needs every trade.
# Slip is logged for monitoring but NEVER used to skip entries.
HYBRID_ENTRY          = False       # DISABLED — skipping kills x1000 math
MAX_SLIP_R            = 0.5         # Threshold for slip logging only

# ─── C3 Fakeout Detection (Real-Time Exit Agent) ───
# 1-minute replay proved C3_REVERSAL is the ONLY signal with 100% precision:
#   Fires on 2/7 losers, 0/8 winners. Zero false positives.
# After entry, wait 5 minutes for C3 to close. If C3 body reverses
# direction AND trade is negative → exit at market immediately.
# Saves ~0.5-1R per detected fakeout with ZERO cost to winners.
C3_EXIT               = True        # Enable C3 fakeout detection
C3_REVERSAL_BODY_PCT  = 0.30        # C3 body must be >30% of its range to count
C3_MAX_R_TO_EXIT      = 0.3         # Only exit if current R is below this

# ─── Skip Monitor ───
# When a trade is skipped (hybrid filter), log what would have happened.
SKIP_LOG              = "live/skipped_trades.csv"

# ─── Risk Tiers (DYNAMIC HYBRID X10 MODE) ───
# Kelly Criterion: at WR=45%, b=1.66 → f*=11.9%. Using 8% (sub-Kelly).
# At WR=50% (Guardian v3 trail): f*=19.9%. 8% is very conservative at 50%.
# Geometric growth at 8% risk: +1.6%/trade → x10 in ~145 trades (~18 days).
# Dynamic engine boosts to 1.3x (max 10.4%) when momentum/regime are ideal.
# Ruin check: P(10 consecutive losses)=0.55^10=0.25% → drawdown 34% (safe).
# At 10x leverage, 8% risk per trade: margin per position is manageable.
RISK_PCT_A       = 0.08     # 8% risk — sub-Kelly, dynamic engine modulates up/down
RISK_PCT_B       = 0.04     # 4% risk — half of A for unproven pairs
SCALE_RISK_PCT_A = 0.04     # unused (SPLIT_ENTRY=False) — set for safety
SCALE_RISK_PCT_B = 0.02     # unused (SPLIT_ENTRY=False) — set for safety
RISK_PCT         = RISK_PCT_A   # default (backward compat)
SCALE_RISK_PCT   = SCALE_RISK_PCT_A

# ─── Promotion / Demotion Rules ───
PROMOTE_WINS     = 2        # consecutive wins to promote B → A (was 3; P(3)=7% vs P(2)=17%)
DEMOTE_LOSSES    = 3        # consecutive losses to demote A → B
REHABILITATE_WINS = 2       # wins (not necessarily consecutive) to promote back B → A

# ─── Sessions (UTC) ───
SESSIONS = {
    "asia":   (0, 8),       # 00:00 – 08:00 UTC
    "london": (8, 16),      # 08:00 – 16:00 UTC
    "ny":     (16, 24),     # 16:00 – 24:00 UTC
}

# ─── Qualifying Pairs by Session with Class ───
# Class A: 30+ backtest trades, positive expectancy (proven)
# Class B: <30 trades (PROV) or borderline expectancy (watchlist)
# From bybit_scan_passed_20260214_205148.csv + discovery_20260216_052422
#
# Format: (pair, class)   — class "A" or "B"
PAIRS = {
    "asia": [
        ("CYS/USDT:USDT",         "A"),   # 0.773R, 30t, 73% WR
        ("ACU/USDT:USDT",          "B"),   # 0.522R, 16t, 63% WR PROV
        ("CLO/USDT:USDT",          "A"),   # 0.308R, 40t, 55% WR
        ("TRUST/USDT:USDT",        "A"),   # 0.310R, 42t, 55% WR
        ("BREV/USDT:USDT",         "B"),   # 0.308R, 24t, 54% WR PROV
        ("RAVE/USDT:USDT",         "B"),   # 0.302R, 22t, 55% WR PROV
        ("FHE/USDT:USDT",          "B"),   # 0.284R, 15t, 53% WR PROV ★NEW
        ("ALU/USDT:USDT",          "A"),   # 0.276R, 66t, 55% WR
        ("OPEN/USDT:USDT",         "A"),   # 0.270R, 74t, 53% WR
        ("HEMI/USDT:USDT",         "A"),   # 0.238R, 57t, 53% WR ★NEW
        ("US/USDT:USDT",           "B"),   # 0.241R, 23t, 52% WR PROV
        ("SUPER/USDT:USDT",        "A"),   # 0.235R, 59t, 53% WR
        ("API3/USDT:USDT",         "A"),   # 0.218R, 63t, 52% WR
        ("1000TOSHI/USDT:USDT",    "A"),   # 0.212R, 66t, 52% WR
        ("UB/USDT:USDT",           "A"),   # 0.183R, 48t, 50% WR ★NEW
        ("HOLO/USDT:USDT",         "A"),   # 0.171R, 69t, 49% WR
        ("STBL/USDT:USDT",         "A"),   # 0.167R, 59t, 51% WR
        ("MYX/USDT:USDT",          "A"),   # 0.160R, 66t, 48% WR ★NEW
        ("F/USDT:USDT",            "A"),   # 0.163R, 68t, 49% WR
        ("SWARMS/USDT:USDT",       "A"),   # 0.157R, 61t, 49% WR
        ("TAI/USDT:USDT",          "A"),   # 0.222R, 46t, 52% WR ★DISC
        ("RONIN/USDT:USDT",        "A"),   # 0.154R, 38t, 50% WR ★DISC
        ("FLUID/USDT:USDT",        "A"),   # 0.152R, 52t, 48% WR
        ("10000QUBIC/USDT:USDT",   "A"),   # 0.151R, 59t, 49% WR
        ("CLOUD/USDT:USDT",        "A"),   # 0.127R, 38t, 50% WR ★DISC
        ("KERNEL/USDT:USDT",       "A"),   # 0.079R, 58t, 47% WR ★DISC
        # ── NEW LIQUID PAIRS (added 2025-02-19) ──────────────────
        # Top performers promoted to A (backtest verified E(R)>0, 40+ trades)
        ("POWER/USDT:USDT",        "A"),   # +0.427R, 41t, 54% WR ★★★ BEST PAIR
        ("PIPPIN/USDT:USDT",       "A"),   # +0.200R, 159t, 48% WR ★★★
        ("CYBER/USDT:USDT",        "A"),   # +0.173R, 119t, 47% WR ★★★
        ("WIF/USDT:USDT",          "A"),   # +0.173R, 155t, 50% WR ★★★
        ("VVV/USDT:USDT",          "A"),   # +0.152R, 127t, 46% WR ★★★
        ("JTO/USDT:USDT",          "A"),   # +0.126R, 117t, 45% WR ★★★
        ("PUMPFUN/USDT:USDT",      "A"),   # +0.100R, 195t, 44% WR ★★★
        ("AXS/USDT:USDT",          "A"),   # +0.090R,  93t, 44% WR ★★
        ("SPACE/USDT:USDT",        "B"),   # $35M tv, 27.9% rng ★LIQ
        ("SOL/USDT:USDT",          "B"),   # $863M tv, 6.5% rng  ★LIQ
        ("XRP/USDT:USDT",          "B"),   # $388M tv, 6.2% rng  ★LIQ
        ("RIVER/USDT:USDT",        "B"),   # $173M tv, 36.3% rng ★LIQ
        ("DOGE/USDT:USDT",         "B"),   # $148M tv, 5.8% rng  ★LIQ
        ("ESP/USDT:USDT",          "B"),   # $121M tv, 58.7% rng ★LIQ
        ("ORCA/USDT:USDT",         "B"),   # $116M tv, 23.5% rng ★LIQ
        ("HYPE/USDT:USDT",         "B"),   # $114M tv, 4.9% rng  ★LIQ
        ("WLFI/USDT:USDT",         "B"),   # $113M tv, 14.6% rng ★LIQ
        ("1000PEPE/USDT:USDT",     "B"),   # $89M tv, 6.0% rng   ★LIQ
        ("SUI/USDT:USDT",          "B"),   # $66M tv, 8.8% rng   ★LIQ
        ("ZEC/USDT:USDT",          "B"),   # $65M tv, 11.2% rng  ★LIQ
        ("OP/USDT:USDT",           "B"),   # $62M tv, 35.1% rng  ★LIQ
        ("ADA/USDT:USDT",          "B"),   # $61M tv, 6.0% rng   ★LIQ
        ("INJ/USDT:USDT",          "B"),   # $47M tv, 16.7% rng  ★LIQ
        ("FARTCOIN/USDT:USDT",     "B"),   # $45M tv, 10.2% rng  ★LIQ
        ("LINK/USDT:USDT",         "B"),   # $40M tv, 4.8% rng   ★LIQ
        ("AAVE/USDT:USDT",         "B"),   # $36M tv, 6.6% rng   ★LIQ
        ("TAO/USDT:USDT",          "B"),   # $35M tv, 8.0% rng   ★LIQ
        ("ATOM/USDT:USDT",         "B"),   # $29M tv, 10.3% rng  ★LIQ
        ("ENA/USDT:USDT",          "B"),   # $24M tv, 7.8% rng   ★LIQ
        ("DOT/USDT:USDT",          "B"),   # $24M tv, 6.5% rng   ★LIQ
        ("NEAR/USDT:USDT",         "B"),   # $23M tv, 6.3% rng   ★LIQ
        ("AVAX/USDT:USDT",         "B"),   # $22M tv, 4.6% rng   ★LIQ
    ],
    "london": [
        ("LAB/USDT:USDT",          "A"),   # 0.412R, 42t, 60% WR ★NEW
        ("HANA/USDT:USDT",         "A"),   # 0.372R, 38t, 58% WR
        ("IRYS/USDT:USDT",         "A"),   # 0.368R, 33t, 58% WR
        ("MYX/USDT:USDT",          "A"),   # 0.308R, 71t, 55% WR ★NEW
        ("CYS/USDT:USDT",          "A"),   # 0.242R, 31t, 52% WR
        ("ALCH/USDT:USDT",         "A"),   # 0.268R, 54t, 54% WR
        ("ENSO/USDT:USDT",         "A"),   # 0.240R, 36t, 53% WR
        ("WHITEWHALE/USDT:USDT",   "B"),   # 0.217R, 16t, 50% WR PROV
        ("APEX/USDT:USDT",         "A"),   # 0.206R, 43t, 51% WR ★NEW
        ("UAI/USDT:USDT",          "B"),   # 0.167R, 29t, 48% WR PROV
        ("F/USDT:USDT",            "A"),   # 0.156R, 57t, 49% WR
        ("VANA/USDT:USDT",         "B"),   # 0.501R, 28t, 64% WR PROV ★DISC
        ("TAI/USDT:USDT",          "A"),   # 0.250R, 45t, 53% WR ★DISC
        ("CLOUD/USDT:USDT",        "B"),   # 0.234R, 26t, 54% WR PROV ★DISC
        ("SENT/USDT:USDT",         "B"),   # 0.136R, 25t, 48% WR PROV ★DISC
        ("WAVES/USDT:USDT",        "A"),   # 0.113R, 33t, 48% WR ★DISC
        # ── NEW LIQUID PAIRS (added 2025-02-19) ──────────────────
        # Top performers promoted to A (backtest verified E(R)>0, 40+ trades)
        ("POWER/USDT:USDT",        "A"),   # +0.427R, 41t, 54% WR ★★★ BEST PAIR
        ("PIPPIN/USDT:USDT",       "A"),   # +0.200R, 159t, 48% WR ★★★
        ("WIF/USDT:USDT",          "A"),   # +0.173R, 155t, 50% WR ★★★
        ("CYBER/USDT:USDT",        "A"),   # +0.173R, 119t, 47% WR ★★★
        ("VVV/USDT:USDT",          "A"),   # +0.152R, 127t, 46% WR ★★★
        ("JTO/USDT:USDT",          "A"),   # +0.126R, 117t, 45% WR ★★★
        ("PUMPFUN/USDT:USDT",      "A"),   # +0.100R, 195t, 44% WR ★★★
        ("AXS/USDT:USDT",          "A"),   # +0.090R,  93t, 44% WR ★★
        ("SPACE/USDT:USDT",        "B"),   # $35M tv, 27.9% rng ★LIQ
        ("SOL/USDT:USDT",          "B"),   # $863M tv, 6.5% rng  ★LIQ
        ("XRP/USDT:USDT",          "B"),   # $388M tv, 6.2% rng  ★LIQ
        ("RIVER/USDT:USDT",        "B"),   # $173M tv, 36.3% rng ★LIQ
        ("DOGE/USDT:USDT",         "B"),   # $148M tv, 5.8% rng  ★LIQ
        ("ESP/USDT:USDT",          "B"),   # $121M tv, 58.7% rng ★LIQ
        ("ORCA/USDT:USDT",         "B"),   # $116M tv, 23.5% rng ★LIQ
        ("HYPE/USDT:USDT",         "B"),   # $114M tv, 4.9% rng  ★LIQ
        ("WLFI/USDT:USDT",         "B"),   # $113M tv, 14.6% rng ★LIQ
        ("1000PEPE/USDT:USDT",     "B"),   # $89M tv, 6.0% rng   ★LIQ
        ("SUI/USDT:USDT",          "B"),   # $66M tv, 8.8% rng   ★LIQ
        ("ZEC/USDT:USDT",          "B"),   # $65M tv, 11.2% rng  ★LIQ
        ("OP/USDT:USDT",           "B"),   # $62M tv, 35.1% rng  ★LIQ
        ("ADA/USDT:USDT",          "B"),   # $61M tv, 6.0% rng   ★LIQ
        ("INJ/USDT:USDT",          "B"),   # $47M tv, 16.7% rng  ★LIQ
        ("FARTCOIN/USDT:USDT",     "B"),   # $45M tv, 10.2% rng  ★LIQ
        ("LINK/USDT:USDT",         "B"),   # $40M tv, 4.8% rng   ★LIQ
        ("AAVE/USDT:USDT",         "B"),   # $36M tv, 6.6% rng   ★LIQ
        ("TAO/USDT:USDT",          "B"),   # $35M tv, 8.0% rng   ★LIQ
        ("ATOM/USDT:USDT",         "B"),   # $29M tv, 10.3% rng  ★LIQ
        ("ENA/USDT:USDT",          "B"),   # $24M tv, 7.8% rng   ★LIQ
        ("DOT/USDT:USDT",          "B"),   # $24M tv, 6.5% rng   ★LIQ
        ("NEAR/USDT:USDT",         "B"),   # $23M tv, 6.3% rng   ★LIQ
        ("AVAX/USDT:USDT",         "B"),   # $22M tv, 4.6% rng   ★LIQ
    ],
    "ny": [
        ("IRYS/USDT:USDT",         "A"),   # 0.325R, 36t, 56% WR
        ("METIS/USDT:USDT",        "A"),   # 0.289R, 65t, 55% WR
        ("ZKP/USDT:USDT",          "B"),   # 0.257R, 19t, 53% WR PROV
        ("LAB/USDT:USDT",          "A"),   # 0.255R, 38t, 53% WR ★NEW
        ("PUFFER/USDT:USDT",       "A"),   # 0.224R, 63t, 52% WR
        ("CLO/USDT:USDT",          "A"),   # 0.221R, 46t, 52% WR
        ("WHITEWHALE/USDT:USDT",   "B"),   # 0.217R, 16t, 50% WR PROV
        ("KITE/USDT:USDT",         "A"),   # 0.210R, 39t, 51% WR
        ("STBL/USDT:USDT",         "A"),   # 0.174R, 64t, 48% WR
        ("RENDER/USDT:USDT",       "A"),   # 0.160R, 65t, 49% WR
        ("EDEN/USDT:USDT",         "A"),   # 0.157R, 51t, 49% WR
        ("RECALL/USDT:USDT",       "A"),   # 0.156R, 49t, 49% WR
        ("COAI/USDT:USDT",         "A"),   # 0.128R, 53t, 47% WR ★DISC
        ("CLOUD/USDT:USDT",        "B"),   # 0.127R, 29t, 48% WR PROV ★DISC
        ("ZAMA/USDT:USDT",         "B"),   # 0.486R, 13t, 62% WR PROV ★DISC
        ("MANTA/USDT:USDT",        "A"),   # 0.064R, 72t, 46% WR ★DISC
        # ── NEW LIQUID PAIRS (added 2025-02-19) ──────────────────
        # Top performers promoted to A (backtest verified E(R)>0, 40+ trades)
        ("POWER/USDT:USDT",        "A"),   # +0.427R, 41t, 54% WR ★★★ BEST PAIR
        ("PIPPIN/USDT:USDT",       "A"),   # +0.200R, 159t, 48% WR ★★★
        ("WIF/USDT:USDT",          "A"),   # +0.173R, 155t, 50% WR ★★★
        ("CYBER/USDT:USDT",        "A"),   # +0.173R, 119t, 47% WR ★★★
        ("VVV/USDT:USDT",          "A"),   # +0.152R, 127t, 46% WR ★★★
        ("JTO/USDT:USDT",          "A"),   # +0.126R, 117t, 45% WR ★★★
        ("PUMPFUN/USDT:USDT",      "A"),   # +0.100R, 195t, 44% WR ★★★
        ("AXS/USDT:USDT",          "A"),   # +0.090R,  93t, 44% WR ★★
        ("SPACE/USDT:USDT",        "B"),   # $35M tv, 27.9% rng ★LIQ
        ("SOL/USDT:USDT",          "B"),   # $863M tv, 6.5% rng  ★LIQ
        ("XRP/USDT:USDT",          "B"),   # $388M tv, 6.2% rng  ★LIQ
        ("RIVER/USDT:USDT",        "B"),   # $173M tv, 36.3% rng ★LIQ
        ("DOGE/USDT:USDT",         "B"),   # $148M tv, 5.8% rng  ★LIQ
        ("ESP/USDT:USDT",          "B"),   # $121M tv, 58.7% rng ★LIQ
        ("ORCA/USDT:USDT",         "B"),   # $116M tv, 23.5% rng ★LIQ
        ("HYPE/USDT:USDT",         "B"),   # $114M tv, 4.9% rng  ★LIQ
        ("WLFI/USDT:USDT",         "B"),   # $113M tv, 14.6% rng ★LIQ
        ("1000PEPE/USDT:USDT",     "B"),   # $89M tv, 6.0% rng   ★LIQ
        ("SUI/USDT:USDT",          "B"),   # $66M tv, 8.8% rng   ★LIQ
        ("ZEC/USDT:USDT",          "B"),   # $65M tv, 11.2% rng  ★LIQ
        ("OP/USDT:USDT",           "B"),   # $62M tv, 35.1% rng  ★LIQ
        ("ADA/USDT:USDT",          "B"),   # $61M tv, 6.0% rng   ★LIQ
        ("INJ/USDT:USDT",          "B"),   # $47M tv, 16.7% rng  ★LIQ
        ("FARTCOIN/USDT:USDT",     "B"),   # $45M tv, 10.2% rng  ★LIQ
        ("LINK/USDT:USDT",         "B"),   # $40M tv, 4.8% rng   ★LIQ
        ("AAVE/USDT:USDT",         "B"),   # $36M tv, 6.6% rng   ★LIQ
        ("TAO/USDT:USDT",          "B"),   # $35M tv, 8.0% rng   ★LIQ
        ("ATOM/USDT:USDT",         "B"),   # $29M tv, 10.3% rng  ★LIQ
        ("ENA/USDT:USDT",          "B"),   # $24M tv, 7.8% rng   ★LIQ
        ("DOT/USDT:USDT",          "B"),   # $24M tv, 6.5% rng   ★LIQ
        ("NEAR/USDT:USDT",         "B"),   # $23M tv, 6.3% rng   ★LIQ
        ("AVAX/USDT:USDT",         "B"),   # $22M tv, 4.6% rng   ★LIQ
    ],
}

# ─── Derived lookups ───
# Flat list of (pair, class) tuples
_ALL_PAIR_CLASSES = {}
for _session, _pair_list in PAIRS.items():
    for _pair, _cls in _pair_list:
        if _pair not in _ALL_PAIR_CLASSES:
            _ALL_PAIR_CLASSES[_pair] = _cls
        elif _cls == "A":
            _ALL_PAIR_CLASSES[_pair] = "A"  # best class wins

# Initial pair class (used at first startup, then state takes over)
INITIAL_PAIR_CLASS: Dict[str, str] = dict(_ALL_PAIR_CLASSES)

# Flat unique list of all pair symbols
ALL_PAIRS = sorted(_ALL_PAIR_CLASSES.keys())

def pairs_for_session(session: str) -> List[str]:
    """Return flat list of pair symbols for a session."""
    return [p for p, _ in PAIRS.get(session, [])]

def pair_class_for_session(session: str, pair: str) -> str:
    """Return the default class for a pair in a specific session."""
    for p, cls in PAIRS.get(session, []):
        if p == pair:
            return cls
    return "B"  # unknown → B

# ─── Backtest Baseline (5m ONLY — 15m killed, was -3.8R in 7 trades) ──
# From volume_hunt.py analysis (2026-02-20).  5m only: 34 trades,
# 50% WR, +4.596R total.  15m was 14.3% WR, pure bleed.
# ─── Backtest Baseline (Guardian v3 Trail — 12,355 trades) ───
# Guardian v3: +1,738R total, 50.3% WR, PF 1.28, avg_win ~1.28R
# Live 5m data: 5W/6L, 45% WR, avg_win 1.36R, avg_loss 0.77R
# With trail + tier protection: E(R) = +0.255 per trade
BACKTEST_WR             = 50.0      # win rate % (Guardian v3 trail)
BACKTEST_EXPECTANCY_R   = 0.141     # R per trade (Guardian v3: +1738R/12355 trades)
BACKTEST_PF             = 1.28      # profit factor (Guardian v3)
BACKTEST_AVG_WIN_R      = 1.280     # avg winning trade R (trail mode)
BACKTEST_AVG_LOSS_R     = 0.770     # avg losing trade R (tier-protected)
BACKTEST_TRADES_PER_DAY = 8.0       # target: 8 trades/day for x10 pace
BACKTEST_START_EQUITY   = 151.40    # actual equity at aggressive mode activation

# ─── Dynamic Pair Scanner ───
# Scans Bybit BEFORE each session for pairs that are liquid and moving NOW.
# Volume sweet spot from live data: $20M-$100M = 80% WR, +5.874R in 5 trades.
# $5M-$100M range had 94 qualifying pairs avg 0.022% spread, 12.7% range.
SCAN_MIN_TURNOVER   = 5_000_000     # $5M minimum rolling 24h turnover
SCAN_MAX_TURNOVER   = 100_000_000   # $100M cap (avoid mega-caps with no range)
SCAN_MAX_SPREAD_PCT = 0.10          # 0.10% max bid-ask spread (SL slippage guard)
SCAN_MIN_RANGE_PCT  = 4.0           # 4% minimum 24h high-low range (need volatility)
SCAN_MAX_PRICE      = 500           # Skip coins >$500 (BTC/ETH sizing issues on small acct)
SCAN_MAX_PAIRS      = 60            # Cap total pairs per session

# ── Pre-Session Intelligence ──
# Profiles each candidate pair using 24h of candle history to assess
# breakout follow-through, congestion zones, volatility regime, etc.
INTEL_ENABLED       = True          # Enable pair intelligence profiling
INTEL_MIN_FITNESS   = 25            # Minimum fitness score for B-class pairs (0-100)
SR_SENSITIVITY      = "normal"      # S/R detection: "weak" (obvious only), "normal", "strong" (subtle)

# ─── Dynamic Hybrid Engine (real-time adaptive intelligence) ───
# DynamicEngine runs during every session, adapting risk in real-time.
# All params prefixed DYN_ so the engine can load them via _cfg().
DYN_ENABLED                  = True   # Master switch for dynamic engine
DYN_HEAT_MAX_CONSEC_LOSS     = 3      # consecutive losses → enter cooldown
DYN_HEAT_COOLDOWN_TRADES     = 2      # trades at reduced size after cooldown triggers
DYN_HEAT_COOLDOWN_MULT       = 0.50   # risk multiplier during cooldown (50%)
DYN_MOMENTUM_BOOST_THRESHOLD = 3      # consecutive session wins → momentum boost
DYN_MOMENTUM_BOOST_MULT      = 1.15   # risk multiplier on momentum (115%)
DYN_SESSION_LOSS_CAP_R       = -3.0   # cumulative R cap → halt session trading
DYN_SESSION_LOSS_CAP_TRADES  = 2      # min trades before loss cap activates
DYN_BANKROLL_PHASE_X2        = 0.15   # equity growth fraction → phase 2 (GROWTH)
DYN_BANKROLL_PHASE_X5        = 0.50   # equity growth fraction → phase 3 (COMPOUND)
DYN_REGIME_BTC_DUMP          = -3.0   # BTC 24h % below this → "dump" regime
DYN_REGIME_BTC_CRASH         = -5.0   # BTC 24h % below this → "crash" regime
DYN_REGIME_BTC_PUMP          = 3.0    # BTC 24h % above this → "pump" regime
DYN_REGIME_BTC_RALLY         = 5.0    # BTC 24h % above this → "rally" regime
DYN_MARKET_WIDE_FAIL_PCT     = 0.75   # if >=75% session breakouts fail → hostile
DYN_MARKET_WIDE_MIN_SAMPLE   = 3      # min resolved trades for market-wide signal
DYN_START_EQUITY             = 150.0  # starting equity for bankroll phase calculation

# Proven pairs — A-class = live winners + backtest-verified profitable.
# Backtest: TP=1.5R, 5x leverage, 20+ trades, positive E(R).
# Mass backtest 2026-02-20: 47 Bybit pairs tested, 20 qualified A-class.
# Live winners (CLO/HANA/SUPER/IRYS) kept despite weak backtest — live proof.
SCAN_ALWAYS_TRADE = {
    # ── Backtest-verified A-class (E(R) > 0, 20+ trades) ──
    "OPEN/USDT:USDT",          # +0.221R, 100t, 50% WR ★★★
    "WHITEWHALE/USDT:USDT",    # +0.216R,  37t, 49% WR ★★★
    "PIPPIN/USDT:USDT",        # +0.200R, 159t, 48% WR ★★★ (promoted from LIQ)
    "FHE/USDT:USDT",           # +0.181R,  36t, 47% WR ★★
    "CYBER/USDT:USDT",         # +0.173R, 119t, 47% WR ★★★ (promoted from LIQ)
    "WIF/USDT:USDT",           # +0.173R, 155t, 50% WR ★★★ (promoted from LIQ)
    "ALCH/USDT:USDT",          # +0.169R, 139t, 47% WR ★★★
    "CLOUD/USDT:USDT",         # +0.160R, 115t, 46% WR ★★★
    "CYS/USDT:USDT",           # +0.156R,  46t, 46% WR ★★
    "VVV/USDT:USDT",           # +0.152R, 127t, 46% WR ★★★ (promoted from LIQ)
    "F/USDT:USDT",             # +0.150R, 113t, 46% WR ★★★
    "API3/USDT:USDT",          # +0.133R, 122t, 45% WR ★★★
    "ENSO/USDT:USDT",          # +0.130R,  91t, 45% WR ★★★
    "JTO/USDT:USDT",           # +0.126R, 117t, 45% WR ★★★ (promoted from LIQ)
    "PUMPFUN/USDT:USDT",       # +0.100R, 195t, 44% WR ★★★ (promoted from LIQ)
    "STBL/USDT:USDT",          # +0.096R,  86t, 44% WR ★★
    "AXS/USDT:USDT",           # +0.090R,  93t, 44% WR ★★ (promoted from LIQ)
    "SENT/USDT:USDT",          # +0.080R,  44t, 43% WR ★★
    "KITE/USDT:USDT",          # +0.069R,  67t, 43% WR ★★
    "TRUST/USDT:USDT",         # +0.050R,  50t, 42% WR ★
    "UB/USDT:USDT",            # +0.050R, 100t, 42% WR ★★
    "BREV/USDT:USDT",          # +0.042R,  32t, 41% WR ★
    "US/USDT:USDT",            # +0.042R,  48t, 42% WR ★
    "KERNEL/USDT:USDT",        # +0.033R, 121t, 41% WR ★★
    "RECALL/USDT:USDT",        # +0.023R,  88t, 41% WR ★★
    "VANA/USDT:USDT",          # +0.017R, 118t, 41% WR ★★
    "MYX/USDT:USDT",           # +0.006R, 164t, 40% WR ★★
    # ── Live winners (kept despite weak backtest) ──
    "CLO/USDT:USDT",           # live winner (BT: -0.191R — overfit to live?)
    "HANA/USDT:USDT",          # live winner (BT: -0.012R — borderline)
    "SUPER/USDT:USDT",         # live winner (BT: -0.083R — monitor)
    "IRYS/USDT:USDT",          # live winner (BT: -0.224R — monitor closely)
    # ── High WR additions ──
    "POWER/USDT:USDT",         # +0.427R,  41t, 54% WR ★★★ (best pair overall!)
}

# ─── Operational ───
EQUITY_FLOOR    = 0         # disabled — trade with whatever equity we have

TIMEFRAME       = "5m"
POLL_INTERVAL   = 5         # seconds between checks within a candle
STATE_FILE      = "live/state.json"
LOG_DIR         = "live/logs"
TRADE_LOG       = "live/trades.csv"
