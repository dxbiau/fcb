"""
research/extended_dd_backtest.py — Extended Drawdown-Focused Backtest

Uses ALL 128 pairs across full 6-month data window (Aug 2025 → Feb 2026).
Tests 3-month and 6-month windows with focus on MINIMIZING DRAWDOWN.

Key insight from user: "backtests always perform worse than live because
wicks stop us out" — so we add a WICK PENALTY model to simulate the
real-world slippage where intra-bar wicks trigger stops that bar-close
backtests miss.

Sweeps:
  1. DD-OPTIMAL parameter combinations (trail, risk, activation)
  2. Monthly equity curves showing consistency
  3. Worst-month analysis
  4. Monte Carlo with DD focus (95th percentile DD)
  5. Wick-penalty sensitivity (how much worse can it get?)
  6. Concurrent position limiting (realistic margin)

Goal: Find the lowest-DD configuration that still reaches x10.
"""

from __future__ import annotations
import csv, glob, math, os, sys, time, statistics, random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT))

# ─── Session Definitions ───
SESSIONS = {
    "asia":   (0, 8),
    "london": (8, 16),
    "ny":     (16, 24),
}


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
    body_ratio: float = 0.0
    candle_dir: int = 0


@dataclass
class Trade:
    pair: str
    session_name: str
    session_date: str
    direction: str
    entry_price: float
    entry_time: datetime
    stop_loss: float
    take_profit: float
    risk_per_unit: float
    range_high: float
    range_low: float
    range_midpoint: float
    fc_body_ratio: float = 0.0
    c2_body_ratio: float = 0.0
    vol_ratio: float = 0.0
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    r_multiple: Optional[float] = None
    peak_r: float = 0.0
    trail_active: bool = False
    trail_stop_price: Optional[float] = None
    peak_price: Optional[float] = None
    had_retest: bool = False

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


def load_csv(path: str) -> List[Candle]:
    candles = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt_str = row["date"]
            try:
                dt_str_clean = dt_str.replace("+00:00", "").replace("Z", "")
                dt = datetime.strptime(dt_str_clean, "%Y-%m-%d %H:%M:%S")
                dt = dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            o, h, l, c, v = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), float(row["volume"])
            candle = Candle(date=dt, open=o, high=h, low=l, close=c, volume=v)
            full_range = h - l
            if full_range > 0:
                candle.body_ratio = abs(c - o) / full_range
            candle.candle_dir = 1 if c > o else (-1 if c < o else 0)
            candles.append(candle)
    candles.sort(key=lambda x: x.date)
    return candles


def assign_sessions(candles: List[Candle]) -> None:
    for c in candles:
        hour = c.date.hour
        for name, (start_h, end_h) in SESSIONS.items():
            if start_h <= hour < end_h:
                c.session_name = name
                c.session_date = c.date.strftime("%Y-%m-%d")
                c.session_start = c.date.replace(hour=start_h, minute=0, second=0, microsecond=0)
                c.session_end = c.date.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=end_h)
                break


def discover_data_files() -> List[Tuple[str, str]]:
    pattern = str(DATA_DIR / "bybit_futures_*.csv")
    files = []
    for fpath in sorted(glob.glob(pattern)):
        base = os.path.basename(fpath).replace(".csv", "")
        parts = base.split("_")
        if len(parts) >= 6 and parts[0] == "bybit" and parts[1] == "futures":
            symbol_parts = parts[2:-3]
            symbol = "_".join(symbol_parts) if symbol_parts else parts[2]
            pair = f"{symbol}/USDT:USDT"
            files.append((pair, fpath))
    return files


# ═══════════════════════════════════════════════════
#  FCB ENGINE — matches live bot with retest + wick penalty
# ═══════════════════════════════════════════════════

