"""
research/mega_sweep.py — Pure-Python FCB backtest + parameter sweep.

ZERO external dependencies — uses only stdlib (csv, math, datetime, etc.).
Does NOT import from config.py, strategy/, backtest/, or live/.
Completely standalone — touches NOTHING in the live bot.

Reads cached 5m OHLCV data from data/bybit_futures_*.csv and sweeps
across the parameter space to find optimal FCB settings.

Usage:
    python research/mega_sweep.py

Output:
    research/sweep_results.csv          — per-config per-pair metrics
    research/sweep_summary.csv          — aggregated per-config metrics
    research/sweep_best_configs.txt     — top configs ranked by edge
"""

from __future__ import annotations

import csv
import glob
import itertools
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ─── Paths ───
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
OUT_DIR  = ROOT_DIR / "research"
OUT_DIR.mkdir(exist_ok=True)

# ─── Session Definitions (matching LIVE bot) ───
SESSIONS = {
    "asia":   (0,  8),   # 00:00 – 08:00 UTC
    "london": (8,  16),  # 08:00 – 16:00 UTC
    "ny":     (16, 24),  # 16:00 – 24:00 UTC
}

TIMEFRAME_MINUTES = 5


# ═══════════════════════════════════════════════════
#  DATA STRUCTURES
# ═══════════════════════════════════════════════════

@dataclass
class Candle:
    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    session_name: str = ""
    session_date: str = ""
    session_start: Optional[datetime] = None
    session_end: Optional[datetime] = None
    # Pre-computed indicators (filled in-place)
    body_ratio: float = 0.0
    candle_dir: int = 0  # +1 bullish, -1 bearish, 0 doji


@dataclass
class SweepConfig:
    """One parameter combination to test."""
    name: str                    # human-readable label
    tp_r: float = 1.5           # take-profit R-multiple
    trail_enabled: bool = False
    trail_activation_r: float = 1.0  # R-level to start trailing
    trail_distance_r: float = 0.5    # trail behind peak in R (NOT price %)
    trail_max_r: float = 10.0        # hard cap R
    safety_tp_r: float = 10.0        # exchange safety TP when trailing
    min_c2_body: float = 0.0         # min breakout candle body ratio
    fc_counter: bool = False          # require FC lean opposite breakout
    vol_ratio_long: float = 0.0      # min vol ratio for longs (0=off)
    vol_ratio_short: float = 0.0     # min vol ratio for shorts (0=off)
    min_range_pct: float = 0.0       # min range as % of price (0=off)


@dataclass
class Trade:
    pair: str
    session_name: str
    session_date: str
    direction: str           # "long" or "short"
    entry_price: float
    entry_time: datetime
    stop_loss: float
    take_profit: float
    risk_per_unit: float
    range_high: float
    range_low: float
    range_midpoint: float
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    r_multiple: Optional[float] = None
    peak_r: float = 0.0
    trail_active: bool = False
    trail_stop_price: Optional[float] = None
    peak_price: Optional[float] = None
    fc_body_ratio: float = 0.0     # first candle body ratio
    c2_body_ratio: float = 0.0     # breakout candle body ratio
    vol_ratio: float = 0.0         # breakout vol / FC vol

    @property
    def is_closed(self) -> bool:
        return self.exit_price is not None

    def close(self, price: float, time: datetime, reason: str) -> None:
        self.exit_price = price
        self.exit_time = time
        self.exit_reason = reason
        if self.risk_per_unit > 0:
            if self.direction == "long":
                self.r_multiple = (price - self.entry_price) / self.risk_per_unit
            else:
                self.r_multiple = (self.entry_price - price) / self.risk_per_unit
        else:
            self.r_multiple = 0.0


@dataclass
class PairResult:
    pair: str
    config_name: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_r: float = 0.0
    avg_r: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    max_drawdown_r: float = 0.0
    best_r: float = 0.0
    worst_r: float = 0.0
    expectancy_r: float = 0.0
    exit_reasons: Dict[str, int] = field(default_factory=dict)


# ═══════════════════════════════════════════════════
#  CSV DATA LOADING (pure Python)
# ═══════════════════════════════════════════════════

def load_csv(path: str) -> List[Candle]:
    """Load a single CSV file into a list of Candle objects."""
    candles = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt_str = row["date"]
            # Parse: "2025-08-17 10:35:00+00:00"
            try:
                if "+" in dt_str or dt_str.endswith("Z"):
                    # Strip timezone for simplicity, assume UTC
                    dt_str_clean = dt_str.replace("+00:00", "").replace("Z", "")
                    dt = datetime.strptime(dt_str_clean, "%Y-%m-%d %H:%M:%S")
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    dt = dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])
            v = float(row["volume"])

            candle = Candle(date=dt, open=o, high=h, low=l, close=c, volume=v)
            # Pre-compute candle metrics
            full_range = h - l
            if full_range > 0:
                candle.body_ratio = abs(c - o) / full_range
            candle.candle_dir = 1 if c > o else (-1 if c < o else 0)
            candles.append(candle)

    # Sort by date
    candles.sort(key=lambda x: x.date)
    return candles


def assign_sessions(candles: List[Candle]) -> None:
    """Assign session info to each candle IN-PLACE."""
    for c in candles:
        hour = c.date.hour
        for name, (start_h, end_h) in SESSIONS.items():
            if start_h <= hour < end_h:
                c.session_name = name
                c.session_date = c.date.strftime("%Y-%m-%d")
                c.session_start = c.date.replace(
                    hour=start_h, minute=0, second=0, microsecond=0
                )
                c.session_end = c.date.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(hours=end_h)
                break


def extract_pair_name(filename: str) -> str:
    """Extract pair symbol from filename like 'bybit_futures_SOL_USDT_USDT_5m.csv'."""
    # Pattern: bybit_futures_{SYMBOL}_USDT_USDT_5m.csv
    base = os.path.basename(filename).replace(".csv", "")
    # Remove prefix and suffix
    parts = base.split("_")
    # bybit_futures_SYMBOL_USDT_USDT_5m
    # Find the symbol part(s) between "futures" and the last "USDT_USDT_5m"
    if len(parts) >= 6 and parts[0] == "bybit" and parts[1] == "futures":
        # Symbol is parts[2:-3] joined (for multi-part symbols like 10000QUBIC)
        symbol_parts = parts[2:-3]
        symbol = "_".join(symbol_parts) if symbol_parts else parts[2]
        return f"{symbol}/USDT:USDT"
    return base


# ═══════════════════════════════════════════════════
#  FCB ENGINE (pure Python, no external deps)
# ═══════════════════════════════════════════════════

