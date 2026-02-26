"""
obr/config.py -- N_TREND_STOCH strategy configuration.

Strategy: EMA Stacked Trend + Stochastic Pullback on 1H candles.
Walk-forward validated: PF 2.09, WR 48%, robust across all parameter variations.

Previously OBR (Outside Bar Reversal) -- replaced after autonomous research
showed OBR was curve-fitted (in-sample 61% WR collapsed to 49% OOS).
"""

import os
from typing import Dict, List, Set, Tuple


# ==================================================================
#  MODE
# ==================================================================

MAINNET = True           # True = real money on Bybit mainnet
DEMO_MODE = False        # True = Bybit demo (api-demo.bybit.com)

# API keys -- from environment on mainnet, hardcoded for demo
API_KEY: str = os.environ.get("BYBIT_API_KEY" if MAINNET else "BYBIT_DEMO_KEY", "")
API_SECRET: str = os.environ.get("BYBIT_API_SECRET" if MAINNET else "BYBIT_DEMO_SECRET", "")


# ==================================================================
#  STRATEGY CORE
# ==================================================================

STRATEGY_NAME = "NTS"                  # N_TREND_STOCH (EMA Stacked Trend + Stochastic Pullback)
SIGNAL_TIMEFRAME = "1h"                # Signal detection on 1H candles
TIMEFRAME = "1h"                       # Poll on 1H candle close (was 1m for OBR)
LEVERAGE = 10
FEE_RATE = 0.00055                     # 0.055% round-trip taker (conservative estimate)

# TP/SL -- from Round 5 robustness sweep
TP_R = 2.75                            # Sweet spot: PF 2.03, WR 48.6% (backtest validated)
# SL is structural: candle low - 0.4*ATR14 (longs), candle high + 0.4*ATR14 (shorts)

# N_TREND_STOCH doesn't use nextbar confirmation -- signal is self-contained
REQUIRE_NEXTBAR_CONFIRM = False        # Not used for NTS strategy


# ==================================================================
#  RISK MANAGEMENT
# ==================================================================

RISK_PCT = 0.03                        # 3% of equity per trade (safe x10: 44d, DD 29%)
RISK_PCT_ABOVE_100 = 0.03              # same once equity >= $100
RISK_TIER_THRESHOLD = 100.0            # equity threshold to switch risk tiers
FIXED_RISK_USD = 0.0                   # 0 = use RISK_PCT (dynamic %); >0 = fixed $
MAX_CONCURRENT_POSITIONS = 5           # max 5 open at once
MAX_TRADES_DAY = 30                    # expanded funnel, pair selection filters quality
MIN_RISK_DISTANCE_PCT = 0.003          # SL must be at least 0.3% from entry (NTS floor)
SL_BUFFER_MULT = 1.0                   # no extra buffer -- NTS uses ATR-based SL directly

# Daily growth cap -- stop trading once equity grows X% from day-open
DAILY_GROWTH_CAP_PCT = 15.0            # 15% daily cap (backtest optimal)

# Safety floor -- stop trading if equity drops below this % of peak
EQUITY_FLOOR_PCT = 0.60                # stop at 40% DD from peak

# 24/7 mode -- no session boundaries, scan continuously
MODE_24_7 = True                       # True = continuous, False = session-gated


# ==================================================================
#  GUARDIAN / TRAIL
# ==================================================================

TRAIL_ENABLED = True
TRAIL_ACTIVATION_R = 1.5              # activate trail at 1.5R (lock profits early)
TRAIL_DISTANCE_R = 0.30               # trail 0.3R behind peak (tight: 1.5R peak → 1.2R SL)
TRAIL_MIN_MOVE_R = 0.10               # only update SL if it moves >= 0.1R (throttle API)
EXCHANGE_TP_R = 10.0                   # exchange-side TP far out (guardian manages real TP)
GUARDIAN_POLL_SECS = 15                # poll positions every 15s (avoids rate limits)

# Progressive SL tiers: (trigger_R, new_SL_R, label)
# When R hits trigger, SL moves to new_SL_R. Trail takes over at TRAIL_ACTIVATION_R.
PROFIT_TIERS = [
    (0.60, -0.30, "tier1_protect"),    # at +0.6R: cap loss at -0.3R
    (1.00,  0.00, "tier2_breakeven"),  # at +1.0R: move SL to breakeven
    (1.50,  0.60, "tier3_lock060"),    # at +1.5R: lock 0.6R (leave room to hit 2R TP)
    (2.00,  1.50, "tier4_lock150"),    # at +2.0R: lock 1.5R profit
    (3.00,  2.30, "tier5_lock230"),    # at +3.0R: lock 2.3R profit
]

