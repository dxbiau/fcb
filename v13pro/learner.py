"""
v13pro/learner.py -- Bayesian Feature Learner + Pair DNA Profiler.

Pure-Python Bayesian system that learns which setup features predict
wins vs losses. Uses Beta-distribution conjugate priors -- the simplest
Bayesian model, zero extra dependencies, mathematically optimal for
binary outcomes with small samples.

How it works:
  For every trade, ~18 discrete features are extracted from the strategy
  signal context (volume tier, trend state, session, direction, hour, etc.).

  Each (feature_name, feature_value) tracks a Beta(a, b) posterior:
    win  → a += 1
    loss → b += 1
    posterior WR = a / (a+b)
    edge = posterior - 0.5

  A conviction adjustment (+/- up to 12 pts) is computed from the combined
  edge of the setup's active features, weighted by confidence.

PairDNA tracks per-symbol performance:
  - Beta(a, b) for per-pair win prob
  - Running PnL, streak, hot/warm/cold classification

Self-contained: no imports from obr/.
"""

import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from v13pro import config as cfg
from v13pro import logger as log

LEARNER_FILE = os.path.join(cfg.LOG_DIR, "learner_memory.json")

# Adjustment bounds
MAX_BONUS = 12.0
MAX_PENALTY = -10.0
MIN_CONFIDENCE = 3

# Pair DNA thresholds
PAIR_HOT_WR = 0.58
PAIR_COLD_WR = 0.38
PAIR_MIN_TRADES = 3

# Session boundaries
SESSIONS = {
    "asia": (0, 8), "london": (8, 13), "overlap": (13, 17),
    "ny": (17, 22), "late": (22, 24),
}
HOUR_BLOCKS = {h: f"h{(h//4)*4:02d}_{(h//4)*4+3:02d}" for h in range(24)}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ═══════════════════════════════════════════════════════════════
#  FEATURE EXTRACTION — adapted for v13pro 12-strategy signals
# ═══════════════════════════════════════════════════════════════

def extract_features(
    candles: List[dict],
    direction: str,
    strategy: str,
    timeframe: str,
    symbol: str = "",
    entry_price: float = 0,
    stop_dist: float = 0,
    conviction: float = 0,
    grade: str = "",
    equity_phase: str = "unknown",
    drawdown_zone: str = "normal",
) -> Dict[str, str]:
    """
    Extract ~18 discrete features from a v13pro signal context.
    All values are categorical strings for Beta tracking.
    """
    now = datetime.now(timezone.utc)

    # Volume classification
    vol_tier = "unknown"
    if candles and len(candles) >= 5:
        last_vol = candles[-1].get("volume", 0)
        avg_vol = sum(c.get("volume", 0) for c in candles[-20:]) / min(len(candles), 20)
        if avg_vol > 0:
            ratio = last_vol / avg_vol
            if ratio >= 3.0: vol_tier = "spike"
            elif ratio >= 1.8: vol_tier = "high"
            elif ratio >= 1.0: vol_tier = "avg"
            else: vol_tier = "low"

    # Body ratio
    body_tier = "unknown"
    if candles:
        c = candles[-1]
        rng = c.get("high", 0) - c.get("low", 0)
        if rng > 0:
            body = abs(c.get("close", 0) - c.get("open", 0))
            br = body / rng
            if br >= 0.80: body_tier = "huge"
            elif br >= 0.60: body_tier = "large"
            elif br >= 0.40: body_tier = "medium"
            elif br >= 0.20: body_tier = "small"
            else: body_tier = "tiny"

    # Trend (simple SMA comparison)
    trend = "unknown"
    if candles and len(candles) >= 24:
        sma24 = sum(c["close"] for c in candles[-24:]) / 24
        sma6 = sum(c["close"] for c in candles[-6:]) / 6
        if sma6 > sma24 * 1.001: trend = "up"
        elif sma6 < sma24 * 0.999: trend = "down"
        else: trend = "flat"

    # ATR relative to price (volatility)
    vol_level = "unknown"
    if candles and len(candles) >= 14 and entry_price > 0:
        atr = sum(c["high"] - c["low"] for c in candles[-14:]) / 14
        atr_pct = atr / entry_price * 100
        if atr_pct >= 2.0: vol_level = "high_vol"
        elif atr_pct >= 0.8: vol_level = "med_vol"
        else: vol_level = "low_vol"

    # Risk/reward context
    rr_tier = "unknown"
    if stop_dist > 0 and entry_price > 0:
        sl_pct = stop_dist / entry_price * 100
        if sl_pct >= 1.5: rr_tier = "wide_sl"
        elif sl_pct >= 0.8: rr_tier = "med_sl"
        else: rr_tier = "tight_sl"

    # Session
    session = "late"
    for sname, (s, e) in SESSIONS.items():
        if s <= now.hour < e:
            session = sname
            break

    # Price magnitude
    if entry_price <= 0.01: pmag = "micro"
    elif entry_price <= 1.0: pmag = "sub_dollar"
    elif entry_price <= 100: pmag = "tens"
    elif entry_price <= 1000: pmag = "hundreds"
    else: pmag = "thousands"

    features = {
        "direction": direction,
        "strategy": strategy,
        "timeframe": timeframe,
        "body_ratio": body_tier,
        "volume": vol_tier,
        "trend": trend,
        "volatility": vol_level,
        "rr_tier": rr_tier,
        "price_magnitude": pmag,
        "session": session,
        "hour_block": HOUR_BLOCKS.get(now.hour, "h00_03"),
        "day": DAYS[now.weekday()],
        "equity_phase": str(equity_phase),
        "drawdown_zone": str(drawdown_zone),
        # Composites
        "trend_x_dir": f"{trend}_{direction}",
        "session_x_dir": f"{session}_{direction}",
        "strat_x_tf": f"{strategy}_{timeframe}",
        "grade": grade or "unscored",
    }
    return features