def run_fcb(pair: str, candles: List[Candle], cfg: SweepConfig) -> List[Trade]:
    """
    Run FCB strategy on candle list. Returns list of closed trades.

    Logic matches live bot:
    1. First candle of session = range (high/low)
    2. Next candle that breaks range = breakout signal
    3. Very next candle must retest range level with wick, hold with close
    4. Entry at close of retest candle, SL at range midpoint
    5. TP at tp_r × risk (or safety TP with trailing)
    6. Trail: once R >= activation_r, trail stop follows peak by distance_r
    """
    if not candles:
        return []

    trades: List[Trade] = []
    # Group candles by session key
    session_keys_seen = set()

    # State machine per session
    # Phase: "waiting_fc" → "waiting_breakout" → "waiting_retest" → "in_trade" → "done"
    phase = "idle"
    fc_candle: Optional[Candle] = None
    range_high: float = 0.0
    range_low: float = 0.0
    range_mid: float = 0.0
    breakout_dir: str = ""
    breakout_idx: int = -1
    current_trade: Optional[Trade] = None
    current_session_key: str = ""
    daily_trades: Dict[str, int] = {}

    for i, candle in enumerate(candles):
        if not candle.session_name:
            continue

        session_key = f"{candle.session_name}_{candle.session_date}"

        # New session → reset state
        if session_key != current_session_key:
            # Close any open trade from previous session
            if current_trade and not current_trade.is_closed:
                if i > 0:
                    current_trade.close(candles[i-1].close, candles[i-1].date, "session_end")
                    trades.append(current_trade)
                current_trade = None

            current_session_key = session_key
            if session_key in session_keys_seen:
                phase = "done"
                continue
            session_keys_seen.add(session_key)
            phase = "waiting_fc"
            fc_candle = None
            breakout_dir = ""
            breakout_idx = -1
            current_trade = None

        # ── In trade: manage SL/TP/trail ──
        if phase == "in_trade" and current_trade and not current_trade.is_closed:
            _manage_trade(candle, current_trade, cfg)
            if current_trade.is_closed:
                trades.append(current_trade)
                current_trade = None
                phase = "done"
            continue

        if phase == "done":
            continue

        # ── Waiting for first candle ──
        if phase == "waiting_fc":
            fc_candle = candle
            range_high = candle.high
            range_low = candle.low
            range_mid = (range_high + range_low) / 2.0
            phase = "waiting_breakout"
            continue

        # ── Waiting for breakout ──
        if phase == "waiting_breakout":
            # Time cutoff: only look for breakouts in first 60 minutes
            if candle.session_start:
                elapsed = (candle.date - candle.session_start).total_seconds() / 60
                if elapsed > 60:
                    phase = "done"
                    continue

            c = candle.close
            if c > range_high:
                breakout_dir = "long"
                breakout_idx = i
                phase = "waiting_retest"
            elif c < range_low:
                breakout_dir = "short"
                breakout_idx = i
                phase = "waiting_retest"
            continue

        # ── Waiting for retest (must be very next candle) ──
        if phase == "waiting_retest":
            if i != breakout_idx + 1:
                phase = "done"
                continue

            c = candle.close
            h = candle.high
            l = candle.low

            if breakout_dir == "long":
                retested = l <= range_high
                held = c > range_high
            else:
                retested = h >= range_low
                held = c < range_low

            if not (retested and held):
                phase = "done"
                continue

            # ── Apply micro-filters ──
            bo_candle = candles[breakout_idx]

            # C2 body ratio filter
            if cfg.min_c2_body > 0:
                if bo_candle.body_ratio < cfg.min_c2_body:
                    phase = "done"
                    continue

            # FC counter filter (first candle must lean opposite breakout)
            if cfg.fc_counter and fc_candle:
                if breakout_dir == "long" and fc_candle.candle_dir > 0:
                    phase = "done"  # FC was bullish but we're buying — no counter
                    continue
                if breakout_dir == "short" and fc_candle.candle_dir < 0:
                    phase = "done"  # FC was bearish but we're selling — no counter
                    continue

            # Volume ratio filter
            if fc_candle and fc_candle.volume > 0:
                vol_r = bo_candle.volume / fc_candle.volume
            else:
                vol_r = 1.0

            if breakout_dir == "long" and cfg.vol_ratio_long > 0:
                if vol_r < cfg.vol_ratio_long:
                    phase = "done"
                    continue
            if breakout_dir == "short" and cfg.vol_ratio_short > 0:
                if vol_r < cfg.vol_ratio_short:
                    phase = "done"
                    continue

            # Min range filter
            if cfg.min_range_pct > 0:
                mid_price = (range_high + range_low) / 2.0
                if mid_price > 0:
                    range_pct = (range_high - range_low) / mid_price
                    if range_pct < cfg.min_range_pct:
                        phase = "done"
                        continue

            # ── Daily trade limit (3 per day) ──
            day_key = candle.session_date
            if daily_trades.get(day_key, 0) >= 3:
                phase = "done"
                continue

            # ── ENTRY ──
            entry_price = c
            sl = range_mid

            if breakout_dir == "long":
                risk = entry_price - sl
            else:
                risk = sl - entry_price

            if risk <= 0:
                phase = "done"
                continue

            # TP calculation
            if cfg.trail_enabled:
                # Safety TP at high R; trail handles real exit
                if breakout_dir == "long":
                    tp = entry_price + cfg.safety_tp_r * risk
                else:
                    tp = entry_price - cfg.safety_tp_r * risk
            else:
                if breakout_dir == "long":
                    tp = entry_price + cfg.tp_r * risk
                else:
                    tp = entry_price - cfg.tp_r * risk

            current_trade = Trade(
                pair=pair,
                session_name=candle.session_name,
                session_date=candle.session_date,
                direction=breakout_dir,
                entry_price=entry_price,
                entry_time=candle.date,
                stop_loss=sl,
                take_profit=tp,
                risk_per_unit=risk,
                range_high=range_high,
                range_low=range_low,
                range_midpoint=range_mid,
                fc_body_ratio=fc_candle.body_ratio if fc_candle else 0,
                c2_body_ratio=bo_candle.body_ratio,
                vol_ratio=vol_r,
            )

            daily_trades[day_key] = daily_trades.get(day_key, 0) + 1
            phase = "in_trade"
            continue

    # Close any remaining open trade
    if current_trade and not current_trade.is_closed and candles:
        current_trade.close(candles[-1].close, candles[-1].date, "data_end")
        trades.append(current_trade)

    return trades