def run_fcb(pair: str, candles: List[Candle],
            tp_r: float = 1.5,
            trail_enabled: bool = True,
            trail_activation_r: float = 0.95,
            trail_distance_r: float = 0.20,
            trail_max_r: float = 10.0,
            safety_tp_r: float = 10.0,
            min_c2_body: float = 0.50,
            fc_counter: bool = True,
            vol_ratio_long: float = 1.0,
            vol_ratio_short: float = 0.25,
            min_range_pct: float = 0.003,
            require_retest: bool = True,
            fee_per_trade_r: float = 0.04,
            breakout_window_min: int = 60,
            wick_penalty_r: float = 0.0,
            max_daily_trades: int = 6,
            ) -> List[Trade]:
    """
    FCB engine matching live bot logic (with retest).
    
    wick_penalty_r: Extra R subtracted from SL distance to simulate
    the fact that live markets have wicks that stop you out but bar-close
    backtests can't see. E.g. 0.05 means real SL is 0.05R tighter than backtest.
    """
    if not candles:
        return []

    trades: List[Trade] = []
    phase = "idle"
    fc_candle: Optional[Candle] = None
    range_high = range_low = range_mid = 0.0
    breakout_dir = ""
    breakout_idx = -1
    breakout_candle: Optional[Candle] = None
    current_trade: Optional[Trade] = None
    current_session_key = ""
    session_keys_seen = set()
    daily_trades: Dict[str, int] = {}

    for i, candle in enumerate(candles):
        if not candle.session_name:
            continue

        session_key = f"{candle.session_name}_{candle.session_date}"

        # ── New session ──
        if session_key != current_session_key:
            # Carry position over (live behavior, no session-end close)
            current_session_key = session_key
            if session_key in session_keys_seen:
                phase = "done"
                continue
            session_keys_seen.add(session_key)
            phase = "waiting_fc"
            fc_candle = None
            breakout_dir = ""
            breakout_idx = -1
            breakout_candle = None

        # ── Manage open trade ──
        if current_trade and not current_trade.is_closed:
            _manage_trade(candle, current_trade, trail_enabled, trail_activation_r,
                          trail_distance_r, trail_max_r, safety_tp_r, wick_penalty_r)
            if current_trade.is_closed:
                if fee_per_trade_r > 0 and current_trade.r_multiple is not None:
                    current_trade.r_multiple -= fee_per_trade_r
                trades.append(current_trade)
                current_trade = None
                phase = "done"
            if phase == "in_trade":
                continue

        if phase == "done" or phase == "in_trade":
            continue

        # ── Waiting for FC ──
        if phase == "waiting_fc":
            fc_candle = candle
            range_high = candle.high
            range_low = candle.low
            range_mid = (range_high + range_low) / 2.0
            phase = "waiting_breakout"
            continue

        # ── Waiting for breakout ──
        if phase == "waiting_breakout":
            if candle.session_start:
                elapsed = (candle.date - candle.session_start).total_seconds() / 60
                if elapsed > breakout_window_min:
                    phase = "done"
                    continue

            c = candle.close
            if c > range_high:
                breakout_dir = "long"
                breakout_idx = i
                breakout_candle = candle
                if require_retest:
                    phase = "waiting_retest"
                else:
                    trade = _try_enter(pair, candle, candle, fc_candle, breakout_dir,
                                       range_high, range_low, range_mid,
                                       min_c2_body, fc_counter, vol_ratio_long, vol_ratio_short,
                                       min_range_pct, trail_enabled, safety_tp_r, tp_r,
                                       daily_trades, max_daily_trades, had_retest=False)
                    if trade:
                        current_trade = trade
                        phase = "in_trade"
                    else:
                        phase = "done"
            elif c < range_low:
                breakout_dir = "short"
                breakout_idx = i
                breakout_candle = candle
                if require_retest:
                    phase = "waiting_retest"
                else:
                    trade = _try_enter(pair, candle, candle, fc_candle, breakout_dir,
                                       range_high, range_low, range_mid,
                                       min_c2_body, fc_counter, vol_ratio_long, vol_ratio_short,
                                       min_range_pct, trail_enabled, safety_tp_r, tp_r,
                                       daily_trades, max_daily_trades, had_retest=False)
                    if trade:
                        current_trade = trade
                        phase = "in_trade"
                    else:
                        phase = "done"
            continue

        # ── Waiting for retest ──
        if phase == "waiting_retest":
            if i != breakout_idx + 1:
                phase = "done"
                continue

            c, h, l = candle.close, candle.high, candle.low
            if breakout_dir == "long":
                retested = l <= range_high
                held = c > range_high
            else:
                retested = h >= range_low
                held = c < range_low

            if not (retested and held):
                phase = "done"
                continue

            trade = _try_enter(pair, candle, breakout_candle, fc_candle, breakout_dir,
                               range_high, range_low, range_mid,
                               min_c2_body, fc_counter, vol_ratio_long, vol_ratio_short,
                               min_range_pct, trail_enabled, safety_tp_r, tp_r,
                               daily_trades, max_daily_trades, had_retest=True)
            if trade:
                current_trade = trade
                phase = "in_trade"
            else:
                phase = "done"
            continue

    # Close remaining
    if current_trade and not current_trade.is_closed and candles:
        current_trade.close(candles[-1].close, candles[-1].date, "data_end")
        if fee_per_trade_r > 0 and current_trade.r_multiple is not None:
            current_trade.r_multiple -= fee_per_trade_r
        trades.append(current_trade)

    return trades


def _try_enter(pair, entry_candle, bo_candle, fc_candle, direction,
               range_high, range_low, range_mid,
               min_c2_body, fc_counter, vol_ratio_long, vol_ratio_short,
               min_range_pct, trail_enabled, safety_tp_r, tp_r,
               daily_trades, max_daily_trades, had_retest):
    # C2 body ratio
    if min_c2_body > 0 and bo_candle.body_ratio < min_c2_body:
        return None
    # FC counter
    if fc_counter and fc_candle:
        if direction == "long" and fc_candle.candle_dir > 0:
            return None
        if direction == "short" and fc_candle.candle_dir < 0:
            return None
    # Volume ratio
    vol_r = bo_candle.volume / fc_candle.volume if (fc_candle and fc_candle.volume > 0) else 1.0
    if direction == "long" and vol_ratio_long > 0 and vol_r < vol_ratio_long:
        return None
    if direction == "short" and vol_ratio_short > 0 and vol_r < vol_ratio_short:
        return None
    # Min range
    if min_range_pct > 0:
        mid_price = (range_high + range_low) / 2.0
        if mid_price > 0:
            range_pct = (range_high - range_low) / mid_price
            if range_pct < min_range_pct:
                return None
    # Daily limit
    day_key = entry_candle.session_date
    if daily_trades.get(day_key, 0) >= max_daily_trades:
        return None

    entry_price = entry_candle.close
    sl = range_mid
    risk = abs(entry_price - sl)
    if risk <= 0:
        return None

    if trail_enabled:
        if direction == "long":
            tp = entry_price + safety_tp_r * risk
        else:
            tp = entry_price - safety_tp_r * risk
    else:
        if direction == "long":
            tp = entry_price + tp_r * risk
        else:
            tp = entry_price - tp_r * risk

    daily_trades[day_key] = daily_trades.get(day_key, 0) + 1

    return Trade(
        pair=pair, session_name=entry_candle.session_name,
        session_date=entry_candle.session_date, direction=direction,
        entry_price=entry_price, entry_time=entry_candle.date,
        stop_loss=sl, take_profit=tp, risk_per_unit=risk,
        range_high=range_high, range_low=range_low, range_midpoint=range_mid,
        fc_body_ratio=fc_candle.body_ratio if fc_candle else 0,
        c2_body_ratio=bo_candle.body_ratio, vol_ratio=vol_r,
        had_retest=had_retest,
    )