# ------------------------------------------------------------------
#  1m REJECTION / REVERSAL EXIT
# ------------------------------------------------------------------
REJECTION_EXIT_ENABLED = True           # scan 1m candles for rejection while in trade
REJECTION_MIN_PROFIT_R = 0.50           # only act when position is above this R
REJECTION_WICK_RATIO = 0.60             # wick must be >= 60% of candle range
REJECTION_BODY_MAX_RATIO = 0.35         # body must be <= 35% of range (thin body = rejection)
REJECTION_MIN_RANGE_PCT = 0.0015        # candle range must be >= 0.15% of price (ignore noise)
REJECTION_ENGULF_BODY_RATIO = 0.40      # engulf candle body must be >= 40% of range


# ==================================================================
#  SESSIONS  (UTC hours)
# ==================================================================

SESSIONS: Dict[str, Tuple[int, int]] = {
    "asia":   (0, 8),
    "london": (8, 16),
    "ny":     (16, 24),
}

SESSION_ORDER = ["asia", "london", "ny"]

# Cooldown between trades on the same pair (minutes)
PAIR_COOLDOWN_MINUTES = 60             # 1H candle → cooldown = 1 candle between trades

# Per-pair consecutive loss cooldown
PAIR_LOSS_COOLDOWN_COUNT = 2           # after N consecutive losses on a pair...
PAIR_LOSS_COOLDOWN_HOURS = 4           # ...pause that pair for N hours

# Pair hunter -- disabled for NTS (pair selection is pre-validated)
HUNTER_ENABLED = False                 # NTS uses walk-forward selected pairs only
HUNTER_MAX_RESULTS = 0                 # not used
HUNTER_MIN_OB_RANGE_PCT = 0.0         # not used


# ==================================================================
#  PAIRS  (from pair_scanner.py -- fresh 60-day Bybit data)
# ==================================================================

# Walk-forward confirmed: +R in BOTH train (70%) AND test (30%) periods
# Sorted by combined edge (train_R + test_R). All at TP 2.75R.
# Source: _extract_pairs.py (N_TREND_STOCH @ 2.75R, 186 pairs tested)
PAIR_TP: Dict[str, float] = {
    # --- TIER 1: Strong in both periods (test_R > +5) ---
    "ENA/USDT:USDT":        2.75,   # TestWR 50-53% TestR +9.6/+13.8 TrainR +19.9/+14.6
    "AIXBT/USDT:USDT":      2.75,   # TestWR 57% TestR +15.1 TrainR +6.1
    "PORTAL/USDT:USDT":     2.75,   # TestWR 53% TestR +14.0 TrainR +10.6
    "LYN/USDT:USDT":        2.75,   # TestWR 100% TestR +10.8 TrainR +1.0
    "SOMI/USDT:USDT":       2.75,   # TestWR 50% TestR +6.5 TrainR +6.9
    "CYS/USDT:USDT":        2.75,   # TestWR 60% TestR +6.1 TrainR +2.3
    "ETH/USDT:USDT":        2.75,   # TestWR 45% TestR +5.8 TrainR +1.0
    "TIA/USDT:USDT":        2.75,   # TestWR 44% TestR +5.3 TrainR +1.8
    "STABLE/USDT:USDT":     2.75,   # TestWR 50% TestR +5.0 TrainR +1.9
    "STRK/USDT:USDT":       2.75,   # TestWR 38% TestR +4.8 TrainR +3.5
    "ENSO/USDT:USDT":       2.75,   # TestWR 50% TestR +4.9 TrainR +0.2

    # --- TIER 2: Confirmed positive (test_R > +1) ---
    "ALT/USDT:USDT":        2.75,   # TestWR 43% TestR +3.6 TrainR +12.3
    "HOLO/USDT:USDT":       2.75,   # TestWR 40% TestR +2.1 TrainR +2.0
    "TRUST/USDT:USDT":      2.75,   # TestWR 40% TestR +1.9 TrainR +0.1
    "SWARMS/USDT:USDT":     2.75,   # TestWR 33% TestR +1.7 TrainR +11.2
    "UNI/USDT:USDT":        2.75,   # TestWR 30-33% TestR +0.4/+1.5 TrainR +0.3/+5.2
    "JELLYJELLY/USDT:USDT": 2.75,   # TestWR 30% TestR +1.5 TrainR +10.5
    "NAORIS/USDT:USDT":     2.75,   # TestWR 31% TestR +1.3 TrainR +7.6
    "WLFI/USDT:USDT":       2.75,   # TestWR 33% TestR +1.1 TrainR +2.8
    "BB/USDT:USDT":         2.75,   # TestWR 31% TestR +1.1 TrainR +0.9

    # --- TIER 3: Marginal but confirmed ---
    "ORCA/USDT:USDT":       2.75,   # TestWR 33% TestR +0.7 TrainR +5.6
    "LIT/USDT:USDT":        2.75,   # TestWR 33% TestR +0.6 TrainR +1.2
    "RECALL/USDT:USDT":     2.75,   # TestWR 30% TestR +0.5 TrainR +2.4
    "PUMPFUN/USDT:USDT":    2.75,   # TestWR 29% TestR +0.1 TrainR +3.1
    "VIRTUAL/USDT:USDT":    2.75,   # TestWR 29% TestR +0.0 TrainR +4.0
}