def _manage_trade(candle: Candle, trade: Trade, cfg: SweepConfig) -> None:
    """Manage an open trade: check SL, TP, trailing stop."""
    h = candle.high
    l = candle.low
    c = candle.close
    t = candle.date
    risk = trade.risk_per_unit

    if trade.direction == "long":
        current_r_high = (h - trade.entry_price) / risk
        current_r_low = (l - trade.entry_price) / risk

        # Check SL
        if l <= trade.stop_loss:
            trade.close(trade.stop_loss, t, "sl")
            return

        # ── Trailing stop mode ──
        if trade.trail_active:
            # Update peak
            if h > (trade.peak_price or 0):
                trade.peak_price = h
                trade.peak_r = current_r_high
                # Trail stop = peak minus distance_r × risk_per_unit
                trade.trail_stop_price = h - cfg.trail_distance_r * risk

            # Max R cap
            if current_r_high >= cfg.trail_max_r:
                exit_p = trade.entry_price + cfg.trail_max_r * risk
                trade.close(exit_p, t, "max_r")
                return

            # Trail stop hit
            if trade.trail_stop_price and l <= trade.trail_stop_price:
                trade.close(trade.trail_stop_price, t, "trail")
                return

        else:
            # ── Not trailing yet ──
            if cfg.trail_enabled and current_r_high >= cfg.trail_activation_r:
                # Activate trail
                trade.trail_active = True
                trade.peak_price = h
                trade.peak_r = current_r_high
                trade.trail_stop_price = h - cfg.trail_distance_r * risk
                # Move SL to breakeven
                trade.stop_loss = trade.entry_price
                return

            elif not cfg.trail_enabled and h >= trade.take_profit:
                # Fixed TP hit
                trade.close(trade.take_profit, t, "tp")
                return

            elif cfg.trail_enabled and h >= trade.take_profit and current_r_high < cfg.trail_activation_r:
                # Safety TP hit but below trail activation
                trade.close(trade.take_profit, t, "tp")
                return

        # Update peak_r tracking even when not trail active
        if current_r_high > trade.peak_r:
            trade.peak_r = current_r_high

    else:  # SHORT
        current_r_high = (trade.entry_price - l) / risk  # best R this candle
        current_r_low  = (trade.entry_price - h) / risk  # worst R this candle

        # Check SL
        if h >= trade.stop_loss:
            trade.close(trade.stop_loss, t, "sl")
            return

        # ── Trailing stop mode ──
        if trade.trail_active:
            if l < (trade.peak_price or float('inf')):
                trade.peak_price = l
                trade.peak_r = current_r_high
                trade.trail_stop_price = l + cfg.trail_distance_r * risk

            if current_r_high >= cfg.trail_max_r:
                exit_p = trade.entry_price - cfg.trail_max_r * risk
                trade.close(exit_p, t, "max_r")
                return

            if trade.trail_stop_price and h >= trade.trail_stop_price:
                trade.close(trade.trail_stop_price, t, "trail")
                return

        else:
            if cfg.trail_enabled and current_r_high >= cfg.trail_activation_r:
                trade.trail_active = True
                trade.peak_price = l
                trade.peak_r = current_r_high
                trade.trail_stop_price = l + cfg.trail_distance_r * risk
                trade.stop_loss = trade.entry_price
                return

            elif not cfg.trail_enabled and l <= trade.take_profit:
                trade.close(trade.take_profit, t, "tp")
                return

            elif cfg.trail_enabled and l <= trade.take_profit and current_r_high < cfg.trail_activation_r:
                trade.close(trade.take_profit, t, "tp")
                return

        if current_r_high > trade.peak_r:
            trade.peak_r = current_r_high

    # Session end check
    if candle.session_end and t >= candle.session_end - timedelta(minutes=TIMEFRAME_MINUTES):
        trade.close(c, t, "session_end")


# ═══════════════════════════════════════════════════
#  METRICS COMPUTATION
# ═══════════════════════════════════════════════════

def compute_metrics(pair: str, config_name: str, trades: List[Trade]) -> PairResult:
    """Compute comprehensive metrics from a list of closed trades."""
    result = PairResult(pair=pair, config_name=config_name)
    closed = [t for t in trades if t.is_closed and t.r_multiple is not None]

    result.trades = len(closed)
    if result.trades == 0:
        return result

    r_values = [t.r_multiple for t in closed]
    winners = [r for r in r_values if r > 0]
    losers  = [r for r in r_values if r <= 0]

    result.wins = len(winners)
    result.losses = len(losers)
    result.total_r = sum(r_values)
    result.avg_r = result.total_r / result.trades
    result.win_rate = result.wins / result.trades if result.trades > 0 else 0
    result.avg_win_r = statistics.mean(winners) if winners else 0
    result.avg_loss_r = statistics.mean(losers) if losers else 0
    result.best_r = max(r_values) if r_values else 0
    result.worst_r = min(r_values) if r_values else 0

    # Profit factor
    gross_wins = sum(winners) if winners else 0
    gross_losses = abs(sum(losers)) if losers else 0
    result.profit_factor = gross_wins / gross_losses if gross_losses > 0 else (999.0 if gross_wins > 0 else 0.0)

    # Expectancy: E(R) = WR × avg_win + (1-WR) × avg_loss
    result.expectancy_r = result.avg_r

    # Max drawdown in R
    peak_r = 0.0
    cum_r = 0.0
    max_dd = 0.0
    for r in r_values:
        cum_r += r
        if cum_r > peak_r:
            peak_r = cum_r
        dd = peak_r - cum_r
        if dd > max_dd:
            max_dd = dd
    result.max_drawdown_r = max_dd

    # Exit reason counts
    for t in closed:
        reason = t.exit_reason or "unknown"
        result.exit_reasons[reason] = result.exit_reasons.get(reason, 0) + 1

    return result


def compute_equity_curve(trades: List[Trade], initial: float, risk_pct: float, leverage: int = 1) -> Tuple[float, float]:
    """
    Compound equity through trades.
    Returns (final_equity, max_drawdown_pct).

    NOTE: Leverage is already implicit in position sizing.
    When you risk X% of equity per trade, the R-multiple already
    captures the full P&L including leverage effects.
    P&L = risk_pct × R. That's it.
    (The old engine formula risk % × R × leverage was WRONG — double-count.)
    """
    closed = [t for t in trades if t.is_closed and t.r_multiple is not None]
    closed.sort(key=lambda t: t.entry_time)

    equity = initial
    peak_equity = initial
    max_dd_pct = 0.0

    for t in closed:
        # Correct formula: P&L % = risk % × R-multiple
        # Leverage is baked into position sizing, NOT into P&L calculation
        pnl_pct = risk_pct * t.r_multiple
        equity *= (1 + pnl_pct)
        equity = max(equity, 0.01)  # floor at 1 cent (ruin)

        if equity > peak_equity:
            peak_equity = equity
        if peak_equity > 0:
            dd_pct = (peak_equity - equity) / peak_equity
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

    return equity, max_dd_pct


# ═══════════════════════════════════════════════════
#  PARAMETER GRID
# ═══════════════════════════════════════════════════

