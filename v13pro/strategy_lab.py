"""
v13pro/strategy_lab.py  --  Strategy Laboratory for ORB & FCB

Shadow-only strategies need SPECIAL tracking that goes beyond normal
shadow data. We need to learn:

1. CONFIRMATION POWER — which confirmations actually predict real moves?
   Track each confirmation flag independently and measure its predictive value.

2. OPTIMAL SL — where should the stop be?
   Track SL placement vs peak excursion to find the sweet spot that
   maximises R while avoiding fake-outs.

3. SESSION/PAIR AFFINITY — which pairs on which sessions work best?
   Build a heatmap of strategy × pair × session performance.

4. LEVERAGE READINESS — can we x8-x10 this?
   Track the SL-as-percent-of-price. If SL is consistently <1%,
   the strategy is leverage-ready (x10+).

5. GRADUATION CRITERIA — when does a shadow strategy go live?
   Minimum N trades, minimum WR, minimum ExpR, minimum Sharpe,
   across multiple sessions — then it auto-graduates.

This module reads from shadow log data and produces periodic reports.
"""

import json
import os
import time
import threading
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from v13pro import config as cfg

log = logging.getLogger("v13pro")

# ── Config ─────────────────────────────────────────────────────────

# Strategies under lab observation
LAB_STRATEGIES = {"ORB", "FCB"}

# ORB is NY-session only
ORB_SESSIONS = {"ny"}

# Graduation thresholds
GRAD_MIN_TRADES = 50         # need ≥50 shadow outcomes
GRAD_MIN_WR = 0.52           # win rate ≥ 52%
GRAD_MIN_EXPR = 0.15         # expected R ≥ +0.15
GRAD_MIN_SESSIONS = 5        # seen in ≥5 distinct sessions
GRAD_MAX_DD_R = -3.0         # max drawdown in R-terms ≤ -3.0R

# Leverage readiness
LEV_READY_MAX_SL_PCT = 1.0   # SL is <1% of price → x10 viable
LEV_HIGH_MAX_SL_PCT = 0.5    # SL is <0.5% → x20 viable (on tested pairs)

# Confirmation flags we track per signal
CONFIRMATION_FLAGS = [
    "vol_spike",      # volume > threshold × vol_sma
    "ema_aligned",    # price on right side of EMA stack
    "rsi_agrees",     # RSI direction matches
    "body_strong",    # body/range ratio meets threshold
    "atr_expanding",  # ATR is expanding (vol expansion)
    "range_narrow",   # setup range was notably narrow
]

# State persistence
LAB_STATE_FILE = os.path.join(os.path.dirname(__file__), "lab_state.json")

# Report log
LAB_LOG_DIR = os.path.join(cfg.LOG_DIR, "lab")

# How often to recompute stats (seconds)
REFRESH_INTERVAL = 300  # 5 minutes