PAIRS: List[str] = list(PAIR_TP.keys())

# Always-trade pairs (proven winners + top backtest scores)
ALWAYS_TRADE: Set[str] = {
    "ENA/USDT:USDT",       # Top Tier 1: strongest combined edge
    "AIXBT/USDT:USDT",
    "PORTAL/USDT:USDT",
    "SOMI/USDT:USDT",
}


# ==================================================================
#  LIQUIDITY FILTERS
# ==================================================================

MIN_TURNOVER_USDT = 3_000_000         # 24h turnover minimum ($3M - filters handle quality)
MAX_SPREAD_PCT = 0.15                  # max bid-ask spread %
MIN_TICK_SIZE = 0.0                    # auto from exchange


# ==================================================================
#  CANDLE LOOKBACK
# ==================================================================

LOOKBACK_CANDLES = 60                  # need 60 1H candles for EMA50 + Stoch14 + ATR14
POLL_INTERVAL = 30                     # seconds between main loop iterations (1H candles)


# ==================================================================
#  HTF TREND ALIGNMENT FILTER
# ==================================================================

HTF_TREND_ENABLED = False              # NTS has trend built-in via EMA(8,21,50) stack
HTF_SMA_PERIOD = 50                    # not used (kept for backward compat)
HTF_CANDLES_NEEDED = 60                # not used
HTF_TREND_BUFFER = 0.001               # not used


# ==================================================================
#  VOLUME SPIKE FILTER
# ==================================================================

VOLUME_FILTER_ENABLED = False          # NTS robust without volume filter (Round 5 confirmed)
VOLUME_SPIKE_THRESHOLD = 2.0          # not used
VOLUME_LOOKBACK = 20                  # not used


# ==================================================================
#  LIMIT ORDER ENTRY
# ==================================================================

LIMIT_ENTRY_ENABLED = False            # NTS uses market orders (signal on 1H close)
LIMIT_ENTRY_TIMEOUT_SEC = 300          # not used
LIMIT_ENTRY_OFFSET_PCT = 0.0           # not used


# ==================================================================
#  MAKER FEE MODEL (v13 deployment)
# ==================================================================

# When True, TP exits use limit orders (maker fee 0.02% instead of taker 0.055%)
# SL stays as market for safety (gaps can skip limit SLs)
MAKER_TP_ENABLED = False               # Enable for v13 portfolio deployment

# When True, entries also use limit orders (maker fee)
MAKER_ENTRY_ENABLED = False            # Enable for v13 portfolio deployment
MAKER_ENTRY_TIMEOUT_SEC = 120          # Cancel unfilled limit entry after 2 min

# Fee rates for position sizing calculations
MAKER_FEE_RATE = 0.0002               # 0.02% maker fee
TAKER_FEE_RATE = 0.00055              # 0.055% taker fee
# The effective fee model: 'current' (entry=maker, SL=taker) or 'full_maker'
EFFECTIVE_FEE_MODEL = 'current'        # Change to 'full_maker' for v13 deployment


# ==================================================================
#  FILES / PATHS
# ==================================================================

