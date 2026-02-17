"""
live/config.py — Live trading configuration for FCB bot on Bybit.

STRATEGY PARAMETERS:
  - Exit = Guardian v3 trail (0.3R behind peak, activates at 1.0R) | exchange TP = 10R safety net
  - SL = range midpoint
  - Fee assumption = 0.02% maker
  - Min range = 0.3%
  - Max 1 trade per pair per session, 3 per pair per day
  - Risk = 2% per trade | 10x leverage

API keys are loaded from environment variables for security:
  BYBIT_API_KEY
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
TP_R            = 1.5       # take-profit in R-multiples
MIN_RANGE_PCT   = 0.003     # 0.3% minimum first-candle range
FEE_RATE        = 0.0002    # 0.02% maker fee
LEVERAGE        = 10        # 10x leverage (validated in backtest sweeps)
MAX_TRADES_SESSION = 1      # per pair per session
MAX_TRADES_DAY     = 3      # per pair per day
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
# DISABLED: markets too volatile for trailing — using fixed 1.5R TP instead
TRAIL_ENABLED        = False         # Set True to re-enable Guardian v3 trailing
TRAIL_ACTIVATION_R   = 1.0          # Start trailing once R >= 1.0
TRAIL_DISTANCE_R     = 0.3          # Trail 0.3R behind peak
EXCHANGE_TP_R        = 1.5           # Fixed 1.5R TP on exchange (clean profit lock)

# Progressive SL tiers: (trigger_R, new_SL_R, label)
# Exchange crash safety net — if bot dies, these SLs protect you.
PROFIT_TIERS = [
    (0.50,  -0.25, "T1: CUT LOSS 75%"),     # +0.5R  → SL -0.25R (max loss $5 instead of $20)
    (0.75,   0.00, "T2: BREAKEVEN"),         # +0.75R → SL at entry (NEVER a loser after this)
    (1.00,   0.50, "T3: LOCK +0.5R"),        # +1.0R  → SL +0.5R  ($10 guaranteed profit)
    (1.20,   0.80, "T4: LOCK +0.8R"),        # +1.2R  → SL +0.8R  ($16 guaranteed, near TP)
]

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

# ─── Risk Tiers ───
# DNA analysis (1,713 trades) showed Class B pairs perform identically to A.
# Flat 2% risk across all pairs — the FC edge is structural, not pair-dependent.
# A/B labels kept for tracking only; promote/demote still runs.
RISK_PCT_A       = 0.02     # 2% risk — all pairs
RISK_PCT_B       = 0.02     # 2% risk — flat (was 1%, equalised after DNA analysis)
SCALE_RISK_PCT_A = 0.01     # 1% scale-in — all pairs
SCALE_RISK_PCT_B = 0.01     # 1% scale-in — flat (was 0.5%, equalised)
RISK_PCT         = RISK_PCT_A   # default (backward compat)
SCALE_RISK_PCT   = SCALE_RISK_PCT_A

# ─── Promotion / Demotion Rules ───
PROMOTE_WINS     = 3        # consecutive wins to promote B → A
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

# ─── Backtest Baseline (1,713-trade validation set) ───
# From breakout_dna_discovery.py analysis.  Used by the startup report
# to compare live performance against what the strategy "should" do.
BACKTEST_WR             = 52.2      # win rate %
BACKTEST_EXPECTANCY_R   = 0.237     # R per trade
BACKTEST_PF             = 1.47      # profit factor
BACKTEST_AVG_WIN_R      = 1.418     # derived from WR + expectancy + PF
BACKTEST_AVG_LOSS_R     = 1.053     # derived
BACKTEST_TRADES_PER_DAY = 9.0       # estimated from 37-pair / 3-session setup
BACKTEST_START_EQUITY   = 1000.0    # starting capital

# ─── Operational ───
EQUITY_FLOOR    = 0         # disabled — trade with whatever equity we have

TIMEFRAME       = "5m"
POLL_INTERVAL   = 5         # seconds between checks within a candle
STATE_FILE      = "live/state.json"
LOG_DIR         = "live/logs"
TRADE_LOG       = "live/trades.csv"
