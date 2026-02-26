"""
v13pro/dna.py -- Setup DNA Profiler: Statistical edge discovery via pandas.

Captures RAW numeric indicator values at every trade entry.
After enough trades, uses pandas to find the exact indicator RANGES
that separate winners from losers. When a new signal's indicators
fall inside a "winning range" cluster, conviction gets boosted.

This is NOT about categorical buckets ("up/down") -- it's about
finding: "When EMA8_slope is 0.3-0.8 AND RSI is 45-65 AND
vol_ratio > 1.5, win rate jumps from 48% to 72%."

The profiler stores ~25 raw numeric features per trade, then
runs correlation analysis to find repeatable winning conditions.
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from v13pro import config as cfg
from v13pro import logger as log
from v13pro.indicators import ema, sma, atr, rsi, bollinger_bands, stochastic

# ===================================================================
#  CONFIG
# ===================================================================

DNA_CSV = os.path.join(cfg.LOG_DIR, "setup_dna.csv")
EDGES_FILE = os.path.join(cfg.LOG_DIR, "dna_edges.json")
MIN_TRADES_FOR_ANALYSIS = 15       # need 15+ outcomes to start
EDGE_MIN_SAMPLE = 5                # need 5+ trades in a range bucket
EDGE_MIN_WR_LIFT = 0.10            # need 10%+ win rate lift over baseline
RECALC_EVERY = 5                   # recalc edges every N new outcomes
MAX_CONVICTION_BOOST = 12          # cap total indicator-driven boost


# ===================================================================
#  FEATURE EXTRACTION -- Raw numeric values
# ===================================================================

def extract_features(candles: list, direction: str,
                     entry_price: float, stop_dist: float) -> Dict:
    """Extract ~25 raw numeric features from candle history at entry time.

    All values are plain floats -- no categories, no buckets.
    The statistical engine later finds which ranges matter.
    """
    if not candles or len(candles) < 20:
        return {}

    c = np.array([x["close"] for x in candles], dtype=float)
    h = np.array([x["high"] for x in candles], dtype=float)
    lo = np.array([x["low"] for x in candles], dtype=float)
    o = np.array([x["open"] for x in candles], dtype=float)
    v = np.array([x["volume"] for x in candles], dtype=float)
    n = len(c)
    last = n - 1

    feat = {}

    # -- EMA structure --
    e8 = ema(c, 8)
    e21 = ema(c, 21)
    e55 = ema(c, min(55, n - 1)) if n > 55 else ema(c, max(8, n // 3))

    if not np.isnan(e8[last]):
        # EMA8 slope (% change over 5 bars)
        if last >= 5 and not np.isnan(e8[last - 5]) and e8[last - 5] > 0:
            feat["ema8_slope"] = round((e8[last] - e8[last - 5]) / e8[last - 5] * 100, 4)
        # EMA21 slope
        if last >= 5 and not np.isnan(e21[last - 5]) and e21[last - 5] > 0:
            feat["ema21_slope"] = round((e21[last] - e21[last - 5]) / e21[last - 5] * 100, 4)
        # Price distance from EMA8 (%)
        if e8[last] > 0:
            feat["price_vs_ema8"] = round((c[last] - e8[last]) / e8[last] * 100, 4)
        # Price distance from EMA21 (%)
        if not np.isnan(e21[last]) and e21[last] > 0:
            feat["price_vs_ema21"] = round((c[last] - e21[last]) / e21[last] * 100, 4)
        # EMA8 vs EMA21 spread (%)
        if not np.isnan(e21[last]) and e21[last] > 0:
            feat["ema8_21_spread"] = round((e8[last] - e21[last]) / e21[last] * 100, 4)
        # EMA ribbon width (e8-e55 spread %)
        if not np.isnan(e55[last]) and e55[last] > 0:
            feat["ema_ribbon_width"] = round((e8[last] - e55[last]) / e55[last] * 100, 4)

    # -- Volatility (ATR) --
    a = atr(h, lo, c, 14)
    if not np.isnan(a[last]) and a[last] > 0:
        # ATR as % of price
        feat["atr_pct"] = round(a[last] / c[last] * 100, 4)
        # ATR expansion ratio (current / 20-bar avg)
        atr_20 = a[max(0, last - 20):last + 1]
        atr_20_clean = atr_20[~np.isnan(atr_20)]
        if len(atr_20_clean) > 3:
            feat["atr_expansion"] = round(float(a[last] / np.mean(atr_20_clean)), 4)
        # Stop distance as multiple of ATR
        if stop_dist > 0:
            feat["stop_atr_ratio"] = round(stop_dist / a[last], 4)

    # -- RSI --
    r = rsi(c, 14)
    if not np.isnan(r[last]):
        feat["rsi"] = round(float(r[last]), 2)
        # RSI rate of change (current - 5 bars ago)
        if last >= 5 and not np.isnan(r[last - 5]):
            feat["rsi_roc"] = round(float(r[last] - r[last - 5]), 2)

    # -- Bollinger Bands --
    upper, mid, lower = bollinger_bands(c, 20, 2.0)
    if not np.isnan(upper[last]) and not np.isnan(lower[last]):
        bb_range = upper[last] - lower[last]
        if bb_range > 0:
            # Position within BB (0 = lower band, 1 = upper band)
            feat["bb_position"] = round(float((c[last] - lower[last]) / bb_range), 4)
            # BB width as % of price (volatility measure)
            feat["bb_width_pct"] = round(float(bb_range / c[last] * 100), 4)

    # -- Stochastic --
    sk, sd = stochastic(h, lo, c)
    if not np.isnan(sk[last]):
        feat["stoch_k"] = round(float(sk[last]), 2)
    if not np.isnan(sd[last]):
        feat["stoch_d"] = round(float(sd[last]), 2)

    # -- Volume --
    v_avg = sma(v, 20)
    if not np.isnan(v_avg[last]) and v_avg[last] > 0:
        feat["vol_ratio"] = round(float(v[last] / v_avg[last]), 4)
    if len(v) >= 5:
        # Volume trend (5-bar avg vs 20-bar avg)
        v5 = float(np.mean(v[last - 4:last + 1]))
        if not np.isnan(v_avg[last]) and v_avg[last] > 0:
            feat["vol_trend"] = round(v5 / float(v_avg[last]), 4)

    # -- Candle structure --
    body = c[last] - o[last]
    full_range = h[last] - lo[last]
    if full_range > 0:
        feat["body_ratio"] = round(float(abs(body) / full_range), 4)
        # Upper wick ratio
        upper_wick = h[last] - max(o[last], c[last])
        feat["upper_wick_ratio"] = round(float(upper_wick / full_range), 4)
        # Lower wick ratio
        lower_wick = min(o[last], c[last]) - lo[last]
        feat["lower_wick_ratio"] = round(float(lower_wick / full_range), 4)
        # Bullish/bearish body direction (1 = bull, -1 = bear)
        feat["body_direction"] = 1.0 if body > 0 else (-1.0 if body < 0 else 0.0)

    # -- Price action context --
    if n >= 5:
        # 5-bar range as % of price
        range_5 = float(np.max(h[last - 4:last + 1]) - np.min(lo[last - 4:last + 1]))
        feat["range_5bar_pct"] = round(range_5 / c[last] * 100, 4)
    if n >= 20:
        # 20-bar range
        range_20 = float(np.max(h[last - 19:last + 1]) - np.min(lo[last - 19:last + 1]))
        feat["range_20bar_pct"] = round(range_20 / c[last] * 100, 4)
        # Where in the 20-bar range (0 = low, 1 = high)
        high_20 = float(np.max(h[last - 19:last + 1]))
        low_20 = float(np.min(lo[last - 19:last + 1]))
        if high_20 > low_20:
            feat["position_in_range"] = round((c[last] - low_20) / (high_20 - low_20), 4)

    # -- Momentum --
    if n >= 10:
        feat["momentum_10"] = round(float((c[last] - c[last - 9]) / c[last - 9] * 100), 4)
    if n >= 20:
        feat["momentum_20"] = round(float((c[last] - c[last - 19]) / c[last - 19] * 100), 4)

    # -- Direction alignment score --
    # How many of the EMA/momentum features favor the trade direction
    dir_mult = 1.0 if direction == "long" else -1.0
    alignment_score = 0.0
    if "ema8_slope" in feat:
        alignment_score += 1 if feat["ema8_slope"] * dir_mult > 0 else -1
    if "momentum_10" in feat:
        alignment_score += 1 if feat["momentum_10"] * dir_mult > 0 else -1
    if "rsi" in feat:
        if direction == "long":
            alignment_score += 1 if feat["rsi"] < 70 else -1
        else:
            alignment_score += 1 if feat["rsi"] > 30 else -1
    feat["direction_alignment"] = alignment_score

    return feat


# ===================================================================
#  SETUP DNA PROFILER (LIVE ENGINE)
# ===================================================================

class SetupDNA:
    """Statistical setup profiler.

    Records raw indicator values per trade, uses pandas to find
    which indicator ranges correlate with winning. In real-time,
    boosts conviction when new signals match proven winning ranges.
    """

    def __init__(self):
        self._df: Optional[pd.DataFrame] = None
        self._edges: List[Dict] = []           # discovered winning edges
        self._outcomes_since_recalc = 0
        self._baseline_wr = 0.5                # updated from data
        self._load()

    def _load(self):
        """Load trade DNA history from CSV + discovered edges from JSON."""
        try:
            if os.path.exists(DNA_CSV):
                self._df = pd.read_csv(DNA_CSV)
                log.info(f"  DNA Profiler: loaded {len(self._df)} records")
            else:
                self._df = pd.DataFrame()

            if os.path.exists(EDGES_FILE):
                with open(EDGES_FILE) as f:
                    data = json.load(f)
                self._edges = data.get("edges", [])
                self._baseline_wr = data.get("baseline_wr", 0.5)
                if self._edges:
                    log.info(f"  DNA Profiler: {len(self._edges)} proven edges loaded")
        except Exception as e:
            log.debug(f"DNA profiler load error: {e}")
            self._df = pd.DataFrame()

    def _save_csv(self):
        """Save full DataFrame to CSV."""
        try:
            os.makedirs(os.path.dirname(DNA_CSV), exist_ok=True)
            if self._df is not None and len(self._df) > 0:
                self._df.to_csv(DNA_CSV, index=False)
        except Exception as e:
            log.debug(f"DNA CSV save error: {e}")

    def _save_edges(self):
        """Save discovered edges to JSON."""
        try:
            os.makedirs(os.path.dirname(EDGES_FILE), exist_ok=True)
            data = {
                "edges": self._edges,
                "baseline_wr": self._baseline_wr,
                "updated": datetime.now(timezone.utc).isoformat(),
                "total_trades": len(self._df) if self._df is not None else 0,
            }
            with open(EDGES_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.debug(f"DNA edges save error: {e}")

    # -- RECORD ENTRY -----------------------------------------------

    def record_entry(self, symbol: str, strategy: str, tf: str,
                     side: str, entry_price: float, stop_dist: float,
                     conviction: float, grade: str, features: Dict,
                     source: str = "portfolio"):
        """Store a trade's indicator snapshot at entry time."""
        row = {
            "symbol": symbol,
            "strategy": strategy,
            "tf": tf,
            "side": side,
            "entry_price": round(float(entry_price), 6),
            "stop_dist": round(float(stop_dist), 6),
            "conviction": round(float(conviction), 1),
            "grade": grade,
            "source": source,
            "entry_ts": datetime.now(timezone.utc).isoformat(),
            "outcome": "",       # filled on exit
            "pnl_r": 0.0,
            "exit_ts": "",
        }
        # Flatten features into columns (all numeric)
        for k, val in features.items():
            row[k] = float(val) if val is not None else float("nan")

        new_row = pd.DataFrame([row])
        if self._df is None or len(self._df) == 0:
            self._df = new_row
        else:
            self._df = pd.concat([self._df, new_row], ignore_index=True)
        self._save_csv()

    # -- RECORD OUTCOME ---------------------------------------------

    def record_outcome(self, symbol: str, pnl_r: float, win: bool):
        """Record result for the most recent open trade on symbol."""
        if self._df is None or len(self._df) == 0:
            return

        # Find the last entry for this symbol with no outcome
        mask = (self._df["symbol"] == symbol) & (self._df["outcome"] == "")
        if not mask.any():
            return

        idx = mask[mask].index[-1]
        self._df.at[idx, "outcome"] = "win" if win else "loss"
        self._df.at[idx, "pnl_r"] = round(float(pnl_r), 4)
        self._df.at[idx, "exit_ts"] = datetime.now(timezone.utc).isoformat()

        self._save_csv()

        self._outcomes_since_recalc += 1
        if self._outcomes_since_recalc >= RECALC_EVERY:
            self._discover_edges()
            self._outcomes_since_recalc = 0

    # -- EDGE DISCOVERY (the core statistical engine) ---------------

    def _discover_edges(self):
        """Use pandas to find indicator ranges that separate W from L.

        For each numeric feature, split into quantile bins and check
        if any bin has significantly higher win rate than baseline.
        """
        if self._df is None or len(self._df) == 0:
            return

        df = self._df[self._df["outcome"].isin(["win", "loss"])].copy()
        if len(df) < MIN_TRADES_FOR_ANALYSIS:
            return

        df["win"] = (df["outcome"] == "win").astype(int)
        baseline_wr = float(df["win"].mean())
        self._baseline_wr = round(baseline_wr, 4)

        # Feature columns = numeric columns that are not metadata
        meta_cols = {"symbol", "strategy", "tf", "side", "entry_price",
                     "stop_dist", "conviction", "grade", "source",
                     "entry_ts", "outcome", "pnl_r", "exit_ts", "win"}
        feat_cols = [c for c in df.columns
                     if c not in meta_cols
                     and pd.api.types.is_numeric_dtype(df[c])]

        edges = []

        for col in feat_cols:
            valid = df[col].dropna()
            if len(valid) < MIN_TRADES_FOR_ANALYSIS:
                continue

            # Try 3 quantile bins (low/mid/high)
            try:
                bins = pd.qcut(df[col].dropna(), q=3, duplicates="drop")
            except (ValueError, TypeError):
                continue

            joined = df.loc[bins.index].copy()
            joined["bin"] = bins

            for bin_label, group in joined.groupby("bin", observed=True):
                if len(group) < EDGE_MIN_SAMPLE:
                    continue

                wr = float(group["win"].mean())
                lift = wr - baseline_wr
                n_trades = len(group)
                avg_r = float(group["pnl_r"].mean())

                if lift >= EDGE_MIN_WR_LIFT:
                    edges.append({
                        "feature": col,
                        "range_low": round(float(bin_label.left), 6),
                        "range_high": round(float(bin_label.right), 6),
                        "win_rate": round(wr, 4),
                        "lift": round(lift, 4),
                        "avg_r": round(avg_r, 4),
                        "sample_size": int(n_trades),
                        "wins": int(group["win"].sum()),
                        "losses": int(n_trades - int(group["win"].sum())),
                    })

        # Also check per-strategy edges
        for strat, strat_group in df.groupby("strategy"):
            if len(strat_group) < EDGE_MIN_SAMPLE:
                continue
            for col in feat_cols:
                valid = strat_group[col].dropna()
                if len(valid) < EDGE_MIN_SAMPLE:
                    continue
                try:
                    bins = pd.qcut(strat_group[col].dropna(), q=2,
                                   duplicates="drop")
                except (ValueError, TypeError):
                    continue
                joined = strat_group.loc[bins.index].copy()
                joined["bin"] = bins
                for bin_label, group in joined.groupby("bin", observed=True):
                    if len(group) < EDGE_MIN_SAMPLE:
                        continue
                    wr = float(group["win"].mean())
                    lift = wr - baseline_wr
                    if lift >= EDGE_MIN_WR_LIFT:
                        edges.append({
                            "feature": col,
                            "strategy": str(strat),
                            "range_low": round(float(bin_label.left), 6),
                            "range_high": round(float(bin_label.right), 6),
                            "win_rate": round(wr, 4),
                            "lift": round(lift, 4),
                            "avg_r": round(float(group["pnl_r"].mean()), 4),
                            "sample_size": int(len(group)),
                            "wins": int(group["win"].sum()),
                            "losses": int(len(group) - int(group["win"].sum())),
                        })

        # Sort by lift x sample_size (most impactful edges first)
        edges.sort(key=lambda e: e["lift"] * e["sample_size"], reverse=True)
        self._edges = edges[:30]  # keep top 30

        self._save_edges()

        if edges:
            best = edges[0]
            log.info(
                f"  DNA: {len(edges)} edges found | "
                f"best: {best['feature']} [{best['range_low']:.3f} to "
                f"{best['range_high']:.3f}] "
                f"WR={best['win_rate']:.0%} (+{best['lift']:.0%} lift, "
                f"n={best['sample_size']})")

    # -- REAL-TIME EDGE SCORING -------------------------------------

    def get_conviction_boost(self, features: Dict,
                             strategy: str = "",
                             tf: str = "") -> Tuple[float, List[Dict]]:
        """Check if current signal features match any proven winning ranges.

        Returns (total_boost, list_of_matching_edges).
        """
        if not self._edges or not features:
            return 0.0, []

        matching = []
        total_boost = 0.0

        for edge in self._edges:
            feat_name = edge["feature"]
            if feat_name not in features:
                continue

            val = features[feat_name]
            if val is None or (isinstance(val, float) and np.isnan(val)):
                continue

            # Check if value falls in winning range
            if edge["range_low"] <= val <= edge["range_high"]:
                # Strategy-specific edges only match same strategy
                if "strategy" in edge and edge["strategy"] != strategy:
                    continue

                # Boost proportional to lift and sample confidence
                confidence = min(edge["sample_size"] / 20, 1.0)
                boost = edge["lift"] * 20 * confidence  # 10% lift -> +2 conv
                total_boost += boost
                matching.append(edge)

        total_boost = min(total_boost, MAX_CONVICTION_BOOST)
        return round(total_boost, 1), matching

    # -- REPORTS ----------------------------------------------------

    @property
    def stats(self) -> Dict:
        n = len(self._df) if self._df is not None else 0
        with_outcome = 0
        wins = 0
        if self._df is not None and n > 0 and "outcome" in self._df.columns:
            with_outcome = int((self._df["outcome"] != "").sum())
            wins = int((self._df["outcome"] == "win").sum())
        return {
            "total_records": n,
            "with_outcome": with_outcome,
            "wins": wins,
            "proven_edges": len(self._edges),
            "baseline_wr": self._baseline_wr,
        }

    def get_edge_report(self) -> str:
        """Human-readable edge report for dashboard/logging."""
        s = self.stats
        if s["total_records"] == 0:
            return "DNA Profiler: No trades recorded yet"

        lines = [
            f"DNA PROFILER: {s['total_records']} trades, "
            f"{s['with_outcome']} outcomes, "
            f"baseline WR={s['baseline_wr']:.0%}",
            ""
        ]

        if not self._edges:
            lines.append("  No proven edges yet "
                         f"(need {MIN_TRADES_FOR_ANALYSIS}+ outcomes)")
            return "\n".join(lines)

        lines.append(f"  TOP WINNING EDGES ({len(self._edges)} found):")
        for i, edge in enumerate(self._edges[:8], 1):
            strat_tag = f" [{edge['strategy']}]" if "strategy" in edge else ""
            lines.append(
                f"  #{i} {edge['feature']}{strat_tag}: "
                f"[{edge['range_low']:.4f} to {edge['range_high']:.4f}] "
                f"WR={edge['win_rate']:.0%} (+{edge['lift']:.0%}) "
                f"n={edge['sample_size']} avgR={edge['avg_r']:+.2f}")

        return "\n".join(lines)

    def get_full_analysis(self) -> str:
        """Deep analysis: correlation matrix + winner/loser split."""
        if self._df is None or len(self._df) == 0:
            return "No data yet"

        df = self._df[self._df["outcome"].isin(["win", "loss"])].copy()
        if len(df) < 5:
            return f"Only {len(df)} outcomes -- need more data"

        df["win"] = (df["outcome"] == "win").astype(int)

        meta_cols = {"symbol", "strategy", "tf", "side", "entry_price",
                     "stop_dist", "conviction", "grade", "source",
                     "entry_ts", "outcome", "pnl_r", "exit_ts"}
        feat_cols = [c for c in df.columns
                     if c not in meta_cols and c != "win"
                     and pd.api.types.is_numeric_dtype(df[c])]

        lines = [f"FULL DNA ANALYSIS ({len(df)} trades, "
                 f"WR={df['win'].mean():.1%})", ""]

        # Feature correlations with win
        lines.append("FEATURE CORRELATION WITH WINNING:")
        corrs = {}
        for col in feat_cols:
            valid = df[[col, "win"]].dropna()
            if len(valid) >= 5:
                corr = float(valid[col].corr(valid["win"]))
                if not np.isnan(corr):
                    corrs[col] = round(corr, 4)

        for col, corr in sorted(corrs.items(),
                                key=lambda x: abs(x[1]), reverse=True):
            arrow = "+" if corr > 0 else "-"
            lines.append(f"  {arrow} {col}: r={corr:+.3f}")

        # Winner vs loser means
        lines.append("\nWINNER vs LOSER MEANS:")
        for col in feat_cols:
            w_mean = float(df.loc[df["win"] == 1, col].mean())
            l_mean = float(df.loc[df["win"] == 0, col].mean())
            if not np.isnan(w_mean) and not np.isnan(l_mean):
                diff = w_mean - l_mean
                if abs(diff) > 0.001:
                    lines.append(f"  {col}: W={w_mean:.3f} L={l_mean:.3f} "
                                 f"diff={diff:+.3f}")

        # Per-strategy breakdown
        lines.append("\nPER-STRATEGY WIN RATES:")
        for strat, group in df.groupby("strategy"):
            wr = float(group["win"].mean())
            n = len(group)
            avg_r = float(group["pnl_r"].mean())
            lines.append(f"  {strat}: WR={wr:.0%} n={n} avgR={avg_r:+.3f}")

        return "\n".join(lines)