def build_sweep_grid() -> List[SweepConfig]:
    """Build full parameter grid for sweep."""
    configs = []

    # ─── Group 1: Fixed TP (no trail) ───
    for tp_r in [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]:
        for body_filt, fc_filt in [(0.0, False), (0.50, True)]:
            label_filt = "filt" if body_filt > 0 else "raw"
            configs.append(SweepConfig(
                name=f"fixed_tp{tp_r}_{label_filt}",
                tp_r=tp_r,
                trail_enabled=False,
                min_c2_body=body_filt,
                fc_counter=fc_filt,
                vol_ratio_long=1.0 if body_filt > 0 else 0.0,
                vol_ratio_short=0.25 if body_filt > 0 else 0.0,
            ))

    # ─── Group 2: Trail combos ───
    # Sweep: activation × distance × max_r
    for act_r in [0.5, 0.75, 1.0, 1.25, 1.5]:
        for dist_r in [0.3, 0.5, 0.7, 1.0]:
            for max_r in [5.0, 7.0, 10.0]:
                for body_filt, fc_filt in [(0.0, False), (0.50, True)]:
                    label_filt = "filt" if body_filt > 0 else "raw"
                    configs.append(SweepConfig(
                        name=f"trail_a{act_r}_d{dist_r}_m{max_r}_{label_filt}",
                        tp_r=1.5,  # doesn't matter much with trail
                        trail_enabled=True,
                        trail_activation_r=act_r,
                        trail_distance_r=dist_r,
                        trail_max_r=max_r,
                        safety_tp_r=max_r,  # safety = max
                        min_c2_body=body_filt,
                        fc_counter=fc_filt,
                        vol_ratio_long=1.0 if body_filt > 0 else 0.0,
                        vol_ratio_short=0.25 if body_filt > 0 else 0.0,
                    ))

    # ─── Group 3: Live bot config (baseline) ───
    configs.append(SweepConfig(
        name="LIVE_BASELINE",
        tp_r=1.5,
        trail_enabled=True,
        trail_activation_r=1.0,
        trail_distance_r=0.5,
        trail_max_r=10.0,
        safety_tp_r=10.0,
        min_c2_body=0.50,
        fc_counter=True,
        vol_ratio_long=1.0,
        vol_ratio_short=0.25,
        min_range_pct=0.005,
    ))

    # ─── Group 4: Exploratory — tighter/wider distances ───
    for act_r in [0.75, 1.0]:
        for dist_r in [0.15, 0.2, 0.25, 1.5, 2.0]:
            configs.append(SweepConfig(
                name=f"trail_explore_a{act_r}_d{dist_r}_filt",
                tp_r=1.5,
                trail_enabled=True,
                trail_activation_r=act_r,
                trail_distance_r=dist_r,
                trail_max_r=10.0,
                safety_tp_r=10.0,
                min_c2_body=0.50,
                fc_counter=True,
                vol_ratio_long=1.0,
                vol_ratio_short=0.25,
            ))

    return configs


def build_focused_grid() -> List[SweepConfig]:
    """
    Smaller focused grid for faster iteration.
    Tests the most impactful parameters.
    """
    configs = []

    # Fixed TP sweep (raw + filtered)
    for tp_r in [1.0, 1.5, 2.0]:
        for filt in [False, True]:
            label = "filt" if filt else "raw"
            configs.append(SweepConfig(
                name=f"fixed_tp{tp_r}_{label}",
                tp_r=tp_r,
                trail_enabled=False,
                min_c2_body=0.50 if filt else 0.0,
                fc_counter=filt,
                vol_ratio_long=1.0 if filt else 0.0,
                vol_ratio_short=0.25 if filt else 0.0,
            ))

    # Trail sweep: key combos
    for act_r, dist_r in [(0.75, 0.3), (0.75, 0.5), (1.0, 0.3), (1.0, 0.5), (1.0, 0.7), (1.25, 0.5), (1.5, 0.5)]:
        for filt in [False, True]:
            label = "filt" if filt else "raw"
            configs.append(SweepConfig(
                name=f"trail_a{act_r}_d{dist_r}_{label}",
                tp_r=1.5,
                trail_enabled=True,
                trail_activation_r=act_r,
                trail_distance_r=dist_r,
                trail_max_r=10.0,
                safety_tp_r=10.0,
                min_c2_body=0.50 if filt else 0.0,
                fc_counter=filt,
                vol_ratio_long=1.0 if filt else 0.0,
                vol_ratio_short=0.25 if filt else 0.0,
            ))

    # Live baseline
    configs.append(SweepConfig(
        name="LIVE_BASELINE",
        tp_r=1.5,
        trail_enabled=True,
        trail_activation_r=1.0,
        trail_distance_r=0.5,
        trail_max_r=10.0,
        safety_tp_r=10.0,
        min_c2_body=0.50,
        fc_counter=True,
        vol_ratio_long=1.0,
        vol_ratio_short=0.25,
        min_range_pct=0.005,
    ))

    return configs


def build_finetune_grid() -> List[SweepConfig]:
    """
    Fine-tuned grid zooming into the winning region identified by focused sweep.

    Key findings from focused sweep:
    - Trail 0.3R distance >> 0.5R (massive improvement)
    - Activation 1.0R is optimal
    - Filters improve Sharpe significantly
    - 0.5R is current live setting → 0.3R could be a game-changer

    This grid fine-tunes around trail_distance [0.15 - 0.45] and
    activation [0.75 - 1.25] with finer steps.
    """
    configs = []

    # ─── Fine sweep: Trail distance (the KEY parameter) ───
    for dist_r in [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45]:
        for act_r in [0.75, 0.85, 0.95, 1.0, 1.1, 1.25]:
            for max_r in [7.0, 10.0]:
                configs.append(SweepConfig(
                    name=f"fine_a{act_r}_d{dist_r}_m{max_r}_filt",
                    tp_r=1.5,
                    trail_enabled=True,
                    trail_activation_r=act_r,
                    trail_distance_r=dist_r,
                    trail_max_r=max_r,
                    safety_tp_r=max_r,
                    min_c2_body=0.50,
                    fc_counter=True,
                    vol_ratio_long=1.0,
                    vol_ratio_short=0.25,
                ))

    # ─── Test effect of min_range_pct (live uses 0.5%) ───
    for range_pct in [0.0, 0.003, 0.005, 0.007, 0.01]:
        configs.append(SweepConfig(
            name=f"range_{range_pct}_a1.0_d0.3_filt",
            tp_r=1.5,
            trail_enabled=True,
            trail_activation_r=1.0,
            trail_distance_r=0.3,
            trail_max_r=10.0,
            safety_tp_r=10.0,
            min_c2_body=0.50,
            fc_counter=True,
            vol_ratio_long=1.0,
            vol_ratio_short=0.25,
            min_range_pct=range_pct,
        ))

    # ─── Test filter variants at the winning distance ───
    # Body ratio thresholds
    for body in [0.0, 0.30, 0.40, 0.50, 0.60, 0.70]:
        configs.append(SweepConfig(
            name=f"body{body}_a1.0_d0.3",
            tp_r=1.5,
            trail_enabled=True,
            trail_activation_r=1.0,
            trail_distance_r=0.3,
            trail_max_r=10.0,
            safety_tp_r=10.0,
            min_c2_body=body,
            fc_counter=True,
            vol_ratio_long=1.0,
            vol_ratio_short=0.25,
        ))

    # FC counter on/off
    for fc in [True, False]:
        for vol_l, vol_s in [(0.0, 0.0), (0.5, 0.25), (1.0, 0.25), (1.5, 0.50)]:
            label = f"fc{'Y' if fc else 'N'}_vl{vol_l}_vs{vol_s}"
            configs.append(SweepConfig(
                name=f"{label}_a1.0_d0.3",
                tp_r=1.5,
                trail_enabled=True,
                trail_activation_r=1.0,
                trail_distance_r=0.3,
                trail_max_r=10.0,
                safety_tp_r=10.0,
                min_c2_body=0.50,
                fc_counter=fc,
                vol_ratio_long=vol_l,
                vol_ratio_short=vol_s,
            ))

    # ─── Live baseline for comparison ───
    configs.append(SweepConfig(
        name="LIVE_BASELINE",
        tp_r=1.5,
        trail_enabled=True,
        trail_activation_r=1.0,
        trail_distance_r=0.5,
        trail_max_r=10.0,
        safety_tp_r=10.0,
        min_c2_body=0.50,
        fc_counter=True,
        vol_ratio_long=1.0,
        vol_ratio_short=0.25,
        min_range_pct=0.005,
    ))

    # ─── Candidate "UPGRADE" config (best from focused sweep, fine-tuned) ───
    configs.append(SweepConfig(
        name="CANDIDATE_UPGRADE",
        tp_r=1.5,
        trail_enabled=True,
        trail_activation_r=1.0,
        trail_distance_r=0.3,
        trail_max_r=10.0,
        safety_tp_r=10.0,
        min_c2_body=0.50,
        fc_counter=True,
        vol_ratio_long=1.0,
        vol_ratio_short=0.25,
        min_range_pct=0.005,
    ))

    return configs