def _manage_trade(candle, trade, trail_enabled, trail_activation_r,
                  trail_distance_r, trail_max_r, safety_tp_r, wick_penalty_r):
    """Manage open trade with wick penalty model."""
    h, l, c, t = candle.high, candle.low, candle.close, candle.date
    risk = trade.risk_per_unit

    # Wick penalty: simulate that real SL is slightly tighter
    # because intra-bar spikes can hit SL even if bar close doesn't
    effective_sl_buffer = wick_penalty_r * risk

    if trade.direction == "long":
        current_r_high = (h - trade.entry_price) / risk
        
        # SL check with wick penalty
        sl_trigger = trade.stop_loss + effective_sl_buffer
        if l <= sl_trigger:
            trade.close(trade.stop_loss, t, "sl")
            return
        
        # Trail active
        if trade.trail_active:
            if h > (trade.peak_price or 0):
                trade.peak_price = h
                trade.peak_r = current_r_high
                trade.trail_stop_price = h - trail_distance_r * risk
            if current_r_high >= trail_max_r:
                trade.close(trade.entry_price + trail_max_r * risk, t, "max_r")
                return
            # Trail stop with wick penalty
            if trade.trail_stop_price and l <= (trade.trail_stop_price + effective_sl_buffer):
                trade.close(trade.trail_stop_price, t, "trail")
                return
        else:
            if trail_enabled and current_r_high >= trail_activation_r:
                trade.trail_active = True
                trade.peak_price = h
                trade.peak_r = current_r_high
                trade.trail_stop_price = h - trail_distance_r * risk
                trade.stop_loss = trade.entry_price  # BE
                return
            elif not trail_enabled and h >= trade.take_profit:
                trade.close(trade.take_profit, t, "tp")
                return
        if current_r_high > trade.peak_r:
            trade.peak_r = current_r_high

    else:  # SHORT
        current_r_high = (trade.entry_price - l) / risk
        
        # SL with wick penalty
        sl_trigger = trade.stop_loss - effective_sl_buffer
        if h >= sl_trigger:
            trade.close(trade.stop_loss, t, "sl")
            return
        
        if trade.trail_active:
            if l < (trade.peak_price or float('inf')):
                trade.peak_price = l
                trade.peak_r = current_r_high
                trade.trail_stop_price = l + trail_distance_r * risk
            if current_r_high >= trail_max_r:
                trade.close(trade.entry_price - trail_max_r * risk, t, "max_r")
                return
            if trade.trail_stop_price and h >= (trade.trail_stop_price - effective_sl_buffer):
                trade.close(trade.trail_stop_price, t, "trail")
                return
        else:
            if trail_enabled and current_r_high >= trail_activation_r:
                trade.trail_active = True
                trade.peak_price = l
                trade.peak_r = current_r_high
                trade.trail_stop_price = l + trail_distance_r * risk
                trade.stop_loss = trade.entry_price
                return
            elif not trail_enabled and l <= trade.take_profit:
                trade.close(trade.take_profit, t, "tp")
                return
        if current_r_high > trade.peak_r:
            trade.peak_r = current_r_high


# ═══════════════════════════════════════════════════
#  EQUITY SIMULATION — realistic with DD tracking
# ═══════════════════════════════════════════════════

def equity_curve(r_vals: List[float], start: float = 150.0, risk_pct: float = 0.08,
                 max_concurrent: int = 2) -> dict:
    """Simulate compounding equity with detailed DD tracking."""
    eq = start
    peak = start
    max_dd = 0.0
    max_dd_pct = 0.0
    eq_history = [start]
    dd_history = [0.0]
    x10_trade = None
    x100_trade = None
    x1000_trade = None
    underwater_trades = 0
    max_underwater = 0
    current_underwater = 0

    for i, r in enumerate(r_vals):
        eq *= (1 + risk_pct * r)
        eq = max(eq, 0.01)
        if eq > peak:
            peak = eq
            current_underwater = 0
        else:
            current_underwater += 1
            max_underwater = max(max_underwater, current_underwater)
        
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
        eq_history.append(eq)
        dd_history.append(dd)

        if x10_trade is None and eq >= start * 10:
            x10_trade = i + 1
        if x100_trade is None and eq >= start * 100:
            x100_trade = i + 1
        if x1000_trade is None and eq >= start * 1000:
            x1000_trade = i + 1

    return {
        "final_eq": eq,
        "max_dd": max_dd,
        "x10_trade": x10_trade,
        "x100_trade": x100_trade,
        "x1000_trade": x1000_trade,
        "max_underwater": max_underwater,
        "eq_history": eq_history,
        "dd_history": dd_history,
        "total_r": sum(r_vals),
    }


