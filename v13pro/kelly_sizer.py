"""Iteration 13: Kelly-based position sizing.

Replaces the 23-multiplier risk chain with clean Kelly sizing:
    edge_estimate → quarter-Kelly → real-time adjustments → risk_pct

Surviving real-time multipliers (NOT in shadow data):
    - Drawdown governor (equity state)
    - Hunter signal flag (signal origin)
    - Pair loss streak (pair state)
    - Burst engine (win streak momentum)
    - Momentum alignment (BTC/ETH/SOL real-time)
    - Pair lifecycle (per-pair health)
    - FlowThrottle (per-combo recent performance)

All other former multipliers (quality, regime, edge_combo, edge_sent,
kl_risk, of_risk, directional, session_lc, calibrator, cross_sect,
shadow_live, correlation, conv_mult, cross_tf) are now ABSORBED
into EdgeEstimator's data-driven prediction.
"""

from v13pro import config as cfg
from v13pro import logger as log

# ── Constants ──────────────────────────────────────────────
MIN_KELLY_RISK = 0.002      # 0.2% minimum meaningful position
MAX_KELLY_RISK = 0.04       # 4% absolute cap (2x static 2%)
PORTFOLIO_DECAY = 0.12      # Risk reduction per existing position
PORTFOLIO_FLOOR = 0.30      # Minimum multiplier when many positions open
STATIC_CEILING_MULT = 2.0   # Kelly can go up to 2x the static risk curve


class KellySizer:
    """Convert edge estimates to position sizes."""

    def size(self, edge: dict, equity: float, peak_equity: float,
             n_positions: int = 0,
             hunter: bool = False,
             pair_streak: int = 0,
             burst_mult: float = 1.0,
             alignment_mult: float = 1.0,
             lifecycle_mult: float = 1.0,
             flow_throttle_mult: float = 1.0) -> dict:
        """
        Compute position risk fraction.

        Args:
            edge: EdgeEstimator.estimate() output
            equity: current equity ($)
            peak_equity: peak equity ($) for DD calculation
            n_positions: number of currently open positions
            hunter: True if hunter signal (half risk)
            pair_streak: consecutive losses on this pair
            burst_mult: from BurstEngine (momentum scaling)
            alignment_mult: from MomentumAlignment
            lifecycle_mult: from LifecycleTracker (pair health)
            flow_throttle_mult: from FlowThrottle (combo recent perf)

        Returns:
            {risk_pct, dollar_risk, blocked, reason, components}
        """
        kelly_f = edge.get("kelly_f", 0.0)
        confidence = edge.get("confidence", 0.0)
        blocked = edge.get("blocked", True)

        if blocked or kelly_f <= 0:
            return {
                "risk_pct": 0.0,
                "dollar_risk": 0.0,
                "blocked": True,
                "reason": edge.get("reason", "negative edge"),
                "components": {"kelly_f": kelly_f, "confidence": confidence},
            }

        # ── Step 1: Base Kelly × confidence ──
        risk = kelly_f * confidence

        # ── Step 2: Drawdown governor ──
        dd_mult = cfg.get_drawdown_multiplier(equity, peak_equity)
        risk *= dd_mult

        # ── Step 3: Portfolio diversification ──
        if n_positions > 0:
            port_mult = max(PORTFOLIO_FLOOR,
                            1.0 - PORTFOLIO_DECAY * n_positions)
            risk *= port_mult
        else:
            port_mult = 1.0

        # ── Step 4: Surviving real-time multipliers ──
        hunter_mult = cfg.HUNTER_RISK_MULT if hunter else 1.0
        streak_mult = cfg.get_loss_streak_risk_mult(pair_streak)

        rt_mult = (hunter_mult * streak_mult * burst_mult
                   * alignment_mult * lifecycle_mult
                   * flow_throttle_mult)
        risk *= rt_mult

        # ── Step 5: Hard caps ──
        static_max = cfg.get_risk_pct(equity)
        upper = min(MAX_KELLY_RISK, static_max * STATIC_CEILING_MULT)
        risk = max(0.0, min(risk, upper))

        # ── Step 6: Minimum threshold ──
        dollar_risk = risk * equity
        # Gate on expected reward (risk × 2R min TP), not just risk amount
        expected_reward = dollar_risk * 2.0  # conservative 2R floor TP
        if risk < MIN_KELLY_RISK or expected_reward < cfg.MIN_REWARD_USD:
            return {
                "risk_pct": 0.0,
                "dollar_risk": 0.0,
                "blocked": True,
                "reason": (f"below minimum "
                           f"(risk={risk:.4f}/{MIN_KELLY_RISK:.4f}, "
                           f"reward=${expected_reward:.2f}/${cfg.MIN_REWARD_USD:.2f})"),
                "components": {
                    "kelly_f": kelly_f, "confidence": confidence,
                    "dd_mult": dd_mult, "port_mult": port_mult,
                    "rt_mult": rt_mult,
                },
            }

        reason = (f"¼K={kelly_f:.4f} conf={confidence:.2f} "
                  f"dd={dd_mult:.2f} port={port_mult:.2f} "
                  f"rt={rt_mult:.2f} → {risk:.4f}")

        return {
            "risk_pct": round(risk, 6),
            "dollar_risk": round(dollar_risk, 2),
            "blocked": False,
            "reason": reason,
            "components": {
                "kelly_f": kelly_f,
                "confidence": confidence,
                "dd_mult": dd_mult,
                "port_mult": port_mult,
                "hunter_mult": hunter_mult,
                "streak_mult": streak_mult,
                "burst_mult": burst_mult,
                "alignment_mult": alignment_mult,
                "lifecycle_mult": lifecycle_mult,
                "flow_throttle_mult": flow_throttle_mult,
                "rt_mult": rt_mult,
                "static_max": static_max,
                "upper_cap": upper,
            },
        }