def build_ultra_grid() -> List[SweepConfig]:
    """
    Ultra-fine grid testing the tighter-than-0.15R boundary.

    Fine-tune sweep revealed 0.15R >> 0.3R >> 0.5R (current live).
    Question: Does the trend continue below 0.15R, or is there a floor?

    Also tests the WINNING 0.15R combined with range filters and
    body/volume filter variants for the definitive optimum.
    """
    configs = []

    # ─── PHASE 1: Ultra-tight trail distances [0.05 - 0.20] ───
    for dist_r in [0.05, 0.08, 0.10, 0.12, 0.15, 0.18, 0.20]:
        for act_r in [0.75, 0.85, 0.95, 1.0, 1.1, 1.25]:
            configs.append(SweepConfig(
                name=f"ultra_a{act_r}_d{dist_r}_filt",
                tp_r=1.5,
                trail_enabled=True,
                trail_activation_r=act_r,
                trail_distance_r=dist_r,
                trail_max_r=10.0,
                safety_tp_r=10.0,
                min_c2_body=0.50,
                fc_counter=True,
                vol_ratio_long=1.0,
                vol_ratio_short=0.25,
            ))

    # ─── PHASE 2: Best distance (0.15R) + range filters ───
    for range_pct in [0.0, 0.002, 0.003, 0.004, 0.005, 0.007, 0.01]:
        for act_r in [0.85, 0.95, 1.0, 1.1, 1.25]:
            configs.append(SweepConfig(
                name=f"best_range{range_pct}_a{act_r}_d0.15",
                tp_r=1.5,
                trail_enabled=True,
                trail_activation_r=act_r,
                trail_distance_r=0.15,
                trail_max_r=10.0,
                safety_tp_r=10.0,
                min_c2_body=0.50,
                fc_counter=True,
                vol_ratio_long=1.0,
                vol_ratio_short=0.25,
                min_range_pct=range_pct,
            ))

    # ─── PHASE 3: Body ratio at 0.15R trail ───
    for body in [0.0, 0.30, 0.40, 0.50, 0.60, 0.70]:
        configs.append(SweepConfig(
            name=f"best_body{body}_a0.95_d0.15",
            tp_r=1.5,
            trail_enabled=True,
            trail_activation_r=0.95,
            trail_distance_r=0.15,
            trail_max_r=10.0,
            safety_tp_r=10.0,
            min_c2_body=body,
            fc_counter=True,
            vol_ratio_long=1.0,
            vol_ratio_short=0.25,
        ))

    # ─── PHASE 4: Volume filter variants at 0.15R ───
    for vol_l, vol_s in [(0.0, 0.0), (0.5, 0.25), (1.0, 0.25), (1.5, 0.50), (2.0, 0.5)]:
        for fc in [True, False]:
            label = f"best_fc{'Y' if fc else 'N'}_vl{vol_l}_vs{vol_s}_d0.15"
            configs.append(SweepConfig(
                name=label,
                tp_r=1.5,
                trail_enabled=True,
                trail_activation_r=0.95,
                trail_distance_r=0.15,
                trail_max_r=10.0,
                safety_tp_r=10.0,
                min_c2_body=0.50,
                fc_counter=fc,
                vol_ratio_long=vol_l,
                vol_ratio_short=vol_s,
            ))

    # ─── PHASE 5: DEFINITIVE CANDIDATE configs ───
    # Best-of Sharpe: a=0.95, d=0.15 with conservative range filter
    configs.append(SweepConfig(
        name="CANDIDATE_ALPHA",
        tp_r=1.5,
        trail_enabled=True,
        trail_activation_r=0.95,
        trail_distance_r=0.15,
        trail_max_r=10.0,
        safety_tp_r=10.0,
        min_c2_body=0.50,
        fc_counter=True,
        vol_ratio_long=1.0,
        vol_ratio_short=0.25,
        min_range_pct=0.003,
    ))

    # High-R variant: a=1.25, d=0.15 (highest total R in fine-tune)
    configs.append(SweepConfig(
        name="CANDIDATE_BETA",
        tp_r=1.5,
        trail_enabled=True,
        trail_activation_r=1.25,
        trail_distance_r=0.15,
        trail_max_r=10.0,
        safety_tp_r=10.0,
        min_c2_body=0.50,
        fc_counter=True,
        vol_ratio_long=1.0,
        vol_ratio_short=0.25,
        min_range_pct=0.003,
    ))

    # Safe variant: a=0.75, d=0.15 (lowest Max DD in fine-tune)
    configs.append(SweepConfig(
        name="CANDIDATE_SAFE",
        tp_r=1.5,
        trail_enabled=True,
        trail_activation_r=0.75,
        trail_distance_r=0.15,
        trail_max_r=10.0,
        safety_tp_r=10.0,
        min_c2_body=0.50,
        fc_counter=True,
        vol_ratio_long=1.0,
        vol_ratio_short=0.25,
        min_range_pct=0.003,
    ))

    # ─── LIVE BASELINE (always include for comparison) ───
    configs.append(SweepConfig(
        name="LIVE_BASELINE",
        tp_r=1.5,
        trail_enabled=True,
        trail_activation_r=1.0,
        trail_distance_r=0.5,
        trail_max_r=10.0,
        safety_tp_r=10.0,
        min_c2_body=0.50,
        fc_counter=True,
        vol_ratio_long=1.0,
        vol_ratio_short=0.25,
        min_range_pct=0.005,
    ))

    return configs


# ═══════════════════════════════════════════════════
#  MAIN SWEEP RUNNER
# ═══════════════════════════════════════════════════