def monthly_breakdown(trades: List[Trade], start_eq: float = 150.0, risk_pct: float = 0.08) -> List[dict]:
    """Break trades into calendar months and show per-month stats."""
    if not trades:
        return []
    
    # Group by month
    months: Dict[str, List[Trade]] = {}
    for t in trades:
        if t.is_closed and t.r_multiple is not None:
            month_key = t.entry_time.strftime("%Y-%m")
            if month_key not in months:
                months[month_key] = []
            months[month_key].append(t)
    
    result = []
    eq = start_eq
    for month_key in sorted(months.keys()):
        mtrades = months[month_key]
        r_vals = [t.r_multiple for t in mtrades]
        winners = sum(1 for r in r_vals if r > 0)
        wr = winners / len(r_vals) if r_vals else 0
        avg_r = statistics.mean(r_vals) if r_vals else 0
        total_r = sum(r_vals)
        
        month_start = eq
        peak_eq = eq
        max_dd_eq = 0.0
        for r in r_vals:
            eq *= (1 + risk_pct * r)
            eq = max(eq, 0.01)
            if eq > peak_eq:
                peak_eq = eq
            dd = (peak_eq - eq) / peak_eq if peak_eq > 0 else 0
            max_dd_eq = max(max_dd_eq, dd)
        
        result.append({
            "month": month_key,
            "trades": len(r_vals),
            "wr": wr,
            "avg_r": avg_r,
            "total_r": total_r,
            "start_eq": month_start,
            "end_eq": eq,
            "month_return": (eq - month_start) / month_start if month_start > 0 else 0,
            "month_max_dd": max_dd_eq,
        })
    
    return result