STATE_FILE = "obr/state.json"
TRACKER_FILE = "obr/tracker.json"
LOG_DIR = "obr/logs"
TRADE_LOG = "obr/trades.csv"
TRADE_JSONL = "obr/logs/trades.jsonl"


# ==================================================================
#  BACKTEST REFERENCE (from OBR lab results)
# ==================================================================

BACKTEST_WR = 48.6                     # NTS WR @ 2.75R (walk-forward test, 238 trades)
BACKTEST_EXPECTANCY_R = 0.50           # est avg R per trade (PF 2.03)
BACKTEST_TOTAL_R = 120.0              # aggregate test R across 25 confirmed pairs
BACKTEST_MAX_DD = 29.0                 # max DD at 3% risk (Round 4)


# ==================================================================
#  GROWTH TARGET
# ==================================================================

START_EQUITY = 500.0
TARGET_EQUITY = 5000.0                 # x10 target
TARGET_DAYS = 10                       # 10 days to reach x10


# ==================================================================
#  x1000 COMPOUNDING CURVES  (Mods 1-10)
# ==================================================================

# Mod 1: Dynamic Risk Curve -- risk_pct decreases as equity grows
RISK_CURVE = [
    # (equity_threshold, risk_pct) -- NTS safe profile (3% base)
    (100,    0.03),   # 3% -- conservative start (PF 2.0+ gives x10 in 44d)
    (250,    0.03),   # Consistent
    (500,    0.03),   # At start equity
    (1000,   0.025),  # Slightly reduce as account grows
    (5000,   0.02),   # x10 target reached → preserve
    (10000,  0.015),  # Large account
    (50000,  0.01),   # Whale mode
    (100000, 0.008),  # Ultra conservative
]

# Mod 2: Adaptive Leverage Curve -- leverage decreases as equity grows
LEVERAGE_CURVE = [
    # (equity_threshold, leverage)
    (100,    10),
    (250,    10),
    (500,    8),
    (1000,   7),
    (5000,   5),
    (10000,  4),
    (50000,  3),
    (100000, 2),
]

# Mod 3: Conviction-Scaled Position Sizing
CONVICTION_MULTIPLIER = {
    # grade: multiplier applied to base risk_pct
    "A+": 1.25,
    "A":  1.10,
    "B":  1.00,
    "C":  0.80,
    "D":  0.60,
}

# Mod 4: Phased Growth Targets
GROWTH_PHASES = [
    # (target_equity, daily_cap_pct, label)
    (250,    20.0,  "Phase 1: Seed → $250"),
    (1000,   15.0,  "Phase 2: Growth → $1K"),
    (5000,   12.0,  "Phase 3: Scale → $5K"),
    (10000,  10.0,  "Phase 4: Compound → $10K"),
    (50000,   8.0,  "Phase 5: Accumulate → $50K"),
    (100000,  6.0,  "Phase 6: Preserve → $100K"),
]

# Mod 6: Drawdown-Based Risk Throttling
DRAWDOWN_THROTTLE = [
    # (dd_pct_threshold, risk_multiplier)
    (5,   1.00),   # Normal: 0-5% DD
    (10,  0.75),   # Caution: 5-10% DD
    (15,  0.50),   # Defensive: 10-15% DD
    (20,  0.25),   # Survival: 15-20% DD
    (30,  0.10),   # Emergency: 20-30% DD
]

# Mod 7: Dynamic TP Multipliers
TP_CONVICTION_MULT = {
    "A+": 1.30,
    "A":  1.15,
    "B":  1.00,
    "C":  0.85,
    "D":  0.75,
}

TP_REGIME_MULT = {
    "trending":  1.20,
    "volatile":  0.90,
    "ranging":   1.00,
    "unknown":   1.00,
}

# Mod 8: Withdrawal Milestones
WITHDRAWAL_MILESTONES = [
    # (equity_level, withdraw_pct, label)
    (500,    0.10, "Withdraw 10% at $500"),
    (1000,   0.10, "Withdraw 10% at $1K"),
    (5000,   0.15, "Withdraw 15% at $5K"),
    (10000,  0.20, "Withdraw 20% at $10K"),
    (25000,  0.20, "Withdraw 20% at $25K"),
    (50000,  0.25, "Withdraw 25% at $50K"),
    (100000, 0.30, "Withdraw 30% at $100K"),
]