# ═══════════════════════════════════════════════════════════════
#  BETA TRACKER
# ═══════════════════════════════════════════════════════════════

class BetaTracker:
    __slots__ = ("alpha", "beta")

    def __init__(self, alpha=1.0, beta=1.0):
        self.alpha = alpha
        self.beta = beta

    def update(self, is_win: bool):
        if is_win:
            self.alpha += 1.0
        else:
            self.beta += 1.0

    @property
    def posterior(self): return self.alpha / (self.alpha + self.beta)

    @property
    def edge(self): return self.posterior - 0.5

    @property
    def confidence(self): return int(self.alpha + self.beta - 2)

    def to_dict(self): return {"a": round(self.alpha, 2), "b": round(self.beta, 2)}

    @classmethod
    def from_dict(cls, d): return cls(d.get("a", 1.0), d.get("b", 1.0))


# ═══════════════════════════════════════════════════════════════
#  PAIR DNA
# ═══════════════════════════════════════════════════════════════

class PairDNA:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.tracker = BetaTracker()
        self.total_r = 0.0
        self.streak = 0
        self.best_r = 0.0
        self.worst_r = 0.0
        self.last_trade_ts = ""
        self.feature_affinity: Dict[str, Dict[str, int]] = {}

    def update(self, is_win: bool, pnl_r: float, features: Dict[str, str]):
        self.tracker.update(is_win)
        self.total_r += pnl_r
        self.best_r = max(self.best_r, pnl_r)
        self.worst_r = min(self.worst_r, pnl_r)
        self.last_trade_ts = datetime.now(timezone.utc).isoformat()
        self.streak = max(0, self.streak) + 1 if is_win else min(0, self.streak) - 1
        if is_win:
            for fname, fval in features.items():
                self.feature_affinity.setdefault(fname, {})
                self.feature_affinity[fname][fval] = \
                    self.feature_affinity[fname].get(fval, 0) + 1

    @property
    def status(self):
        n = self.tracker.confidence
        if n < PAIR_MIN_TRADES: return "unknown"
        wr = self.tracker.posterior
        if wr >= PAIR_HOT_WR: return "hot"
        if wr <= PAIR_COLD_WR: return "cold"
        return "warm"

    def to_dict(self):
        return {
            "symbol": self.symbol, "tracker": self.tracker.to_dict(),
            "total_r": round(self.total_r, 4), "streak": self.streak,
            "best_r": round(self.best_r, 4), "worst_r": round(self.worst_r, 4),
            "last_trade": self.last_trade_ts, "affinity": self.feature_affinity,
        }

    @classmethod
    def from_dict(cls, d):
        p = cls(d["symbol"])
        p.tracker = BetaTracker.from_dict(d.get("tracker", {}))
        p.total_r = d.get("total_r", 0.0)
        p.streak = d.get("streak", 0)
        p.best_r = d.get("best_r", 0.0)
        p.worst_r = d.get("worst_r", 0.0)
        p.last_trade_ts = d.get("last_trade", "")
        p.feature_affinity = d.get("affinity", {})
        return p


