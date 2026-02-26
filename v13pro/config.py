"""
v13pro/config.py -- All configuration for v13 PRO bot.

Merged from obr/config.py x1000 curves + v13 fee model + guardian params.
Self-contained: no imports from obr/.
"""

import os
from typing import Dict, List, Set, Tuple

# ==================================================================
#  MODE
# ==================================================================

MAINNET = True
DEMO_MODE = False

API_KEY: str = os.environ.get("BYBIT_API_KEY" if MAINNET else "BYBIT_DEMO_KEY", "")
API_SECRET: str = os.environ.get("BYBIT_API_SECRET" if MAINNET else "BYBIT_DEMO_SECRET", "")

# ==================================================================
#  PATHS
# ==================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
STATE_FILE = os.path.join(BASE_DIR, "state.json")
TRACKER_FILE = os.path.join(BASE_DIR, "tracker.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
DEPLOY_COMBOS = os.path.join(BASE_DIR, "deploy_combos.json")

# ==================================================================
#  STRATEGY / EXECUTION
# ==================================================================

# Fee model
MAKER_TP_ENABLED = False          # --maker flag sets True
MAKER_ENTRY_ENABLED = False       # --entry flag sets True
MAKER_ENTRY_TIMEOUT_SEC = 120
MAKER_FEE_RATE = 0.0002           # 0.02%
TAKER_FEE_RATE = 0.00055          # 0.055%
EFFECTIVE_FEE_MODEL = "current"

# Default leverage / risk
LEVERAGE = 8
FEE_RATE = 0.00055
RISK_PCT = 0.02
MAX_RISK_PCT = 0.02
# TP philosophy: NEVER risk more than you can make. Trail min capture >= 1R.

# Trade quality gates
MIN_KEY_LEVEL_SCORE = 5      # shadow: rejected key_level longs still win 62.5% — loosened from 10 to 5
MIN_REWARD_USD = 5.0         # minimum dollar reward to accept trade (skip tiny bets)
MAX_STOP_DIST_PCT = 5.0      # reject signals with stop > 5% (shadow: wider stops won at 51% WR, 2.5% was too tight)
DNA_BOOST_CAP = 6            # max +conviction from DNA profiler (prevent garbage inflation)

# Shadow-validated edge filters
LONG_ONLY_MODE = True         # shadow: longs 59.4% WR, shorts 27.9% WR → block shorts
REQUIRE_OF_ALIGNMENT = False  # legacy flag — replaced by tiered OF gate in bot.py
OF_HARD_BLOCK_IMB = 0.30      # block entry if OF imbalance > this against our direction
                              # shadow: imb -0.20 to 0 wins 83% → only block heavy opposition

# ── Live vs Shadow-only combos ──
# Only these strategy/TF combos place LIVE orders.
# ALL combos still get shadow-tracked for study.
# Empty set = all passed signals trade live (backward compat).
# Populated from shadow analysis 2025-02-25 (longs ExpR > 0):
LIVE_COMBOS = {
    # Tier 1: Strong edge (ExpR > +0.30, proven)
    ("ENGULF",    "15m"),   # ExpR=+1.004  WR=83%  N=12
    ("RSI_FADE",  "1h"),    # ExpR=+0.821  WR=44%  N=9
    ("MTF_RSI",   "15m"),   # ExpR=+0.787  WR=86%  N=7
    ("MOM_SURGE", "1h"),    # ExpR=+0.609  WR=83%  N=6
    ("BB_FADE",   "15m"),   # ExpR=+0.609  WR=61%  N=64
    ("BB_FADE",   "1h"),    # ExpR=+0.590  WR=85%  N=61
    ("BB_BREAK",  "15m"),   # ExpR=+0.365  WR=61%  N=110
    ("TR_PULL",   "1h"),    # ExpR=+0.364  WR=74%  N=95
    # Tier 2: Moderate edge (ExpR +0.03 to +0.30)
    ("EMA_RIB",   "1h"),    # ExpR=+0.214  WR=69%  N=77
    ("RSI_FADE",  "15m"),   # ExpR=+0.172  WR=44%  N=9
    ("BB_BREAK",  "1h"),    # ExpR=+0.163  WR=56%  N=48
    ("IB_BREAK",  "15m"),   # ExpR=+0.154  WR=53%  N=77
    ("PIN_BAR",   "15m"),   # ExpR=+0.147  WR=65%  N=23
    ("TR_PULL",   "15m"),   # ExpR=+0.092  WR=53%  N=208
    ("PIN_BAR",   "1h"),    # ExpR=+0.074  WR=71%  N=7
    ("DONCHIAN",  "15m"),   # ExpR=+0.034  WR=42%  N=123
    # Ensemble signals (portfolio) — allowed through
    ("ENS2",      "1h"),
    ("ENS3",      "1h"),
}
# Shadow-only combos (negative ExpR for longs — study only):
# EMA_RIB/15m (-0.030), IB_BREAK/1h (-0.030), MTF_RSI/1h (-0.091)
# ENGULF/1h (-0.094), DONCHIAN/1h (-0.111), MOM_SURGE/30m (-0.113)
# MOM_SURGE/15m (-0.238)
# → These still shadow-track every signal. When their rolling ExpR
#   turns positive, they auto-promote to LIVE_COMBOS.

# Micro-TF shadow intelligence (3m/5m cross-TF validation)
# These TFs are shadow-only — never place live orders.
# Their outcomes feed cross-TF validation for 15m/1h signals.
MICRO_TFS = {"3m", "5m"}

# Shadow promotion: auto-promote combos from shadow to live
SHADOW_PROMOTE_MIN_TRADES = 30     # need ≥30 shadow outcomes to evaluate
SHADOW_PROMOTE_MIN_EXPR = 0.05     # need ExpR ≥ +0.05 to promote
SHADOW_DEMOTE_MIN_TRADES = 50      # need ≥50 trades before demotion
SHADOW_DEMOTE_MAX_EXPR = -0.10     # demote if ExpR drops below -0.10
SHADOW_REVIEW_INTERVAL = 3600      # review every 1 hour

# Guardian
TRAIL_ENABLED = True
TRAIL_ACTIVATION_R = 1.5       # shadow: let runners reach 1.5R before trailing
TRAIL_DISTANCE_R = 0.50        # shadow: wider trail 0.5R behind peak for room
TRAIL_MIN_MOVE_R = 0.10
EXCHANGE_TP_R = 10.0
GUARDIAN_POLL_SECS = 15
TP_R = 2.75

PROFIT_TIERS = [
    # Philosophy: once in profit, NEVER give back more than 50% of peak.
    # Old tiers let you give back MORE than your initial risk. Never again.
    (0.50, -0.30, "tier0_protect"),     # was 0.30R→-0.60R (losing more than risked!)
    (1.00,  0.30, "tier1_lock030"),     # at breakeven R, lock +0.30R (was 0.00)
    (1.50,  0.80, "tier2_lock080"),     # 1.5R reached → lock 0.80R minimum
    (2.00,  1.50, "tier3_lock150"),     # 2R reached → lock 1.50R
    (3.00,  2.30, "tier4_lock230"),     # 3R reached → lock 2.30R
]

# Burst engine partial TP — locks gains during BURST windows before edge decay
BURST_PARTIAL_TP_ENABLED = True
BURST_PARTIAL_TP_R = 1.0          # trigger partial TP at this R level during BURST
BURST_PARTIAL_TP_PCT = 0.33       # close 33% of position to lock gains
BURST_PARTIAL_TP_COOLDOWN = 0     # 0 = one partial per position (flag-based)

# Rejection exit (1m candle)
REJECTION_EXIT_ENABLED = True
REJECTION_MIN_PROFIT_R = 0.50
REJECTION_WICK_RATIO = 0.60
REJECTION_BODY_MAX_RATIO = 0.35
REJECTION_MIN_RANGE_PCT = 0.0015
REJECTION_ENGULF_BODY_RATIO = 0.40

# Funding rate protection
FUNDING_RATE_MAX_PCT = 0.10     # max acceptable rate per 8h (0.10% = 10bps)
FUNDING_RATE_EXIT_PCT = 0.30    # force-close if rate exceeds this (0.30% = 30bps)
FUNDING_CHECK_INTERVAL = 300    # check funding every 5 min (seconds)

# Risk management
DAILY_GROWTH_CAP_PCT = 0       # disabled — x10 goal, no cap on daily gains
EQUITY_FLOOR_PCT = 0.60
MAX_CONCURRENT_POSITIONS = 6
MAX_TRADES_DAY = 9999          # effectively unlimited — never gate trades by count

# Circuit breaker: pause new entries after sustained daily losses
CIRCUIT_BREAKER_R = -4.0       # pause all new entries when daily PnL drops below this R
CIRCUIT_BREAKER_LOSSES = 8     # or after this many losses in a day with <25% WR

# COLD regime freeze: block new entries when regime is COLD
COLD_REGIME_FREEZE = False      # disabled — let regime multiplier (0.70x) scale risk instead of hard block
PAIR_COOLDOWN_MINUTES = 30     # 30 min cooldown per pair (was 60)
PAIR_LOSS_COOLDOWN_COUNT = 3   # escalating cooldown after 3 consecutive losses
PAIR_LOSS_COOLDOWN_HOURS = 2   # start at 2h (was 4)
PAIR_LOSS_COOLDOWN_MAX_HOURS = 12  # cap at 12h (was 24)

# Risk reduction for repeat losers (consecutive_losses → risk multiplier)
LOSS_STREAK_RISK_MULT = {
    2: 0.75,   # 2 consecutive losses → 75% risk
    3: 0.50,   # 3 consecutive losses → 50% risk
    4: 0.25,   # 4+ consecutive losses → 25% risk
}

# Growth targets
START_EQUITY = 500.0
TARGET_EQUITY = 5000.0
TARGET_DAYS = 10

# ==================================================================
#  SESSIONS
# ==================================================================

SESSIONS: Dict[str, Tuple[int, int]] = {
    "asia":   (0, 8),
    "london": (8, 16),
    "ny":     (16, 24),
}

# ==================================================================
#  x1000 COMPOUNDING CURVES
# ==================================================================

RISK_CURVE = [
    (100,    0.02),    # 2% at all stages — never oversize, let compounding do the work
    (250,    0.02),    # risk $10 at $500 = room for 50 trades before ruin
    (500,    0.02),    # consistent sizing — yesterday NY proved 2% + trail works
    (1000,   0.02),    # keep 2% up to $1k
    (5000,   0.018),
    (10000,  0.015),
    (50000,  0.010),
    (100000, 0.008),
]

LEVERAGE_CURVE = [
    (100,    8),     # NEVER increase leverage as equity drops — that's a death spiral
    (250,    8),
    (500,    8),
    (1000,   8),     # was 7 — keep consistent, don't punish growth
    (5000,   5),
    (10000,  4),
    (50000,  3),
    (100000, 2),
]

CONVICTION_MULTIPLIER = {
    "A+": 1.50, "A": 1.15, "B": 1.00, "C": 0.75, "D": 0.50,
    # A+ longs: +0.544 ExpR (66% WR) → deserve 50% more risk
    # A  longs: +0.185 ExpR (57% WR) → slight boost to 1.15
}

GROWTH_PHASES = [
    (250,    20.0,  "Phase 1: Seed → $250"),
    (1000,   15.0,  "Phase 2: Growth → $1K"),
    (5000,   12.0,  "Phase 3: Scale → $5K"),
    (10000,  10.0,  "Phase 4: Compound → $10K"),
    (50000,   8.0,  "Phase 5: Accumulate → $50K"),
    (100000,  6.0,  "Phase 6: Preserve → $100K"),
]

DRAWDOWN_THROTTLE = [
    (5,   1.00),
    (10,  0.85),   # was 0.75 — softer to allow recovery
    (15,  0.65),   # was 0.50 — at 14.9% DD, positions were dust
    (20,  0.40),   # was 0.25 — significant but not crippling
    (30,  0.15),   # was 0.10 — survival mode
]

MAX_CONCURRENT_CURVE = [
    (100,    5),
    (250,    6),
    (500,    7),
    (1000,   8),
    (5000,   10),
    (10000,  12),
    (50000,  15),
]

# ==================================================================
#  PAIR HUNTER
# ==================================================================

HUNTER_ENABLED = True
HUNTER_SCAN_INTERVAL = 300       # 5 min between full universe scans
HUNTER_MIN_VOL_24H = 3_000_000
HUNTER_MAX_SPREAD_PCT = 0.15
HUNTER_MAX_RESULTS = 10

# Hunter → Trading (take signals, scale risk by conviction)
HUNTER_TRADE_ENABLED = True      # actually trade hunter signals
HUNTER_MAX_POSITIONS = 5         # up to 5 hunter scalp positions
HUNTER_RISK_MULT = 0.50          # 50% risk vs portfolio
HUNTER_MIN_GRADE = "B"           # B grade minimum for hunter scalps
# Per-strategy exit modes for hunter signals (shadow sim validated)
# Key: (strategy, timeframe) → exit_mode
# trail_tight = activate at 1R, trail 0.3R behind peak
# Shadow simulation: current -264.8R → routed +490.7R (+755.6R improvement)
HUNTER_EXIT_MAP = {
    # 15m strategies — nearly all best with trail_tight
    ("EMA_RIB", "15m"):  "trl_tight",  # was fix2.0 → -67.7R → +133.8R (+201R swing)
    ("BB_BREAK", "15m"): "trl_tight",  # was fix3.0 → +29.1R → +71.0R (+42R swing)
    ("BB_FADE", "15m"):  "trl_tight",  # was trl    → +35.3R → +74.2R (+39R swing)
    ("IB_BREAK", "15m"): "trl_tight",  # was fix2.0 → +0.7R  → +50.9R (+50R swing)
    ("DONCHIAN", "15m"): "trl_tight",  # was fix1.5 → +7.1R  → +39.3R (+32R swing)
    ("ENGULF", "15m"):   "trl_tight",  # was trl    → trail_tight captures more
    ("PIN_BAR", "15m"):  "trl_tight",  # was fix3.0 → tight trail better
    ("TR_PULL", "15m"):  "trl_tight",  # was fix1.5 → -41.5R → +25.7R (+67R swing)
    ("RSI_FADE", "15m"): "trl_tight",  # all 15m strategies tested better
    ("MOM_SURGE","15m"): "trl_tight",
    ("STOCH_X", "15m"):  "trl_tight",
    ("MTF_RSI", "15m"):  "trl_tight",
    # 30m strategies — trail_tight default
    ("EMA_RIB", "30m"):  "trl_tight",
    ("BB_BREAK", "30m"): "trl_tight",
    ("BB_FADE", "30m"):  "trl_tight",
    # 1h strategies — some better with wider exits
    ("BB_FADE", "1h"):   "fix3.0",    # 1h needs room: fix3.0 slightly better than trail_tight
    ("TR_PULL", "1h"):   "fix2.0",    # 1h slow grinder stays at fix2.0
    ("EMA_RIB", "1h"):   "trl_tight", # 1h still better with tight trail
    ("BB_BREAK", "1h"):  "trl_tight", # 1h strong moves trail well
    ("IB_BREAK", "1h"):  "trl_tight",
}
HUNTER_EXIT_DEFAULT = "trl_tight"    # fallback: trail_tight is universally best

# ==================================================================
#  WEBSOCKET
# ==================================================================

WS_RECONNECT_DELAY = 3          # seconds before reconnect attempt
WS_MAX_SUBS_PER_BATCH = 50      # max symbols per ws batch
WS_CANDLE_BUFFER = 220          # enough for SMA200 + warmup

# ==================================================================
#  HELPERS
# ==================================================================

def current_session_name(hour: int) -> str:
    for name, (start, end) in SESSIONS.items():
        if start <= hour < end:
            return name
    return "asia"

def get_risk_pct(equity: float) -> float:
    for threshold, risk in reversed(RISK_CURVE):
        if equity >= threshold:
            return risk
    return RISK_CURVE[0][1] if RISK_CURVE else RISK_PCT

def get_leverage(equity: float) -> int:
    for threshold, lev in reversed(LEVERAGE_CURVE):
        if equity >= threshold:
            return lev
    return LEVERAGE_CURVE[0][1] if LEVERAGE_CURVE else LEVERAGE

def get_current_phase(equity: float) -> Tuple[float, float, str]:
    for target, cap, label in GROWTH_PHASES:
        if equity < target:
            return (target, cap, label)
    last = GROWTH_PHASES[-1]
    return (last[0], last[1], f"Phase {len(GROWTH_PHASES)}: Maintenance")

def get_drawdown_multiplier(equity: float, peak_equity: float) -> float:
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

def get_max_concurrent(equity: float) -> int:
    for threshold, max_pos in reversed(MAX_CONCURRENT_CURVE):
        if equity >= threshold:
            return max_pos
    return MAX_CONCURRENT_CURVE[0][1] if MAX_CONCURRENT_CURVE else MAX_CONCURRENT_POSITIONS

def get_loss_streak_cooldown_hours(consecutive_losses: int) -> float:
    """Escalating cooldown: more losses = longer wait. Capped at MAX."""
    if consecutive_losses < PAIR_LOSS_COOLDOWN_COUNT:
        return 0.0
    escalation = consecutive_losses - PAIR_LOSS_COOLDOWN_COUNT + 1
    hours = PAIR_LOSS_COOLDOWN_HOURS * escalation
    return min(hours, PAIR_LOSS_COOLDOWN_MAX_HOURS)

def get_loss_streak_risk_mult(consecutive_losses: int) -> float:
    """Risk multiplier for repeat losers. Fewer losses = no change."""
    mult = 1.0
    for threshold, m in sorted(LOSS_STREAK_RISK_MULT.items()):
        if consecutive_losses >= threshold:
            mult = m
    return mult