def discover_data_files() -> List[Tuple[str, str]]:
    """Find all Bybit 5m CSVs. Returns list of (pair_symbol, filepath)."""
    pattern = str(DATA_DIR / "bybit_futures_*_5m.csv")
    files = sorted(glob.glob(pattern))
    pairs = []
    for f in files:
        pair = extract_pair_name(f)
        pairs.append((pair, f))
    return pairs


def run_sweep(grid_name: str = "focused"):
    """
    Main entry point: run parameter sweep across all data.

    Args:
        grid_name: "full", "focused", or "finetune"
    """
    print("=" * 70)
    print("  FCB MEGA PARAMETER SWEEP — Pure Python Engine")
    print("  No numpy. No pandas. No live bot modifications.")
    print("=" * 70)

    # Discover data
    pair_files = discover_data_files()
    print(f"\n  Data files found: {len(pair_files)}")

    if not pair_files:
        print("  ERROR: No data files found in data/")
        return

    # Build config grid
    if grid_name == "full":
        configs = build_sweep_grid()
    elif grid_name == "ultra":
        configs = build_ultra_grid()
    elif grid_name == "finetune":
        configs = build_finetune_grid()
    else:
        configs = build_focused_grid()
    print(f"  Parameter configs: {len(configs)} ({grid_name} grid)")
    print(f"  Total backtests: {len(pair_files) * len(configs):,}")
    print()

    # Load all data upfront (memory is cheaper than re-reading)
    print("  Loading data...")
    t0 = time.time()
    pair_data: Dict[str, List[Candle]] = {}
    total_candles = 0
    for pair, fpath in pair_files:
        candles = load_csv(fpath)
        assign_sessions(candles)
        pair_data[pair] = candles
        total_candles += len(candles)
        sys.stdout.write(f"\r    Loaded {len(pair_data)}/{len(pair_files)} pairs ({total_candles:,} candles)")
        sys.stdout.flush()

    load_time = time.time() - t0
    print(f"\n    Done in {load_time:.1f}s — {total_candles:,} candles across {len(pair_data)} pairs\n")

    # Run sweep
    all_results: List[PairResult] = []
    config_aggregates: List[Dict] = []
    total_tests = len(configs) * len(pair_data)
    done = 0
    t_sweep = time.time()

    for ci, cfg in enumerate(configs):
        config_trades: List[Trade] = []
        config_pair_results: List[PairResult] = []

        for pair, candles in pair_data.items():
            trades = run_fcb(pair, candles, cfg)
            result = compute_metrics(pair, cfg.name, trades)
            all_results.append(result)
            config_pair_results.append(result)
            config_trades.extend(trades)
            done += 1

            if done % 500 == 0 or done == total_tests:
                elapsed = time.time() - t_sweep
                rate = done / elapsed if elapsed > 0 else 0
                eta = (total_tests - done) / rate if rate > 0 else 0
                sys.stdout.write(
                    f"\r  [{done:,}/{total_tests:,}] "
                    f"Config {ci+1}/{len(configs)} '{cfg.name}' — "
                    f"{rate:.0f} tests/s, ETA {eta:.0f}s"
                )
                sys.stdout.flush()

        # Aggregate metrics for this config
        agg = aggregate_config(cfg, config_pair_results, config_trades)
        config_aggregates.append(agg)

    elapsed = time.time() - t_sweep
    print(f"\n\n  Sweep complete in {elapsed:.1f}s ({done:,} tests)\n")

    # ─── Write results ───
    write_pair_results(all_results)
    write_summary(config_aggregates)
    write_best_configs(config_aggregates)

    # ─── Print top 20 ───
    print_top_configs(config_aggregates)


def aggregate_config(cfg: SweepConfig, pair_results: List[PairResult],
                     all_trades: List[Trade]) -> Dict:
    """Aggregate metrics across all pairs for one config."""
    total_trades = sum(r.trades for r in pair_results)
    total_wins = sum(r.wins for r in pair_results)
    total_losses = sum(r.losses for r in pair_results)
    total_r = sum(r.total_r for r in pair_results)
    profitable_pairs = sum(1 for r in pair_results if r.total_r > 0 and r.trades >= 5)
    tested_pairs = sum(1 for r in pair_results if r.trades >= 5)

    wr = total_wins / total_trades if total_trades > 0 else 0

    # Collect all R values
    r_values = []
    for t in all_trades:
        if t.is_closed and t.r_multiple is not None:
            r_values.append(t.r_multiple)

    winners = [r for r in r_values if r > 0]
    losers  = [r for r in r_values if r <= 0]
    avg_win = statistics.mean(winners) if winners else 0
    avg_loss = statistics.mean(losers) if losers else 0
    gross_w = sum(winners) if winners else 0
    gross_l = abs(sum(losers)) if losers else 0
    pf = gross_w / gross_l if gross_l > 0 else (999 if gross_w > 0 else 0)
    avg_r = total_r / total_trades if total_trades > 0 else 0

    # Max drawdown (sequential R)
    peak_r = 0.0
    cum_r = 0.0
    max_dd_r = 0.0
    for r in r_values:
        cum_r += r
        if cum_r > peak_r:
            peak_r = cum_r
        dd = peak_r - cum_r
        if dd > max_dd_r:
            max_dd_r = dd

    # Equity simulation at different risk levels
    # Leverage is implicit in position sizing — only risk% matters for compounding
    equity_12, dd_12 = compute_equity_curve(all_trades, 150.0, 0.12)
    equity_08, dd_08 = compute_equity_curve(all_trades, 150.0, 0.08)
    equity_06, dd_06 = compute_equity_curve(all_trades, 150.0, 0.06)
    equity_04, dd_04 = compute_equity_curve(all_trades, 150.0, 0.04)
    equity_02, dd_02 = compute_equity_curve(all_trades, 150.0, 0.02)

    # Sharpe-like ratio (R units)
    if len(r_values) >= 2:
        r_std = statistics.stdev(r_values)
        sharpe_r = avg_r / r_std if r_std > 0 else 0
    else:
        sharpe_r = 0

    # Kelly fraction: f* = (b*p - q) / b where p=WR, q=1-WR, b=avg_win/|avg_loss|
    if avg_loss < 0:
        b = avg_win / abs(avg_loss)
        q = 1 - wr
        kelly = (b * wr - q) / b if b > 0 else 0
    else:
        b = 0
        kelly = 0

    # Trades needed for x10 at 12% risk
    if total_trades > 0 and avg_r > 0:
        growth_per_trade = 1 + 0.12 * avg_r
        if growth_per_trade > 1:
            trades_to_x10 = math.log(10) / math.log(growth_per_trade)
        else:
            trades_to_x10 = float('inf')
    else:
        trades_to_x10 = float('inf')

    # Exit reason distribution
    exit_reasons: Dict[str, int] = {}
    for t in all_trades:
        if t.is_closed:
            reason = t.exit_reason or "unknown"
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    return {
        "config_name": cfg.name,
        "trail_enabled": cfg.trail_enabled,
        "tp_r": cfg.tp_r,
        "trail_act_r": cfg.trail_activation_r if cfg.trail_enabled else 0,
        "trail_dist_r": cfg.trail_distance_r if cfg.trail_enabled else 0,
        "trail_max_r": cfg.trail_max_r if cfg.trail_enabled else 0,
        "filtered": cfg.min_c2_body > 0,
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "win_rate": round(wr, 4),
        "total_r": round(total_r, 2),
        "avg_r": round(avg_r, 4),
        "avg_win_r": round(avg_win, 4),
        "avg_loss_r": round(avg_loss, 4),
        "profit_factor": round(pf, 3),
        "max_dd_r": round(max_dd_r, 2),
        "sharpe_r": round(sharpe_r, 4),
        "kelly_f": round(kelly, 4),
        "payoff_ratio": round(b, 3) if avg_loss < 0 else 0,
        "profitable_pairs": profitable_pairs,
        "tested_pairs": tested_pairs,
        "pair_hit_rate": round(profitable_pairs / tested_pairs, 3) if tested_pairs > 0 else 0,
        "equity_12": round(equity_12, 2),
        "dd_12": round(dd_12, 4),
        "equity_08": round(equity_08, 2),
        "dd_08": round(dd_08, 4),
        "equity_06": round(equity_06, 2),
        "dd_06": round(dd_06, 4),
        "equity_04": round(equity_04, 2),
        "dd_04": round(dd_04, 4),
        "equity_02": round(equity_02, 2),
        "dd_02": round(dd_02, 4),
        "trades_to_x10": round(trades_to_x10, 1) if trades_to_x10 < 99999 else "inf",
        "exit_reasons": exit_reasons,
    }