# ═══════════════════════════════════════════════════════════════
#  BAYESIAN LEARNER
# ═══════════════════════════════════════════════════════════════

class BayesianLearner:
    """Adaptive learning engine with Beta-distribution inference."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = self._load()
        self._feature_trackers: Dict[str, Dict[str, BetaTracker]] = {}
        self._pair_dna: Dict[str, PairDNA] = {}
        self._pending: Dict[str, Dict[str, str]] = {}
        self._hydrate()

    def _load(self):
        default = {
            "feature_trackers": {}, "pair_dna": {},
            "total_updates": 0,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        try:
            os.makedirs(cfg.LOG_DIR, exist_ok=True)
            if os.path.exists(LEARNER_FILE):
                with open(LEARNER_FILE, "r") as f:
                    data = json.load(f)
                for k, v in default.items():
                    if k not in data:
                        data[k] = v
                return data
        except Exception:
            pass
        return default

    def _save(self):
        try:
            data = {
                "feature_trackers": {},
                "pair_dna": {},
                "total_updates": self._data.get("total_updates", 0),
                "created": self._data.get("created", ""),
                "last_update": datetime.now(timezone.utc).isoformat(),
            }
            for fname, vals in self._feature_trackers.items():
                data["feature_trackers"][fname] = {
                    fv: bt.to_dict() for fv, bt in vals.items()
                }
            for sym, dna in self._pair_dna.items():
                data["pair_dna"][sym] = dna.to_dict()
            os.makedirs(cfg.LOG_DIR, exist_ok=True)
            with open(LEARNER_FILE, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            log.debug(f"Learner save: {e}")

    def _hydrate(self):
        for fname, vals in self._data.get("feature_trackers", {}).items():
            self._feature_trackers[fname] = {
                fv: BetaTracker.from_dict(bt) for fv, bt in vals.items()
            }
        for sym, dd in self._data.get("pair_dna", {}).items():
            self._pair_dna[sym] = PairDNA.from_dict(dd)

    # ── Scoring adjustment ───────────────────────────────────

    def compute_adjustment(self, features: Dict[str, str]) -> float:
        with self._lock:
            if not self._feature_trackers:
                return 0.0
            total_we = 0.0
            n = 0
            for fname, fval in features.items():
                t = self._feature_trackers.get(fname, {}).get(fval)
                if t is None or t.confidence < MIN_CONFIDENCE:
                    continue
                weight = min(t.confidence, 20) / 20.0
                total_we += t.edge * weight
                n += 1
            if n == 0:
                return 0.0
            avg = total_we / n
            scale = (MAX_BONUS - MAX_PENALTY) / 0.50
            adj = max(MAX_PENALTY, min(MAX_BONUS, avg * scale))
            return round(adj, 2)

    def get_pair_adjustment(self, symbol: str) -> float:
        with self._lock:
            dna = self._pair_dna.get(symbol)
            if dna is None or dna.tracker.confidence < PAIR_MIN_TRADES:
                return 0.0
            st = dna.status
            if st == "hot": return 3.0
            if st == "cold": return -5.0
            return 0.0

    def get_pair_status(self, symbol: str) -> dict:
        with self._lock:
            dna = self._pair_dna.get(symbol)
            if dna is None:
                return {"status": "unknown", "wr": 0.5, "trades": 0, "total_r": 0.0}
            return {
                "status": dna.status,
                "wr": round(dna.tracker.posterior, 3),
                "trades": dna.tracker.confidence,
                "total_r": round(dna.total_r, 4),
                "streak": dna.streak,
            }

    def get_hot_pairs(self) -> List[str]:
        with self._lock:
            return [s for s, d in self._pair_dna.items() if d.status == "hot"]

    def get_cold_pairs(self) -> List[str]:
        with self._lock:
            return [s for s, d in self._pair_dna.items() if d.status == "cold"]

    # ── Pending context ──────────────────────────────────────

    def store_pending(self, symbol: str, features: Dict[str, str]):
        with self._lock:
            self._pending[symbol] = features.copy()

    # ── Learning update ──────────────────────────────────────

    def update(self, symbol: str, is_win: bool, pnl_r: float,
               features: Optional[Dict[str, str]] = None):
        with self._lock:
            if features is None:
                features = self._pending.pop(symbol, None)
            else:
                self._pending.pop(symbol, None)
            if features:
                for fname, fval in features.items():
                    self._feature_trackers.setdefault(fname, {})
                    if fval not in self._feature_trackers[fname]:
                        self._feature_trackers[fname][fval] = BetaTracker()
                    self._feature_trackers[fname][fval].update(is_win)
            if symbol not in self._pair_dna:
                self._pair_dna[symbol] = PairDNA(symbol)
            self._pair_dna[symbol].update(is_win, pnl_r, features or {})
            self._data["total_updates"] = self._data.get("total_updates", 0) + 1
            self._save()

    # ── Insights ─────────────────────────────────────────────

    def get_insights(self) -> dict:
        with self._lock:
            all_f = []
            for fname, vals in self._feature_trackers.items():
                for fval, bt in vals.items():
                    if bt.confidence >= MIN_CONFIDENCE:
                        all_f.append((fname, fval, bt.posterior, bt.confidence, bt.edge))
            all_f.sort(key=lambda x: x[4], reverse=True)
            winners = [(f[0], f[1], round(f[2], 3), f[3]) for f in all_f[:5] if f[4] > 0]
            losers = [(f[0], f[1], round(f[2], 3), f[3]) for f in all_f[-5:] if f[4] < 0]
            losers.reverse()

            pairs = []
            for sym, dna in self._pair_dna.items():
                if dna.tracker.confidence >= 2:
                    pairs.append((sym, round(dna.tracker.posterior, 3),
                                  dna.tracker.confidence, round(dna.total_r, 4)))
            pairs.sort(key=lambda x: x[1], reverse=True)

            return {
                "winning_features": winners,
                "losing_features": losers,
                "hot_pairs": [p for p in pairs if p[1] >= PAIR_HOT_WR],
                "cold_pairs": [p for p in pairs if p[1] <= PAIR_COLD_WR],
                "total_updates": self._data.get("total_updates", 0),
            }

    def log_status(self):
        insights = self.get_insights()
        n = insights["total_updates"]
        if n == 0:
            return
        log.info(f"  Learner: {n} outcomes tracked")
        if insights["winning_features"]:
            top = insights["winning_features"][0]
            log.info(f"    Best: {top[0]}={top[1]} (WR={top[2]*100:.0f}%, n={top[3]})")
        if insights["losing_features"]:
            worst = insights["losing_features"][0]
            log.info(f"    Worst: {worst[0]}={worst[1]} (WR={worst[2]*100:.0f}%, n={worst[3]})")
        hot = insights["hot_pairs"]
        cold = insights["cold_pairs"]
        if hot:
            log.info(f"    Hot: {', '.join(p[0].split('/')[0] for p in hot[:3])}")
        if cold:
            log.info(f"    Cold: {', '.join(p[0].split('/')[0] for p in cold[:3])}")

    @property
    def total_updates(self):
        with self._lock:
            return self._data.get("total_updates", 0)