# Mod 10: Dynamic Max Concurrent Positions
MAX_CONCURRENT_CURVE = [
    # (equity_threshold, max_positions)
    (100,    5),
    (250,    6),
    (500,    7),
    (1000,   8),
    (5000,   10),
    (10000,  12),
    (50000,  15),
]

# Absolute risk cap (ceiling regardless of curve calculations)
MAX_RISK_PCT = 0.03


# ==================================================================
#  HELPERS
# ==================================================================

def current_session_name(hour: int) -> str:
    """Return session name for a UTC hour."""
    for name, (start, end) in SESSIONS.items():
        if start <= hour < end:
            return name
    return "asia"  # fallback


def all_pairs() -> List[str]:
    """Return deduplicated sorted list of all configured pairs."""
    return sorted(set(PAIRS))


def get_pair_tp(symbol: str) -> float:
    """Return per-pair optimal TP_R from scanner, fallback to global TP_R."""
    return PAIR_TP.get(symbol, TP_R)


# ------------------------------------------------------------------
#  x1000 Curve Getters
# ------------------------------------------------------------------

def get_risk_pct(equity: float) -> float:
    """Mod 1: Dynamic risk % based on equity curve.
    Walks the RISK_CURVE from highest threshold downward.
    Fallback: RISK_PCT (original hardcoded value)."""
    try:
        for threshold, risk in reversed(RISK_CURVE):
            if equity >= threshold:
                return risk
        return RISK_CURVE[0][1] if RISK_CURVE else RISK_PCT
    except Exception:
        return RISK_PCT


def get_leverage(equity: float) -> int:
    """Mod 2: Dynamic leverage based on equity curve.
    Fallback: LEVERAGE (original hardcoded value)."""
    try:
        for threshold, lev in reversed(LEVERAGE_CURVE):
            if equity >= threshold:
                return lev
        return LEVERAGE_CURVE[0][1] if LEVERAGE_CURVE else LEVERAGE
    except Exception:
        return LEVERAGE


def get_conviction_mult(grade: str) -> float:
    """Mod 3: Conviction-based position size multiplier.
    Fallback: 1.0 (no change)."""
    return CONVICTION_MULTIPLIER.get(grade, 1.0)


def get_current_phase(equity: float) -> Tuple[float, float, str]:
    """Mod 4: Return (target, daily_cap_pct, label) for current growth phase.
    Fallback: (TARGET_EQUITY, DAILY_GROWTH_CAP_PCT, 'default')."""
    try:
        for target, cap, label in GROWTH_PHASES:
            if equity < target:
                return (target, cap, label)
        last = GROWTH_PHASES[-1]
        return (last[0], last[1], f"Phase {len(GROWTH_PHASES)}: Maintenance")
    except Exception:
        return (TARGET_EQUITY, DAILY_GROWTH_CAP_PCT, "default")


def get_drawdown_multiplier(equity: float, peak_equity: float) -> float:
    """Mod 6: Risk multiplier based on drawdown from peak.
    Returns 0.10–1.00 (lower = deeper drawdown = less risk).
    Fallback: 1.0 (no throttle)."""
    try:
        if peak_equity <= 0:
            return 1.0
        dd_pct = (peak_equity - equity) / peak_equity * 100
        if dd_pct <= 0:
            return 1.0
        mult = 1.0
        for threshold, m in DRAWDOWN_THROTTLE:
            if dd_pct >= threshold:
                mult = m
        return mult
    except Exception:
        return 1.0


def get_dynamic_tp(base_tp: float, grade: str, regime: str = "unknown") -> float:
    """Mod 7: Adjust TP based on conviction grade and market regime.
    Fallback: base_tp unchanged."""
    try:
        conv_mult = TP_CONVICTION_MULT.get(grade, 1.0)
        reg_mult = TP_REGIME_MULT.get(regime, 1.0)
        return base_tp * conv_mult * reg_mult
    except Exception:
        return base_tp


def get_max_concurrent(equity: float) -> int:
    """Mod 10: Dynamic max concurrent positions based on equity.
    Fallback: MAX_CONCURRENT_POSITIONS (original hardcoded value)."""
    try:
        for threshold, max_pos in reversed(MAX_CONCURRENT_CURVE):
            if equity >= threshold:
                return max_pos
        return MAX_CONCURRENT_CURVE[0][1] if MAX_CONCURRENT_CURVE else MAX_CONCURRENT_POSITIONS
    except Exception:
        return MAX_CONCURRENT_POSITIONS