# ═══════════════════════════════════════════════════
#  OUTPUT
# ═══════════════════════════════════════════════════

def write_pair_results(results: List[PairResult]):
    """Write per-pair per-config results to CSV."""
    path = OUT_DIR / "sweep_results.csv"
    fields = [
        "config_name", "pair", "trades", "wins", "losses",
        "win_rate", "total_r", "avg_r", "avg_win_r", "avg_loss_r",
        "profit_factor", "max_drawdown_r", "best_r", "worst_r",
        "expectancy_r", "exit_reasons",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            writer.writerow({
                "config_name": r.config_name,
                "pair": r.pair,
                "trades": r.trades,
                "wins": r.wins,
                "losses": r.losses,
                "win_rate": round(r.win_rate, 4),
                "total_r": round(r.total_r, 3),
                "avg_r": round(r.avg_r, 4),
                "avg_win_r": round(r.avg_win_r, 4),
                "avg_loss_r": round(r.avg_loss_r, 4),
                "profit_factor": round(r.profit_factor, 3),
                "max_drawdown_r": round(r.max_drawdown_r, 2),
                "best_r": round(r.best_r, 3),
                "worst_r": round(r.worst_r, 3),
                "expectancy_r": round(r.expectancy_r, 4),
                "exit_reasons": str(r.exit_reasons),
            })
    print(f"  → {path} ({len(results):,} rows)")


def write_summary(aggregates: List[Dict]):
    """Write config-level summary to CSV."""
    path = OUT_DIR / "sweep_summary.csv"
    fields = [k for k in aggregates[0].keys() if k != "exit_reasons"]
    fields.append("exit_sl")
    fields.append("exit_tp")
    fields.append("exit_trail")
    fields.append("exit_session_end")
    fields.append("exit_max_r")
    fields.append("exit_data_end")

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for a in aggregates:
            row = {k: v for k, v in a.items() if k != "exit_reasons"}
            er = a.get("exit_reasons", {})
            row["exit_sl"] = er.get("sl", 0)
            row["exit_tp"] = er.get("tp", 0)
            row["exit_trail"] = er.get("trail", 0)
            row["exit_session_end"] = er.get("session_end", 0)
            row["exit_max_r"] = er.get("max_r", 0)
            row["exit_data_end"] = er.get("data_end", 0)
            writer.writerow(row)
    print(f"  → {path} ({len(aggregates)} configs)")


def write_best_configs(aggregates: List[Dict]):
    """Write human-readable top configs report."""
    path = OUT_DIR / "sweep_best_configs.txt"

    # Sort by different criteria
    by_total_r = sorted(aggregates, key=lambda x: x["total_r"], reverse=True)
    by_sharpe = sorted(aggregates, key=lambda x: x["sharpe_r"], reverse=True)
    by_pf = sorted([a for a in aggregates if a["total_trades"] >= 100],
                    key=lambda x: x["profit_factor"], reverse=True)
    by_equity_12 = sorted(aggregates, key=lambda x: x["equity_12"] if isinstance(x["equity_12"], (int, float)) else 0, reverse=True)
    by_kelly = sorted([a for a in aggregates if a["total_trades"] >= 100],
                      key=lambda x: x["kelly_f"], reverse=True)

    with open(path, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  FCB MEGA SWEEP — BEST CONFIGURATIONS\n")
        f.write(f"  Generated: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"  Total configs tested: {len(aggregates)}\n")
        f.write("=" * 80 + "\n\n")

        def write_section(title, data, n=15):
            f.write(f"\n{'─' * 80}\n")
            f.write(f"  {title}\n")
            f.write(f"{'─' * 80}\n\n")
            for i, a in enumerate(data[:n], 1):
                f.write(f"  #{i:2d} {a['config_name']}\n")
                f.write(f"      Trades: {a['total_trades']:,}  WR: {a['win_rate']:.1%}  "
                        f"Total R: {a['total_r']:+.1f}  Avg R: {a['avg_r']:+.4f}\n")
                f.write(f"      PF: {a['profit_factor']:.3f}  Sharpe(R): {a['sharpe_r']:.3f}  "
                        f"Kelly f*: {a['kelly_f']:.3f}  Payoff: {a['payoff_ratio']:.3f}\n")
                f.write(f"      Avg Win: {a['avg_win_r']:+.3f}R  Avg Loss: {a['avg_loss_r']:.3f}R  "
                        f"Max DD: {a['max_dd_r']:.1f}R\n")
                f.write(f"      Pairs: {a['profitable_pairs']}/{a['tested_pairs']} profitable"
                        f"  ({a['pair_hit_rate']:.1%})\n")
                er = a.get("exit_reasons", {})
                f.write(f"      Exits: SL={er.get('sl',0)} TP={er.get('tp',0)} "
                        f"Trail={er.get('trail',0)} SessEnd={er.get('session_end',0)} "
                        f"MaxR={er.get('max_r',0)}\n")
                # Equity curves
                f.write(f"      Equity ($150 start, compounded):\n")
                f.write(f"        12% risk: ${a['equity_12']:>12,.2f}  DD={a['dd_12']:.1%}\n")
                f.write(f"        08% risk: ${a['equity_08']:>12,.2f}  DD={a['dd_08']:.1%}\n")
                f.write(f"        06% risk: ${a['equity_06']:>12,.2f}  DD={a['dd_06']:.1%}\n")
                f.write(f"        04% risk: ${a['equity_04']:>12,.2f}  DD={a['dd_04']:.1%}\n")
                f.write(f"        02% risk: ${a['equity_02']:>12,.2f}  DD={a['dd_02']:.1%}\n")
                t2x10 = a['trades_to_x10']
                f.write(f"      Trades to x10 @ 12% risk: {t2x10}\n")
                f.write("\n")

        write_section("TOP 15 BY TOTAL R (raw edge)", by_total_r)
        write_section("TOP 15 BY SHARPE RATIO (risk-adjusted)", by_sharpe)
        write_section("TOP 15 BY PROFIT FACTOR (≥100 trades)", by_pf)
        write_section("TOP 15 BY EQUITY @ 12% risk ($150 start)", by_equity_12)
        write_section("TOP 15 BY KELLY FRACTION (≥100 trades)", by_kelly)

        # ─── LIVE BASELINE comparison ───
        baseline = [a for a in aggregates if a["config_name"] == "LIVE_BASELINE"]
        if baseline:
            f.write(f"\n{'═' * 80}\n")
            f.write("  LIVE BASELINE (current bot params)\n")
            f.write(f"{'═' * 80}\n\n")
            b = baseline[0]
            f.write(f"  Config: {b['config_name']}\n")
            f.write(f"  Trades: {b['total_trades']:,}  WR: {b['win_rate']:.1%}  "
                    f"Total R: {b['total_r']:+.1f}  Avg R: {b['avg_r']:+.4f}\n")
            f.write(f"  PF: {b['profit_factor']:.3f}  Sharpe(R): {b['sharpe_r']:.3f}  "
                    f"Kelly f*: {b['kelly_f']:.3f}\n")
            f.write(f"  Avg Win: {b['avg_win_r']:+.3f}R  Avg Loss: {b['avg_loss_r']:.3f}R\n")
            f.write(f"  Equity @ 12% risk: ${b['equity_12']:,.2f}  DD: {b['dd_12']:.1%}\n")
            er = b.get("exit_reasons", {})
            f.write(f"  Exits: SL={er.get('sl',0)} TP={er.get('tp',0)} "
                    f"Trail={er.get('trail',0)} SessEnd={er.get('session_end',0)}\n")

            # How many configs beat baseline?
            baseline_r = b['total_r']
            better = sum(1 for a in aggregates if a['total_r'] > baseline_r)
            f.write(f"\n  Configs that beat baseline Total R: {better}/{len(aggregates)}\n")

        # ─── Recommendations ───
        f.write(f"\n\n{'═' * 80}\n")
        f.write("  RECOMMENDATIONS\n")
        f.write(f"{'═' * 80}\n\n")

        # Find configs that are both high Sharpe AND high total R AND reasonable Kelly
        robust = [a for a in aggregates
                  if a["total_trades"] >= 200
                  and a["sharpe_r"] > 0.05
                  and a["profit_factor"] > 1.05
                  and a["kelly_f"] > 0.01]
        robust.sort(key=lambda x: x["sharpe_r"] * x["total_r"], reverse=True)

        if robust:
            f.write("  ROBUST CONFIGS (≥200 trades, Sharpe>0.05, PF>1.05, Kelly>1%):\n\n")
            for i, a in enumerate(robust[:10], 1):
                f.write(f"    #{i} {a['config_name']}\n")
                f.write(f"       {a['total_trades']}t  WR={a['win_rate']:.1%}  "
                        f"R={a['total_r']:+.1f}  PF={a['profit_factor']:.3f}  "
                        f"Sharpe={a['sharpe_r']:.3f}  Kelly={a['kelly_f']:.3f}\n")
                f.write(f"       Equity(12%): ${a['equity_12']:,.2f}  "
                        f"DD={a['dd_12']:.1%}  "
                        f"x10 in {a['trades_to_x10']} trades\n\n")
        else:
            f.write("  No configs met all robustness criteria.\n")

    print(f"  → {path}")


def print_top_configs(aggregates: List[Dict]):
    """Print top 20 configs to stdout."""
    print("\n" + "=" * 80)
    print("  TOP 20 CONFIGS BY TOTAL R")
    print("=" * 80)

    by_r = sorted(aggregates, key=lambda x: x["total_r"], reverse=True)
    for i, a in enumerate(by_r[:20], 1):
        trail_label = ""
        if a["trail_enabled"]:
            trail_label = f" trail(a={a['trail_act_r']},d={a['trail_dist_r']})"
        filt_label = " +filt" if a["filtered"] else ""
        config_desc = f"{a['config_name']}{trail_label}{filt_label}"

        mark = " ★" if a["config_name"] == "LIVE_BASELINE" else ""
        print(f"  #{i:2d} {config_desc:<45s}  "
              f"{a['total_trades']:>5d}t  WR={a['win_rate']:>5.1%}  "
              f"R={a['total_r']:>+7.1f}  PF={a['profit_factor']:>5.3f}  "
              f"Sharpe={a['sharpe_r']:>6.3f}{mark}")

    print("\n" + "=" * 80)
    print("  TOP 10 CONFIGS BY SHARPE (≥100 trades)")
    print("=" * 80)
    by_sharpe = sorted([a for a in aggregates if a["total_trades"] >= 100],
                        key=lambda x: x["sharpe_r"], reverse=True)
    for i, a in enumerate(by_sharpe[:10], 1):
        mark = " ★" if a["config_name"] == "LIVE_BASELINE" else ""
        print(f"  #{i:2d} {a['config_name']:<45s}  "
              f"{a['total_trades']:>5d}t  WR={a['win_rate']:>5.1%}  "
              f"R={a['total_r']:>+7.1f}  PF={a['profit_factor']:>5.3f}  "
              f"Sharpe={a['sharpe_r']:>6.3f}{mark}")

    # Live baseline position
    baseline = [a for a in aggregates if a["config_name"] == "LIVE_BASELINE"]
    if baseline:
        b = baseline[0]
        rank_r = sum(1 for a in aggregates if a["total_r"] > b["total_r"]) + 1
        rank_s = sum(1 for a in aggregates if a["sharpe_r"] > b["sharpe_r"]) + 1
        print(f"\n  LIVE BASELINE: rank #{rank_r} by Total R, #{rank_s} by Sharpe")
        print(f"  Trades: {b['total_trades']:,}  WR: {b['win_rate']:.1%}  "
              f"Total R: {b['total_r']:+.1f}  Sharpe: {b['sharpe_r']:.3f}")

    print()


# ═══════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="FCB Mega Parameter Sweep")
    parser.add_argument("--full", action="store_true", help="Run full grid (~400 configs)")
    parser.add_argument("--focused", action="store_true", help="Run focused grid (~25 configs)")
    parser.add_argument("--finetune", action="store_true", help="Run fine-tuned grid (~110 configs)")
    parser.add_argument("--ultra", action="store_true", help="Run ultra-fine grid (~130 configs, sub-0.15R boundary)")
    args = parser.parse_args()

    if args.ultra:
        run_sweep(grid_name="ultra")
    elif args.finetune:
        run_sweep(grid_name="finetune")
    elif args.full:
        run_sweep(grid_name="full")
    else:
        run_sweep(grid_name="focused")