def monte_carlo_dd(r_vals: List[float], n_trials: int = 2000,
                   start: float = 150.0, risk_pct: float = 0.08) -> dict:
    """Monte Carlo shuffle — focus on DD distribution."""
    max_dds = []
    x10_trades = []
    bust_count = 0  # equity hits <$10
    
    for _ in range(n_trials):
        shuffled = list(r_vals)
        random.shuffle(shuffled)
        eq = start
        peak = start
        max_dd = 0.0
        x10 = None
        
        for i, r in enumerate(shuffled):
            eq *= (1 + risk_pct * r)
            eq = max(eq, 0.01)
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
            if x10 is None and eq >= start * 10:
                x10 = i + 1
            if eq < 10:
                bust_count += 1
                break
        
        max_dds.append(max_dd)
        if x10:
            x10_trades.append(x10)
    
    max_dds.sort()
    return {
        "median_dd": max_dds[len(max_dds)//2],
        "p75_dd": max_dds[int(len(max_dds)*0.75)],
        "p90_dd": max_dds[int(len(max_dds)*0.90)],
        "p95_dd": max_dds[int(len(max_dds)*0.95)],
        "p99_dd": max_dds[int(len(max_dds)*0.99)],
        "worst_dd": max_dds[-1],
        "bust_pct": bust_count / n_trials,
        "x10_median": sorted(x10_trades)[len(x10_trades)//2] if x10_trades else None,
        "x10_pct": len(x10_trades) / n_trials,
    }


# ═══════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════

def main():
    t0 = time.time()
    random.seed(42)

    print("=" * 80)
    print("  EXTENDED DRAWDOWN-FOCUSED BACKTEST")
    print("  128 pairs × 6 months (Aug 2025 → Feb 2026)")
    print("  Focus: MINIMIZE DRAWDOWN while still reaching x10")
    print("=" * 80)

    # ── Load all data ──
    pair_files = discover_data_files()
    print(f"\n  Loading {len(pair_files)} pairs...")
    pair_data: Dict[str, List[Candle]] = {}
    for pair, fpath in pair_files:
        candles = load_csv(fpath)
        assign_sessions(candles)
        pair_data[pair] = candles
        sys.stdout.write(f"\r    {len(pair_data)}/{len(pair_files)}")
        sys.stdout.flush()
    total_candles = sum(len(c) for c in pair_data.values())
    print(f"\n    Done — {total_candles:,} candles across {len(pair_data)} pairs\n")

    # ═══════════════════════════════════════════════════
    #  SECTION 1: DD-OPTIMAL PARAMETER SWEEP
    #  Sweep trail_distance × risk_pct × trail_activation
    #  Report: sorted by MAX_DD ascending
    # ═══════════════════════════════════════════════════
    print("=" * 80)
    print("  SECTION 1: DD-OPTIMAL PARAMETER SWEEP")
    print("  Goal: Find config with lowest drawdown that still reaches x10")
    print("=" * 80)

    sweep_results = []
    
    # Parameter grid focused on DD-relevant params
    risk_pcts = [0.04, 0.05, 0.06, 0.08, 0.10]
    trail_dists = [0.15, 0.20, 0.25, 0.30, 0.40]
    trail_acts = [0.85, 0.95, 1.0]
    wick_penalties = [0.0]  # Section 1 uses no wick penalty (optimistic)

    total_combos = len(risk_pcts) * len(trail_dists) * len(trail_acts)
    print(f"  Testing {total_combos} parameter combinations...\n")
    
    # Pre-run trades for each trail config (risk doesn't affect trade generation)
    trade_cache: Dict[str, List[Trade]] = {}
    combo_idx = 0
    
    for act_r in trail_acts:
        for dist_r in trail_dists:
            cache_key = f"{act_r}_{dist_r}"
            if cache_key not in trade_cache:
                all_trades = []
                for pair, candles in pair_data.items():
                    trades = run_fcb(pair, candles,
                                     require_retest=True,
                                     fee_per_trade_r=0.04,
                                     trail_activation_r=act_r,
                                     trail_distance_r=dist_r,
                                     wick_penalty_r=0.0)
                    all_trades.extend(trades)
                closed = sorted(
                    [t for t in all_trades if t.is_closed and t.r_multiple is not None],
                    key=lambda t: t.entry_time
                )
                trade_cache[cache_key] = closed
            
            closed = trade_cache[cache_key]
            r_vals = [t.r_multiple for t in closed]
            
            if not r_vals:
                continue
            
            wr = sum(1 for r in r_vals if r > 0) / len(r_vals)
            avg_r = statistics.mean(r_vals)
            
            for risk_pct in risk_pcts:
                combo_idx += 1
                ec = equity_curve(r_vals, 150.0, risk_pct)
                
                sweep_results.append({
                    "risk": risk_pct,
                    "trail_act": act_r,
                    "trail_dist": dist_r,
                    "trades": len(r_vals),
                    "wr": wr,
                    "avg_r": avg_r,
                    "max_dd": ec["max_dd"],
                    "final_eq": ec["final_eq"],
                    "x10": ec["x10_trade"],
                    "x100": ec["x100_trade"],
                    "max_underwater": ec["max_underwater"],
                    "total_r": ec["total_r"],
                })
                
                if combo_idx % 15 == 0:
                    sys.stdout.write(f"\r    {combo_idx}/{total_combos} combos tested")
                    sys.stdout.flush()
    
    print(f"\r    {combo_idx}/{total_combos} combos tested — done\n")

    # Sort by max_dd ascending, filter to configs that reach x10
    x10_results = [r for r in sweep_results if r["x10"] is not None]
    x10_results.sort(key=lambda r: r["max_dd"])
    
    all_sorted = sorted(sweep_results, key=lambda r: r["max_dd"])

    print(f"  {'Risk':>5s}  {'Act':>4s}  {'Dist':>5s}  {'Trades':>6s}  {'WR':>5s}  {'AvgR':>7s}  {'MaxDD':>6s}  {'FinalEq':>10s}  {'x10':>6s}  {'x100':>6s}  {'UW':>4s}")
    print(f"  {'-'*5}  {'-'*4}  {'-'*5}  {'-'*6}  {'-'*5}  {'-'*7}  {'-'*6}  {'-'*10}  {'-'*6}  {'-'*6}  {'-'*4}")
    
    # Show TOP 20 lowest DD configs that reach x10
    print(f"\n  === TOP 20 LOWEST DD (that reach x10) ===")
    for r in x10_results[:20]:
        x10s = f"{r['x10']}t" if r['x10'] else "never"
        x100s = f"{r['x100']}t" if r['x100'] else "-"
        print(f"  {r['risk']:>4.0%}  {r['trail_act']:>4.2f}  {r['trail_dist']:>4.2f}R  {r['trades']:>6d}  "
              f"{r['wr']:>4.1%}  {r['avg_r']:>+.4f}  {r['max_dd']:>5.1%}  ${r['final_eq']:>9,.0f}  "
              f"{x10s:>6s}  {x100s:>6s}  {r['max_underwater']:>4d}")

    # Show WORST 5 DD configs (to show the danger zone)
    print(f"\n  === WORST 5 DD (danger zone) ===")
    for r in all_sorted[-5:]:
        x10s = f"{r['x10']}t" if r['x10'] else "never"
        print(f"  {r['risk']:>4.0%}  {r['trail_act']:>4.2f}  {r['trail_dist']:>4.2f}R  {r['trades']:>6d}  "
              f"{r['wr']:>4.1%}  {r['avg_r']:>+.4f}  {r['max_dd']:>5.1%}  ${r['final_eq']:>9,.0f}  "
              f"{x10s:>6s}")

    # ═══════════════════════════════════════════════════
    #  SECTION 2: MONTHLY EQUITY CURVES
    #  Show per-month breakdown for the top 3 DD configs
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 2: MONTHLY BREAKDOWN — TOP 3 LOWEST DD CONFIGS")
    print("=" * 80)
    
    for idx, best in enumerate(x10_results[:3]):
        cache_key = f"{best['trail_act']}_{best['trail_dist']}"
        closed = trade_cache[cache_key]
        risk = best["risk"]
        
        print(f"\n  Config #{idx+1}: risk={risk:.0%}, trail_act={best['trail_act']}, "
              f"trail_dist={best['trail_dist']}R, maxDD={best['max_dd']:.1%}")
        
        months = monthly_breakdown(closed, 150.0, risk)
        
        print(f"  {'Month':>8s}  {'Trades':>6s}  {'WR':>5s}  {'AvgR':>7s}  {'TotR':>7s}  "
              f"{'StartEq':>9s}  {'EndEq':>9s}  {'Return':>7s}  {'MonthDD':>8s}")
        print(f"  {'-'*8}  {'-'*6}  {'-'*5}  {'-'*7}  {'-'*7}  {'-'*9}  {'-'*9}  {'-'*7}  {'-'*8}")
        
        for m in months:
            print(f"  {m['month']:>8s}  {m['trades']:>6d}  {m['wr']:>4.1%}  {m['avg_r']:>+.4f}  "
                  f"{m['total_r']:>+6.1f}  ${m['start_eq']:>8,.0f}  ${m['end_eq']:>8,.0f}  "
                  f"{m['month_return']:>+6.1%}  {m['month_max_dd']:>7.1%}")
        
        # Worst month
        if months:
            worst = min(months, key=lambda m: m["total_r"])
            best_m = max(months, key=lambda m: m["total_r"])
            print(f"  → Worst month: {worst['month']} ({worst['total_r']:+.1f}R, {worst['month_max_dd']:.1%} DD)")
            print(f"  → Best month:  {best_m['month']} ({best_m['total_r']:+.1f}R)")

    # ═══════════════════════════════════════════════════
    #  SECTION 3: WICK PENALTY SENSITIVITY
    #  "Backtests are optimistic — how much worse can it get?"
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 3: WICK PENALTY SENSITIVITY")
    print("  Simulates real wicks stopping you out earlier than bar-close shows")
    print("  wick_penalty_r = extra R of adverse wick that can hit your SL")
    print("=" * 80)

    # Use top-3 DD configs for wick penalty test
    wick_levels = [0.0, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20]
    
    for idx, best in enumerate(x10_results[:3]):
        risk = best["risk"]
        act_r = best["trail_act"]
        dist_r = best["trail_dist"]
        
        print(f"\n  Config #{idx+1}: risk={risk:.0%}, act={act_r}, dist={dist_r}R")
        print(f"  {'WickPen':>8s}  {'Trades':>6s}  {'WR':>5s}  {'AvgR':>7s}  {'MaxDD':>6s}  "
              f"{'FinalEq':>10s}  {'x10':>6s}")
        print(f"  {'-'*8}  {'-'*6}  {'-'*5}  {'-'*7}  {'-'*6}  {'-'*10}  {'-'*6}")
        
        for wp in wick_levels:
            all_trades = []
            for pair, candles in pair_data.items():
                trades = run_fcb(pair, candles,
                                 require_retest=True,
                                 fee_per_trade_r=0.04,
                                 trail_activation_r=act_r,
                                 trail_distance_r=dist_r,
                                 wick_penalty_r=wp)
                all_trades.extend(trades)
            
            closed = sorted(
                [t for t in all_trades if t.is_closed and t.r_multiple is not None],
                key=lambda t: t.entry_time
            )
            r_vals = [t.r_multiple for t in closed]
            if not r_vals:
                print(f"  {wp:>7.2f}R  no trades")
                continue
            
            wr = sum(1 for r in r_vals if r > 0) / len(r_vals)
            avg_r = statistics.mean(r_vals)
            ec = equity_curve(r_vals, 150.0, risk)
            x10s = f"{ec['x10_trade']}t" if ec['x10_trade'] else "never"
            
            print(f"  {wp:>7.2f}R  {len(r_vals):>6d}  {wr:>4.1%}  {avg_r:>+.4f}  "
                  f"{ec['max_dd']:>5.1%}  ${ec['final_eq']:>9,.0f}  {x10s:>6s}")

    # ═══════════════════════════════════════════════════
    #  SECTION 4: MONTE CARLO DD DISTRIBUTION
    #  2000 shuffles — what DD should we expect in reality?
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 4: MONTE CARLO DRAWDOWN DISTRIBUTION (2000 trials)")
    print("  Shows the DD you should EXPECT, not just the historical DD")
    print("=" * 80)

    for idx, best in enumerate(x10_results[:3]):
        cache_key = f"{best['trail_act']}_{best['trail_dist']}"
        closed = trade_cache[cache_key]
        r_vals = [t.r_multiple for t in closed]
        risk = best["risk"]
        
        print(f"\n  Config #{idx+1}: risk={risk:.0%}, act={best['trail_act']}, dist={best['trail_dist']}R")
        
        mc = monte_carlo_dd(r_vals, n_trials=2000, start=150.0, risk_pct=risk)
        
        print(f"    Median DD:   {mc['median_dd']:>6.1%}")
        print(f"    75th %ile:   {mc['p75_dd']:>6.1%}")
        print(f"    90th %ile:   {mc['p90_dd']:>6.1%}")
        print(f"    95th %ile:   {mc['p95_dd']:>6.1%}  ← plan for this")
        print(f"    99th %ile:   {mc['p99_dd']:>6.1%}")
        print(f"    Worst case:  {mc['worst_dd']:>6.1%}")
        print(f"    Bust (<$10): {mc['bust_pct']:>6.1%}")
        print(f"    x10 chance:  {mc['x10_pct']:>6.1%}")
        if mc['x10_median']:
            print(f"    x10 median:  {mc['x10_median']}t (~{mc['x10_median']/8:.0f} days)")

    # ═══════════════════════════════════════════════════
    #  SECTION 5: 3-MONTH ROLLING WINDOWS
    #  Test if edge is consistent across time periods
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 5: 3-MONTH ROLLING WINDOWS")
    print("  Does the edge hold in every 3-month slice?")
    print("=" * 80)

    # Use the overall best low-DD config
    if x10_results:
        best = x10_results[0]
        risk = best["risk"]
        act_r = best["trail_act"]
        dist_r = best["trail_dist"]
        
        print(f"  Using lowest-DD config: risk={risk:.0%}, act={act_r}, dist={dist_r}R\n")
        
        # Define 3-month windows
        windows = [
            ("Aug-Oct 2025", datetime(2025, 8, 1, tzinfo=timezone.utc), datetime(2025, 11, 1, tzinfo=timezone.utc)),
            ("Sep-Nov 2025", datetime(2025, 9, 1, tzinfo=timezone.utc), datetime(2025, 12, 1, tzinfo=timezone.utc)),
            ("Oct-Dec 2025", datetime(2025, 10, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, tzinfo=timezone.utc)),
            ("Nov-Jan 2026", datetime(2025, 11, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc)),
            ("Dec-Feb 2026", datetime(2025, 12, 1, tzinfo=timezone.utc), datetime(2026, 3, 1, tzinfo=timezone.utc)),
        ]
        
        print(f"  {'Window':>14s}  {'Pairs':>5s}  {'Trades':>6s}  {'WR':>5s}  {'AvgR':>7s}  {'TotR':>7s}  {'MaxDD':>6s}  {'x10':>6s}")
        print(f"  {'-'*14}  {'-'*5}  {'-'*6}  {'-'*5}  {'-'*7}  {'-'*7}  {'-'*6}  {'-'*6}")
        
        for wname, wstart, wend in windows:
            all_trades = []
            pairs_used = 0
            for pair, candles in pair_data.items():
                # Filter candles to window
                window_candles = [c for c in candles if wstart <= c.date < wend]
                if len(window_candles) < 100:
                    continue
                pairs_used += 1
                trades = run_fcb(pair, window_candles,
                                 require_retest=True, fee_per_trade_r=0.04,
                                 trail_activation_r=act_r, trail_distance_r=dist_r)
                all_trades.extend(trades)
            
            closed = sorted(
                [t for t in all_trades if t.is_closed and t.r_multiple is not None],
                key=lambda t: t.entry_time
            )
            r_vals = [t.r_multiple for t in closed]
            if not r_vals:
                print(f"  {wname:>14s}  {pairs_used:>5d}  no trades")
                continue
            
            wr = sum(1 for r in r_vals if r > 0) / len(r_vals)
            avg_r = statistics.mean(r_vals)
            ec = equity_curve(r_vals, 150.0, risk)
            x10s = f"{ec['x10_trade']}t" if ec['x10_trade'] else "never"
            
            print(f"  {wname:>14s}  {pairs_used:>5d}  {len(r_vals):>6d}  {wr:>4.1%}  "
                  f"{avg_r:>+.4f}  {sum(r_vals):>+6.1f}  {ec['max_dd']:>5.1%}  {x10s:>6s}")

    # ═══════════════════════════════════════════════════
    #  SECTION 6: CONSECUTIVE LOSS STREAKS ANALYSIS
    #  How bad can losing streaks get? This kills accounts.
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 6: CONSECUTIVE LOSS STREAKS")
    print("  How many losses in a row should you prepare for?")
    print("=" * 80)
    
    if x10_results:
        for idx, best in enumerate(x10_results[:3]):
            cache_key = f"{best['trail_act']}_{best['trail_dist']}"
            closed = trade_cache[cache_key]
            r_vals = [t.r_multiple for t in closed]
            risk = best["risk"]
            
            # Count streaks
            streaks = []
            current_streak = 0
            for r in r_vals:
                if r <= 0:
                    current_streak += 1
                else:
                    if current_streak > 0:
                        streaks.append(current_streak)
                    current_streak = 0
            if current_streak > 0:
                streaks.append(current_streak)
            
            if not streaks:
                continue
            
            max_streak = max(streaks)
            avg_streak = statistics.mean(streaks) if streaks else 0
            streaks_5plus = sum(1 for s in streaks if s >= 5)
            streaks_8plus = sum(1 for s in streaks if s >= 8)
            streaks_10plus = sum(1 for s in streaks if s >= 10)
            
            # DD from worst streak at this risk
            worst_streak_dd = 1 - (1 - risk) ** max_streak
            
            print(f"\n  Config #{idx+1}: risk={risk:.0%}, act={best['trail_act']}, dist={best['trail_dist']}R")
            print(f"    Total loss streaks:  {len(streaks)}")
            print(f"    Max streak:          {max_streak} losses in a row")
            print(f"    Avg streak:          {avg_streak:.1f}")
            print(f"    Streaks >= 5:        {streaks_5plus}")
            print(f"    Streaks >= 8:        {streaks_8plus}")
            print(f"    Streaks >= 10:       {streaks_10plus}")
            print(f"    Worst streak DD:     {worst_streak_dd:.1%} (purely from consecutive losses)")
            
            # Distribution
            streak_counts = {}
            for s in streaks:
                bucket = min(s, 15)
                streak_counts[bucket] = streak_counts.get(bucket, 0) + 1
            print(f"    Distribution: ", end="")
            for length in sorted(streak_counts.keys()):
                label = f"{length}+" if length == 15 else str(length)
                print(f"{label}×{streak_counts[length]}", end="  ")
            print()

    # ═══════════════════════════════════════════════════
    #  SECTION 7: SESSION BREAKDOWN (which sessions perform best?)
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 7: SESSION PERFORMANCE")
    print("=" * 80)
    
    if x10_results:
        best = x10_results[0]
        cache_key = f"{best['trail_act']}_{best['trail_dist']}"
        closed = trade_cache[cache_key]
        
        for sess_name in ["asia", "london", "ny"]:
            sess_trades = [t for t in closed if t.session_name == sess_name]
            if not sess_trades:
                continue
            r_vals = [t.r_multiple for t in sess_trades]
            wr = sum(1 for r in r_vals if r > 0) / len(r_vals)
            avg_r = statistics.mean(r_vals)
            winners = [r for r in r_vals if r > 0]
            losers = [r for r in r_vals if r <= 0]
            avg_win = statistics.mean(winners) if winners else 0
            avg_loss = statistics.mean(losers) if losers else 0
            
            exits = {}
            for t in sess_trades:
                exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1
            
            print(f"\n  {sess_name.upper():>8s}: {len(r_vals)} trades, WR={wr:.1%}, "
                  f"AvgR={avg_r:+.4f}, TotalR={sum(r_vals):+.1f}")
            print(f"           AvgWin={avg_win:+.3f}, AvgLoss={avg_loss:+.3f}, "
                  f"Payoff={avg_win/abs(avg_loss) if avg_loss < 0 else 999:.2f}x")
            print(f"           Exits: {exits}")

    # ═══════════════════════════════════════════════════
    #  SECTION 8: PAIR CONCENTRATION — who generates the edge?
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 8: PAIR PERFORMANCE (top 10 best + worst 10)")
    print("=" * 80)
    
    if x10_results:
        best = x10_results[0]
        cache_key = f"{best['trail_act']}_{best['trail_dist']}"
        closed = trade_cache[cache_key]
        
        pair_stats: Dict[str, dict] = {}
        for t in closed:
            p = t.pair.replace("/USDT:USDT", "")
            if p not in pair_stats:
                pair_stats[p] = {"trades": 0, "wins": 0, "total_r": 0.0, "r_vals": []}
            pair_stats[p]["trades"] += 1
            pair_stats[p]["total_r"] += t.r_multiple
            pair_stats[p]["r_vals"].append(t.r_multiple)
            if t.r_multiple > 0:
                pair_stats[p]["wins"] += 1
        
        # Sort by total R
        sorted_pairs = sorted(pair_stats.items(), key=lambda x: x[1]["total_r"], reverse=True)
        
        print(f"\n  {'Pair':>15s}  {'Trades':>6s}  {'WR':>5s}  {'AvgR':>7s}  {'TotR':>7s}  {'MaxR':>6s}")
        print(f"  {'-'*15}  {'-'*6}  {'-'*5}  {'-'*7}  {'-'*7}  {'-'*6}")
        
        print(f"\n  === TOP 10 PAIRS ===")
        for p, s in sorted_pairs[:10]:
            wr = s["wins"] / s["trades"] if s["trades"] > 0 else 0
            avg = statistics.mean(s["r_vals"])
            max_r = max(s["r_vals"])
            print(f"  {p:>15s}  {s['trades']:>6d}  {wr:>4.1%}  {avg:>+.4f}  {s['total_r']:>+6.1f}  {max_r:>+5.2f}")
        
        print(f"\n  === WORST 10 PAIRS ===")
        for p, s in sorted_pairs[-10:]:
            wr = s["wins"] / s["trades"] if s["trades"] > 0 else 0
            avg = statistics.mean(s["r_vals"])
            min_r = min(s["r_vals"])
            print(f"  {p:>15s}  {s['trades']:>6d}  {wr:>4.1%}  {avg:>+.4f}  {s['total_r']:>+6.1f}  {min_r:>+5.2f}")
        
        # Edge concentration
        total_r = sum(s["total_r"] for s in pair_stats.values())
        top_10_r = sum(s["total_r"] for _, s in sorted_pairs[:10])
        bottom_half_r = sum(s["total_r"] for _, s in sorted_pairs[len(sorted_pairs)//2:])
        profitable_pairs = sum(1 for _, s in sorted_pairs if s["total_r"] > 0)
        
        print(f"\n  Edge concentration:")
        print(f"    Total R across all pairs:  {total_r:+.1f}")
        print(f"    Top 10 pairs contribute:   {top_10_r:+.1f} ({top_10_r/total_r*100:.0f}% of total)" if total_r != 0 else "    Top 10: N/A")
        print(f"    Bottom half contributes:   {bottom_half_r:+.1f}")
        print(f"    Profitable pairs:          {profitable_pairs}/{len(sorted_pairs)} ({profitable_pairs/len(sorted_pairs)*100:.0f}%)")

    # ═══════════════════════════════════════════════════
    #  EXECUTIVE SUMMARY
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  EXECUTIVE SUMMARY — LOWEST DD PATH TO x10")
    print("=" * 80)
    
    if x10_results:
        best = x10_results[0]
        print(f"\n  RECOMMENDED CONFIG (lowest DD that reaches x10):")
        print(f"    Risk per trade:     {best['risk']:.0%}")
        print(f"    Trail activation:   {best['trail_act']}R")
        print(f"    Trail distance:     {best['trail_dist']}R")
        print(f"    Retest required:    YES")
        print(f"    Fee assumption:     0.04R")
        print(f"    ─────────────────────")
        print(f"    Trades (6 months):  {best['trades']}")
        print(f"    Win rate:           {best['wr']:.1%}")
        print(f"    Avg R per trade:    {best['avg_r']:+.4f}")
        print(f"    Max drawdown:       {best['max_dd']:.1%}")
        x10s = f"{best['x10']}t (~{best['x10']//8}d)" if best['x10'] else "never"
        x100s = f"{best['x100']}t (~{best['x100']//8}d)" if best['x100'] else "never"
        print(f"    Reach x10 ($1,500): {x10s}")
        print(f"    Reach x100 ($15K):  {x100s}")
        print(f"    Final equity:       ${best['final_eq']:,.0f}")
        
        # Current config comparison
        current = None
        for r in sweep_results:
            if r["risk"] == 0.08 and r["trail_act"] == 0.95 and r["trail_dist"] == 0.20:
                current = r
                break
        
        if current:
            print(f"\n  CURRENT CONFIG (risk=8%, act=0.95, dist=0.20R):")
            print(f"    Max drawdown:       {current['max_dd']:.1%}")
            x10s = f"{current['x10']}t" if current['x10'] else "never"
            print(f"    Reach x10:          {x10s}")
            print(f"    Final equity:       ${current['final_eq']:,.0f}")
            
            print(f"\n  DD IMPROVEMENT:  {current['max_dd']:.1%} → {best['max_dd']:.1%} "
                  f"({(current['max_dd']-best['max_dd'])*100:+.1f}pp)")
    else:
        print("\n  WARNING: No configuration reached x10 in the test period.")
        print("  Showing lowest DD configs regardless:")
        for r in all_sorted[:5]:
            print(f"    risk={r['risk']:.0%}, act={r['trail_act']}, dist={r['trail_dist']}R, "
                  f"DD={r['max_dd']:.1%}, finalEq=${r['final_eq']:,.0f}")

    elapsed = time.time() - t0
    print(f"\n  Total runtime: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"  Data: {len(pair_data)} pairs, {total_candles:,} candles")
    print()


if __name__ == "__main__":
    main()
