"""
live/edge_score.py -- Per-Trade Structural Oracle

ORACLE UPDATE (2026-02-21): skill_oracle.py across 17,611 trades on 163 pairs
discovered that each trade's STRUCTURAL FINGERPRINT predicts outcome quality.

TRAIL MODE (2026-02-21): Now using 1.5R TP + Guardian trail (no cap).
  Backtest trail: 50.3% WR, PF 1.28, avg_win=1.28R, avg_loss=0.77R
  Live 5m-only: 45.5% WR, payoff b=1.564, Kelly f*=10.6%

STRUCTURAL FEATURES (ranked by Spearman correlation with peak MFE):
  c2_engulfing      +0.315  C2 body engulfs FC body -> strong commitment
  fc_is_counter     +0.222  FC leans AGAINST breakout -> spring-loaded energy
  trend_aligned     +0.153  pre-FC trend matches breakout -> momentum continuation
  c3_hold_strength  -0.125  LESS follow-through = better (counterintuitive but valid)
  c2_momentum       +0.069  breakout distance past FC boundary -> conviction
  minutes_in        +0.047  later entries slightly better (more price discovery)
  fc_lower_wick_pct +0.036  lower rejection on FC -> buying interest
  c2_against_wick   +0.034  less resistance wick on C2 -> clean breakout

RISK SIZING BY TIER (trail mode — all tiers taken, risk scales with quality):
  S (top 20%):  risk_mult=1.0   Full Kelly — elite structural setup
  A (60-80th):  risk_mult=1.0   Full Kelly — high quality
  B (40-60th):  risk_mult=0.75  3/4 Kelly — standard quality
  C (20-40th):  risk_mult=0.60  Reduced — below average structure
  D (bottom):   risk_mult=0.50  Half Kelly — weak structure, still taken

No trades are skipped. Capital is concentrated on the best setups.
"""

from __future__ import annotations

# Oracle weights derived from 17,611-trade Spearman rank correlations
ORACLE_WEIGHTS = {
    "c2_engulfing":       0.3145,
    "fc_is_counter":      0.2220,
    "trend_aligned":      0.1526,
    "c3_hold_strength":  -0.1249,
    "c2_momentum":        0.0689,
    "minutes_in":         0.0469,
    "fc_lower_wick_pct":  0.0357,
    "c2_against_wick_pct": 0.0344,
}

# Normalization constants (from training set of 17,611 trades)
SCORE_MEAN = 1.2458
SCORE_STD  = 0.9830


def compute_quality_score(
    c2_body: float,        # abs(c2_close - c2_open)
    fc_body: float,        # abs(fc_close - fc_open)
    fc_is_counter: bool,   # FC body direction != breakout direction
    trend_aligned: bool,   # pre-FC 3-candle trend matches breakout
    c3_hold_strength: float,  # how far past FC level C3 closed (normalized by FC range)
    c2_momentum: float,    # (c2_close - FC boundary) / FC_range
    minutes_in: float,     # minutes from session start to entry
    fc_lower_wick_pct: float,  # FC lower wick / FC range
    c2_against_wick_pct: float,  # C2 wick against direction / C2 range
) -> float:
    """
    Compute composite quality z-score for a single FCB setup.
    Higher = better structural quality.

    Returns z-score (mean=0, std=1 relative to training distribution).
    """
    c2_engulfing = 1.0 if c2_body >= fc_body * 0.9 else 0.0

    raw = (
        ORACLE_WEIGHTS["c2_engulfing"] * c2_engulfing
        + ORACLE_WEIGHTS["fc_is_counter"] * (1.0 if fc_is_counter else 0.0)
        + ORACLE_WEIGHTS["trend_aligned"] * (1.0 if trend_aligned else 0.0)
        + ORACLE_WEIGHTS["c3_hold_strength"] * c3_hold_strength
        + ORACLE_WEIGHTS["c2_momentum"] * c2_momentum
        + ORACLE_WEIGHTS["minutes_in"] * minutes_in
        + ORACLE_WEIGHTS["fc_lower_wick_pct"] * fc_lower_wick_pct
        + ORACLE_WEIGHTS["c2_against_wick_pct"] * c2_against_wick_pct
    )

    z_score = (raw - SCORE_MEAN) / SCORE_STD if SCORE_STD > 0 else 0.0
    return z_score


def score_entry(
    direction: str,
    session: str,
    fc_range_pct: float,
    c2_body_ratio: float,
    fee_r: float,
    vol_ratio: float,
    slip_r: float,
    minutes_into_session: int,
    is_15m: bool = False,
    # Oracle extra features (populated when available)
    c2_body: float = 0.0,
    fc_body: float = 0.0,
    fc_is_counter: bool = False,
    trend_aligned: bool = False,
    c3_hold_strength: float = 0.0,
    c2_momentum: float = 0.0,
    fc_lower_wick_pct: float = 0.0,
    c2_against_wick_pct: float = 0.0,
) -> dict:
    """
    Per-trade structural quality scoring.

    All trades pass (TP=0.5R is positive for ALL tiers).
    Quality score used for diagnostics and optional risk sizing.
    """
    # Compute quality score if oracle features are available
    has_oracle_data = (c2_body > 0 or fc_body > 0)
    quality = 0.0
    tier = "N/A"

    if has_oracle_data:
        quality = compute_quality_score(
            c2_body=c2_body,
            fc_body=fc_body,
            fc_is_counter=fc_is_counter,
            trend_aligned=trend_aligned,
            c3_hold_strength=c3_hold_strength,
            c2_momentum=c2_momentum,
            minutes_in=float(minutes_into_session),
            fc_lower_wick_pct=fc_lower_wick_pct,
            c2_against_wick_pct=c2_against_wick_pct,
        )
        # Tier classification
        if quality >= 0.291:
            tier = "S_elite"
        elif quality >= -0.065:
            tier = "A_quality"
        elif quality >= -0.276:
            tier = "B_standard"
        elif quality >= -0.527:
            tier = "C_quick"
        else:
            tier = "D_low"

    # Risk multiplier by tier — concentrate capital on best setups
    # All tiers are taken (no skipping). Higher quality = more capital.
    TIER_RISK = {
        "S_elite":    1.0,   # Full Kelly — elite structural setup
        "A_quality":  1.0,   # Full Kelly — high quality
        "B_standard": 0.75,  # 3/4 Kelly — standard quality
        "C_quick":    0.60,  # Reduced — below average structure
        "D_low":      0.50,  # Half Kelly — weak but still taken
        "N/A":        0.75,  # No oracle data — conservative default
    }
    risk_mult = TIER_RISK.get(tier, 0.75)

    return {
        "confidence": 1.0,
        "score": 10,
        "action": "TAKE",
        "flip_direction": None,
        "risk_mult": risk_mult,
        "reasons": [f"oracle_tier={tier}", f"q={quality:+.2f}", f"risk={risk_mult:.0%}"],
        "flags": [],
        "quality_score": quality,
        "tier": tier,
    }


def should_block(result: dict) -> bool:
    """Never block. All tiers are positive at TP=0.5R."""
    return False


def risk_multiplier(result: dict) -> float:
    return result.get("risk_mult", 1.0)


def format_score(result: dict) -> str:
    tier = result.get("tier", "N/A")
    quality = result.get("quality_score", 0.0)
    risk_mult = result.get("risk_mult", 1.0)
    return f"ORACLE {tier} q={quality:+.2f} | risk={risk_mult:.0%} | TRAIL"