class StrategyLab:
    """
    Laboratory for shadow-only strategies (ORB, FCB).

    Records every signal with rich metadata, tracks outcomes,
    learns which confirmations predict winners, finds optimal SL,
    and determines when a strategy is ready for live trading.
    """

    def __init__(self):
        self._lock = threading.RLock()

        # Per-strategy outcomes: strategy → list of outcome dicts
        self._outcomes: Dict[str, deque] = {
            s: deque(maxlen=500) for s in LAB_STRATEGIES
        }

        # Per-strategy × session × pair stats
        self._heatmap: Dict[str, Dict[str, Dict[str, dict]]] = {
            s: defaultdict(lambda: defaultdict(lambda: {
                "n": 0, "wins": 0, "pnl_r": 0.0, "peak_r": 0.0
            })) for s in LAB_STRATEGIES
        }

        # Confirmation effectiveness: strategy → flag → {total, wins}
        self._confirmations: Dict[str, Dict[str, dict]] = {
            s: {f: {"total": 0, "wins": 0, "pnl_sum": 0.0}
                for f in CONFIRMATION_FLAGS}
            for s in LAB_STRATEGIES
        }

        # SL analysis: strategy → list of {sl_pct, peak_r, outcome_r, hit_sl_first}
        self._sl_data: Dict[str, deque] = {
            s: deque(maxlen=500) for s in LAB_STRATEGIES
        }

        # Graduation status
        self._graduated: Dict[str, bool] = {s: False for s in LAB_STRATEGIES}
        self._grad_details: Dict[str, dict] = {}

        # Overall stats (cached)
        self._stats: Dict[str, dict] = {}
        self._last_refresh = 0.0

        # Load persisted state
        self._load_state()

    # ── Public API ─────────────────────────────────────────────

    def record_outcome(self, strategy: str, symbol: str, side: str,
                       tf: str, session: str, entry_price: float,
                       stop_dist: float, peak_r: float, trough_r: float,
                       outcome_r: float, hit_tp: bool, hit_sl: bool,
                       confirmations: Dict[str, bool] = None,
                       duration_min: float = 0.0):
        """
        Record a completed shadow outcome for a lab strategy.

        Called by shadow.py when a tracked ORB/FCB signal completes.
        """
        if strategy not in LAB_STRATEGIES:
            return

        with self._lock:
            win = outcome_r > 0

            # Store full outcome
            record = {
                "ts": time.time(),
                "symbol": symbol,
                "side": side,
                "tf": tf,
                "session": session,
                "entry_price": entry_price,
                "stop_dist": stop_dist,
                "sl_pct": (stop_dist / entry_price * 100) if entry_price > 0 else 0,
                "peak_r": peak_r,
                "trough_r": trough_r,
                "outcome_r": outcome_r,
                "hit_tp": hit_tp,
                "hit_sl": hit_sl,
                "win": win,
                "confirmations": confirmations or {},
                "duration_min": duration_min,
            }
            self._outcomes[strategy].append(record)

            # Update heatmap
            hm = self._heatmap[strategy][session][symbol]
            hm["n"] += 1
            if win:
                hm["wins"] += 1
            hm["pnl_r"] += outcome_r
            hm["peak_r"] = max(hm["peak_r"], peak_r)

            # Update confirmation effectiveness
            if confirmations:
                for flag, was_set in confirmations.items():
                    if flag in self._confirmations[strategy] and was_set:
                        cf = self._confirmations[strategy][flag]
                        cf["total"] += 1
                        if win:
                            cf["wins"] += 1
                        cf["pnl_sum"] += outcome_r

            # SL analysis
            self._sl_data[strategy].append({
                "sl_pct": record["sl_pct"],
                "peak_r": peak_r,
                "outcome_r": outcome_r,
                "hit_sl": hit_sl,
            })

            # Persist
            self._save_state()

    def is_graduated(self, strategy: str) -> bool:
        """Check if a strategy has graduated from shadow to live-ready."""
        return self._graduated.get(strategy, False)

    def graduation_report(self, strategy: str) -> dict:
        """Get detailed graduation analysis for a strategy."""
        return self._grad_details.get(strategy, {})

    def get_stats(self, strategy: str) -> dict:
        """Get current stats for a lab strategy."""
        now = time.time()
        if now - self._last_refresh > REFRESH_INTERVAL:
            self._refresh_stats()
        return self._stats.get(strategy, {})

    def leverage_recommendation(self, strategy: str) -> dict:
        """
        Recommend leverage based on SL tightness.

        Returns dict with:
          - max_safe_lev: recommended max leverage
          - avg_sl_pct: average SL as % of price
          - median_sl_pct: median SL as %
          - pct_under_1: % of trades with SL < 1%
          - verdict: "x10_ready", "x8_ready", "standard", "not_enough_data"
        """
        with self._lock:
            data = list(self._sl_data.get(strategy, []))

        if len(data) < 10:
            return {"verdict": "not_enough_data", "n": len(data)}

        sl_pcts = [d["sl_pct"] for d in data]
        avg_sl = sum(sl_pcts) / len(sl_pcts)
        sorted_sl = sorted(sl_pcts)
        median_sl = sorted_sl[len(sorted_sl) // 2]
        pct_under_1 = sum(1 for s in sl_pcts if s < 1.0) / len(sl_pcts) * 100
        pct_under_05 = sum(1 for s in sl_pcts if s < 0.5) / len(sl_pcts) * 100

        if median_sl < LEV_HIGH_MAX_SL_PCT and pct_under_05 > 70:
            verdict = "x20_ready"
            max_lev = 20
        elif median_sl < LEV_READY_MAX_SL_PCT and pct_under_1 > 70:
            verdict = "x10_ready"
            max_lev = 10
        elif avg_sl < 2.0:
            verdict = "x8_ready"
            max_lev = 8
        else:
            verdict = "standard"
            max_lev = cfg.LEVERAGE

        return {
            "verdict": verdict,
            "max_safe_lev": max_lev,
            "avg_sl_pct": round(avg_sl, 3),
            "median_sl_pct": round(median_sl, 3),
            "pct_under_1pct": round(pct_under_1, 1),
            "pct_under_05pct": round(pct_under_05, 1),
            "n": len(data),
        }

    def confirmation_power(self, strategy: str) -> List[dict]:
        """
        Rank confirmations by predictive power.

        Returns list of {flag, total, wins, wr, avg_r, power_score}
        sorted by power_score descending.
        """
        with self._lock:
            cfm = dict(self._confirmations.get(strategy, {}))

        results = []
        for flag, data in cfm.items():
            n = data["total"]
            if n < 3:
                results.append({
                    "flag": flag, "total": n, "wins": 0,
                    "wr": 0, "avg_r": 0, "power_score": 0,
                })
                continue
            wr = data["wins"] / n
            avg_r = data["pnl_sum"] / n
            # Power score: combination of WR and avg_r (higher = better predictor)
            power = wr * 0.5 + max(0, avg_r) * 0.5
            results.append({
                "flag": flag,
                "total": n,
                "wins": data["wins"],
                "wr": round(wr * 100, 1),
                "avg_r": round(avg_r, 3),
                "power_score": round(power, 3),
            })

        return sorted(results, key=lambda x: x["power_score"], reverse=True)

    def summary(self) -> dict:
        """Dashboard-friendly summary of all lab strategies."""
        now = time.time()
        if now - self._last_refresh > REFRESH_INTERVAL:
            self._refresh_stats()

        result = {}
        for strat in LAB_STRATEGIES:
            st = self._stats.get(strat, {})
            lev = self.leverage_recommendation(strat)
            result[strat] = {
                "n": st.get("n", 0),
                "wr": st.get("wr", 0),
                "expr": st.get("expr", 0),
                "peak_r": st.get("best_peak_r", 0),
                "sessions_seen": st.get("sessions_seen", 0),
                "graduated": self._graduated.get(strat, False),
                "lev_verdict": lev.get("verdict", "not_enough_data"),
                "lev_rec": lev.get("max_safe_lev", cfg.LEVERAGE),
                "top_confirm": "",
            }
            # Top confirmation
            cp = self.confirmation_power(strat)
            if cp and cp[0]["total"] >= 3:
                result[strat]["top_confirm"] = f"{cp[0]['flag']}({cp[0]['wr']:.0f}%)"

        return result

    # ── Internal ───────────────────────────────────────────────

    def _refresh_stats(self):
        """Recompute stats for all lab strategies."""
        with self._lock:
            for strat in LAB_STRATEGIES:
                outcomes = list(self._outcomes[strat])
                n = len(outcomes)
                if n == 0:
                    self._stats[strat] = {"n": 0, "wr": 0, "expr": 0}
                    continue

                wins = sum(1 for o in outcomes if o["win"])
                wr = wins / n
                total_r = sum(o["outcome_r"] for o in outcomes)
                expr = total_r / n
                best_peak = max((o["peak_r"] for o in outcomes), default=0)

                # Sessions seen
                sessions_seen = len(set(
                    f"{o['session']}_{datetime.fromtimestamp(o['ts'], tz=timezone.utc).strftime('%Y%m%d')}"
                    for o in outcomes
                ))

                # Max drawdown in R
                running_r = 0.0
                peak_running = 0.0
                max_dd = 0.0
                for o in outcomes:
                    running_r += o["outcome_r"]
                    peak_running = max(peak_running, running_r)
                    dd = running_r - peak_running
                    max_dd = min(max_dd, dd)

                stats = {
                    "n": n,
                    "wins": wins,
                    "wr": round(wr * 100, 1),
                    "expr": round(expr, 3),
                    "total_r": round(total_r, 2),
                    "best_peak_r": round(best_peak, 2),
                    "sessions_seen": sessions_seen,
                    "max_dd_r": round(max_dd, 2),
                }
                self._stats[strat] = stats

                # Check graduation
                graduated = (
                    n >= GRAD_MIN_TRADES and
                    wr >= GRAD_MIN_WR and
                    expr >= GRAD_MIN_EXPR and
                    sessions_seen >= GRAD_MIN_SESSIONS and
                    max_dd >= GRAD_MAX_DD_R  # max_dd is negative, so >= means less bad
                )

                self._graduated[strat] = graduated
                self._grad_details[strat] = {
                    "graduated": graduated,
                    "checks": {
                        f"trades≥{GRAD_MIN_TRADES}": n >= GRAD_MIN_TRADES,
                        f"wr≥{GRAD_MIN_WR*100:.0f}%": wr >= GRAD_MIN_WR,
                        f"expr≥{GRAD_MIN_EXPR}": expr >= GRAD_MIN_EXPR,
                        f"sessions≥{GRAD_MIN_SESSIONS}": sessions_seen >= GRAD_MIN_SESSIONS,
                        f"dd≥{GRAD_MAX_DD_R}R": max_dd >= GRAD_MAX_DD_R,
                    },
                    "stats": stats,
                }

            self._last_refresh = time.time()

    def _save_state(self):
        """Persist lab state to disk."""
        try:
            state = {
                "outcomes": {s: list(d) for s, d in self._outcomes.items()},
                "confirmations": dict(self._confirmations),
                "sl_data": {s: list(d) for s, d in self._sl_data.items()},
                "graduated": dict(self._graduated),
            }
            tmp = LAB_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, default=str)
            os.replace(tmp, LAB_STATE_FILE)
        except Exception as e:
            log.debug(f"Lab state save error: {e}")

    def _load_state(self):
        """Load persisted lab state."""
        if not os.path.exists(LAB_STATE_FILE):
            return
        try:
            with open(LAB_STATE_FILE) as f:
                state = json.load(f)

            for strat, outcomes in state.get("outcomes", {}).items():
                if strat in self._outcomes:
                    self._outcomes[strat] = deque(outcomes, maxlen=500)

            for strat, cfm in state.get("confirmations", {}).items():
                if strat in self._confirmations:
                    self._confirmations[strat] = cfm

            for strat, sl in state.get("sl_data", {}).items():
                if strat in self._sl_data:
                    self._sl_data[strat] = deque(sl, maxlen=500)

            self._graduated = state.get("graduated", {s: False for s in LAB_STRATEGIES})

            log.info(f"Strategy lab loaded: {', '.join(f'{s}={len(self._outcomes[s])}' for s in LAB_STRATEGIES)}")
        except Exception as e:
            log.debug(f"Lab state load error: {e}")

    def write_report(self):
        """Write detailed lab report to file."""
        os.makedirs(LAB_LOG_DIR, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        path = os.path.join(LAB_LOG_DIR, f"lab_report_{ts}.txt")

        self._refresh_stats()
        lines = [
            f"╔{'═'*60}╗",
            f"║  STRATEGY LAB REPORT  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"╚{'═'*60}╝",
            "",
        ]

        for strat in LAB_STRATEGIES:
            st = self._stats.get(strat, {})
            grad = self._grad_details.get(strat, {})
            lev = self.leverage_recommendation(strat)

            lines.append(f"──── {strat} ────")
            lines.append(f"  Trades: {st.get('n', 0)}  WR: {st.get('wr', 0)}%  ExpR: {st.get('expr', 0)}")
            lines.append(f"  Total R: {st.get('total_r', 0)}  Best Peak: {st.get('best_peak_r', 0)}R")
            lines.append(f"  Sessions: {st.get('sessions_seen', 0)}  Max DD: {st.get('max_dd_r', 0)}R")
            lines.append(f"  Graduated: {'YES' if self._graduated.get(strat) else 'NO'}")
            lines.append("")

            # Graduation checklist
            if grad:
                lines.append("  Graduation Checklist:")
                for check, passed in grad.get("checks", {}).items():
                    mark = "✓" if passed else "✗"
                    lines.append(f"    {mark} {check}")
                lines.append("")

            # Leverage analysis
            lines.append(f"  Leverage: {lev.get('verdict', '?')} (max {lev.get('max_safe_lev', '?')}x)")
            lines.append(f"    Avg SL: {lev.get('avg_sl_pct', '?')}%  "
                         f"Median: {lev.get('median_sl_pct', '?')}%  "
                         f"<1%: {lev.get('pct_under_1pct', '?')}%  "
                         f"<0.5%: {lev.get('pct_under_05pct', '?')}%")
            lines.append("")

            # Confirmation power
            cp = self.confirmation_power(strat)
            if cp:
                lines.append("  Confirmation Power (best → worst):")
                for cf in cp:
                    if cf["total"] >= 3:
                        lines.append(f"    {cf['flag']:20s}  {cf['total']:3d} signals  "
                                     f"WR={cf['wr']:5.1f}%  avgR={cf['avg_r']:+.3f}  "
                                     f"power={cf['power_score']:.3f}")
                    else:
                        lines.append(f"    {cf['flag']:20s}  {cf['total']:3d} signals  (insufficient data)")
                lines.append("")

            # Top pairs
            with self._lock:
                hm = self._heatmap.get(strat, {})
            pair_stats = []
            for sess, pairs in hm.items():
                for pair, ps in pairs.items():
                    if ps["n"] >= 3:
                        pair_wr = ps["wins"] / ps["n"] * 100
                        pair_expr = ps["pnl_r"] / ps["n"]
                        pair_stats.append((pair, sess, ps["n"], pair_wr, pair_expr))

            if pair_stats:
                pair_stats.sort(key=lambda x: x[4], reverse=True)
                lines.append("  Top Pairs (≥3 trades):")
                for p, s, n, wr, expr in pair_stats[:10]:
                    lines.append(f"    {p:16s} {s:7s}  N={n:3d}  WR={wr:5.1f}%  ExpR={expr:+.3f}")
                lines.append("")

            lines.append("")

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            log.info(f"Lab report written: {path}")
        except Exception as e:
            log.debug(f"Lab report write error: {e}")
