"""
research/realistic_backtest.py — CORRECTED Extended Backtest

Previous version had CRITICAL bugs:
  1. No concurrent position limit — took ALL 1933 trades, live allows 2
  2. equity_curve() had max_concurrent param but NEVER USED it
  3. Sequential compounding of overlapping trades (should be concurrent)
  4. Cross-session carry bug (open trade overwritten by new session)

This version FIXES ALL of these:
  - Event-driven simulation: processes entry/exit events chronologically
  - Proper concurrent position tracking (MAX_CONCURRENT=2)
  - Skips entries when no slot available (just like live bot)
  - Correct compounding: equity changes on CLOSE, not on entry
  - Per-session daily trade caps
  - Realistic: only tests configs matching live bot constraints

Uses ALL 128 pairs × 6 months (Aug 2025 → Feb 2026).
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
            o, h, l, c, v = (float(row["open"]), float(row["high"]),
                             float(row["low"]), float(row["close"]),
                             float(row["volume"]))
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
                c.session_start = c.date.replace(
                    hour=start_h, minute=0, second=0, microsecond=0)
                c.session_end = (c.date.replace(hour=0, minute=0, second=0, microsecond=0)
                                 + timedelta(hours=end_h))
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
#  PER-PAIR TRADE GENERATOR
#  Generates (entry_time, exit_time, r_multiple, pair, session, direction) tuples
#  Does NOT touch equity — just produces potential trades
# ═══════════════════════════════════════════════════

def generate_pair_trades(
    pair: str, candles: List[Candle],
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
) -> List[Trade]:
    """Generate all potential trades for one pair. No equity, no position limits."""
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
    session_keys_seen: set = set()

    for i, candle in enumerate(candles):
        if not candle.session_name:
            continue

        session_key = f"{candle.session_name}_{candle.session_date}"

        # ── New session ──
        if session_key != current_session_key:
            current_session_key = session_key
            if session_key in session_keys_seen:
                # Already processed this session (shouldn't happen with sorted data)
                if current_trade and not current_trade.is_closed:
                    # Keep managing the open trade
                    pass
                else:
                    phase = "done"
                continue
            session_keys_seen.add(session_key)

            # Only reset phase if no open trade
            if current_trade and not current_trade.is_closed:
                # Trade carries over — keep managing, but DON'T look for new entries
                phase = "in_trade"
            else:
                phase = "waiting_fc"
                fc_candle = None
                breakout_dir = ""
                breakout_idx = -1
                breakout_candle = None

        # ── Manage open trade ──
        if current_trade and not current_trade.is_closed:
            _manage_trade(candle, current_trade, trail_activation_r,
                          trail_distance_r, trail_max_r, safety_tp_r,
                          wick_penalty_r)
            if current_trade.is_closed:
                if fee_per_trade_r > 0 and current_trade.r_multiple is not None:
                    current_trade.r_multiple -= fee_per_trade_r
                trades.append(current_trade)
                current_trade = None
                # After trade closes, we're done for THIS session's entry
                # (one trade per pair per session)
                phase = "done"
            continue  # Whether open or just closed, move to next candle

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
            direction = None
            if c > range_high:
                direction = "long"
            elif c < range_low:
                direction = "short"

            if direction is not None:
                breakout_dir = direction
                breakout_idx = i
                breakout_candle = candle
                if require_retest:
                    phase = "waiting_retest"
                else:
                    trade = _try_enter(
                        pair, candle, candle, fc_candle, direction,
                        range_high, range_low, range_mid,
                        min_c2_body, fc_counter, vol_ratio_long,
                        vol_ratio_short, min_range_pct, safety_tp_r, 1.5)
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

            trade = _try_enter(
                pair, candle, breakout_candle, fc_candle, breakout_dir,
                range_high, range_low, range_mid,
                min_c2_body, fc_counter, vol_ratio_long,
                vol_ratio_short, min_range_pct, safety_tp_r, 1.5)
            if trade:
                current_trade = trade
                phase = "in_trade"
            else:
                phase = "done"
            continue

    # Close remaining trade at data end
    if current_trade and not current_trade.is_closed and candles:
        current_trade.close(candles[-1].close, candles[-1].date, "data_end")
        if fee_per_trade_r > 0 and current_trade.r_multiple is not None:
            current_trade.r_multiple -= fee_per_trade_r
        trades.append(current_trade)

    return trades


def _try_enter(pair, entry_candle, bo_candle, fc_candle, direction,
               range_high, range_low, range_mid,
               min_c2_body, fc_counter, vol_ratio_long, vol_ratio_short,
               min_range_pct, safety_tp_r, tp_r):
    """Apply micro-filters. No daily limit here — simulation handles that."""
    if min_c2_body > 0 and bo_candle.body_ratio < min_c2_body:
        return None
    if fc_counter and fc_candle:
        if direction == "long" and fc_candle.candle_dir > 0:
            return None
        if direction == "short" and fc_candle.candle_dir < 0:
            return None
    vol_r = (bo_candle.volume / fc_candle.volume
             if (fc_candle and fc_candle.volume > 0) else 1.0)
    if direction == "long" and vol_ratio_long > 0 and vol_r < vol_ratio_long:
        return None
    if direction == "short" and vol_ratio_short > 0 and vol_r < vol_ratio_short:
        return None
    if min_range_pct > 0:
        mid_price = (range_high + range_low) / 2.0
        if mid_price > 0 and (range_high - range_low) / mid_price < min_range_pct:
            return None

    entry_price = entry_candle.close
    sl = range_mid
    risk = abs(entry_price - sl)
    if risk <= 0:
        return None

    if direction == "long":
        tp = entry_price + safety_tp_r * risk
    else:
        tp = entry_price - safety_tp_r * risk

    return Trade(
        pair=pair, session_name=entry_candle.session_name,
        session_date=entry_candle.session_date, direction=direction,
        entry_price=entry_price, entry_time=entry_candle.date,
        stop_loss=sl, take_profit=tp, risk_per_unit=risk,
        range_high=range_high, range_low=range_low, range_midpoint=range_mid,
        fc_body_ratio=fc_candle.body_ratio if fc_candle else 0,
        c2_body_ratio=bo_candle.body_ratio, vol_ratio=vol_r,
    )


def _manage_trade(candle, trade, trail_activation_r, trail_distance_r,
                  trail_max_r, safety_tp_r, wick_penalty_r):
    """Manage open trade with wick penalty."""
    h, l, c, t = candle.high, candle.low, candle.close, candle.date
    risk = trade.risk_per_unit
    sl_buffer = wick_penalty_r * risk

    if trade.direction == "long":
        current_r = (h - trade.entry_price) / risk

        # SL check (with wick penalty)
        if l <= trade.stop_loss + sl_buffer:
            trade.close(trade.stop_loss, t, "sl")
            return

        if trade.trail_active:
            if h > (trade.peak_price or 0):
                trade.peak_price = h
                trade.peak_r = current_r
                trade.trail_stop_price = h - trail_distance_r * risk
            if current_r >= trail_max_r:
                trade.close(trade.entry_price + trail_max_r * risk, t, "max_r")
                return
            if trade.trail_stop_price and l <= trade.trail_stop_price + sl_buffer:
                trade.close(trade.trail_stop_price, t, "trail")
                return
        else:
            if current_r >= trail_activation_r:
                trade.trail_active = True
                trade.peak_price = h
                trade.peak_r = current_r
                trade.trail_stop_price = h - trail_distance_r * risk
                trade.stop_loss = trade.entry_price  # BE
                return

        if current_r > trade.peak_r:
            trade.peak_r = current_r

    else:  # SHORT
        current_r = (trade.entry_price - l) / risk

        if h >= trade.stop_loss - sl_buffer:
            trade.close(trade.stop_loss, t, "sl")
            return

        if trade.trail_active:
            if l < (trade.peak_price or float('inf')):
                trade.peak_price = l
                trade.peak_r = current_r
                trade.trail_stop_price = l + trail_distance_r * risk
            if current_r >= trail_max_r:
                trade.close(trade.entry_price - trail_max_r * risk, t, "max_r")
                return
            if trade.trail_stop_price and h >= trade.trail_stop_price - sl_buffer:
                trade.close(trade.trail_stop_price, t, "trail")
                return
        else:
            if current_r >= trail_activation_r:
                trade.trail_active = True
                trade.peak_price = l
                trade.peak_r = current_r
                trade.trail_stop_price = l + trail_distance_r * risk
                trade.stop_loss = trade.entry_price
                return

        if current_r > trade.peak_r:
            trade.peak_r = current_r


# ═══════════════════════════════════════════════════
#  REALISTIC EQUITY SIMULATOR
#  Event-driven: processes ENTRY + EXIT events in time order
#  Each trade risks % of equity AT ENTRY TIME (dollar_risk is locked)
#  Enforces MAX_CONCURRENT, daily trade caps
# ═══════════════════════════════════════════════════

@dataclass
class OpenPosition:
    trade: Trade
    dollar_risk: float       # equity * risk_pct at entry time
    entry_equity: float

def simulate_equity(
    all_trades: List[Trade],
    start_equity: float = 150.0,
    risk_pct: float = 0.08,
    max_concurrent: int = 2,
    max_per_day: int = 6,
    fee_r: float = 0.04,
) -> dict:
    """
    Event-driven equity simulation with concurrent position limits.

    KEY FIX from previous version:
    - Each trade locks dollar_risk = equity * risk_pct at ENTRY time
    - P&L on exit = dollar_risk * r_multiple (based on entry equity, not exit equity)
    - This correctly handles concurrent positions
    """
    if not all_trades:
        return {"trades_taken": 0, "trades_skipped": 0, "trades_available": 0,
                "wr": 0, "avg_r": 0, "total_r": 0, "avg_win": 0, "avg_loss": 0,
                "max_dd": 0, "final_eq": start_equity, "x10": None, "x100": None,
                "x1000": None, "max_consec_loss": 0, "r_vals": [], "monthly": {},
                "eq_snapshots": [], "closed_trades": []}

    # Filter to only closed trades with valid r_multiple
    valid = [t for t in all_trades if t.is_closed and t.r_multiple is not None
             and t.exit_time is not None]
    if not valid:
        return {"trades_taken": 0, "trades_skipped": len(all_trades),
                "trades_available": len(all_trades), "wr": 0, "avg_r": 0,
                "total_r": 0, "avg_win": 0, "avg_loss": 0, "max_dd": 0,
                "final_eq": start_equity, "x10": None, "x100": None,
                "x1000": None, "max_consec_loss": 0, "r_vals": [], "monthly": {},
                "eq_snapshots": [], "closed_trades": []}

    # Build events: ('entry', time, trade) and ('exit', time, trade)
    events = []
    for t in valid:
        events.append(('entry', t.entry_time, t))
        events.append(('exit', t.exit_time, t))

    # Sort: by time, then exits before entries (free slots first)
    events.sort(key=lambda e: (e[1], 0 if e[0] == 'exit' else 1, e[2].pair))

    equity = start_equity
    peak_equity = start_equity
    max_dd = 0.0
    open_positions: Dict[str, OpenPosition] = {}  # pair -> OpenPosition
    daily_entries: Dict[str, int] = {}
    session_pair_entries: set = set()

    taken_set: set = set()    # trade ids we accepted
    skipped_count = 0
    closed_trades: List[Trade] = []
    r_vals: List[float] = []
    eq_snapshots: List[Tuple[datetime, float]] = [(valid[0].entry_time, equity)]

    for event_type, event_time, trade in events:
        trade_id = id(trade)

        if event_type == 'exit':
            # Only process exits for trades we actually took
            if trade_id not in taken_set:
                continue
            if trade.pair not in open_positions:
                continue

            pos = open_positions[trade.pair]
            if id(pos.trade) != trade_id:
                continue  # Different trade for this pair

            # P&L based on entry-time equity
            pnl = pos.dollar_risk * trade.r_multiple
            equity += pnl
            equity = max(equity, 0.01)

            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
            max_dd = max(max_dd, dd)

            del open_positions[trade.pair]
            closed_trades.append(trade)
            r_vals.append(trade.r_multiple)
            eq_snapshots.append((event_time, equity))

        elif event_type == 'entry':
            day_key = trade.entry_time.strftime("%Y-%m-%d")
            session_pair_key = f"{trade.session_name}_{trade.session_date}_{trade.pair}"

            # --- Filters ---
            # 1. Pair already open?
            if trade.pair in open_positions:
                skipped_count += 1
                continue

            # 2. Already entered this pair this session?
            if session_pair_key in session_pair_entries:
                skipped_count += 1
                continue

            # 3. Position cap?
            if len(open_positions) >= max_concurrent:
                skipped_count += 1
                continue

            # 4. Daily cap?
            if daily_entries.get(day_key, 0) >= max_per_day:
                skipped_count += 1
                continue

            # 5. Equity too low to trade?
            if equity < 5.0:
                skipped_count += 1
                continue

            # --- ENTER ---
            dollar_risk = equity * risk_pct
            open_positions[trade.pair] = OpenPosition(
                trade=trade, dollar_risk=dollar_risk, entry_equity=equity)
            daily_entries[day_key] = daily_entries.get(day_key, 0) + 1
            session_pair_entries.add(session_pair_key)
            taken_set.add(trade_id)

    # Stats
    winners = [r for r in r_vals if r > 0]
    losers = [r for r in r_vals if r <= 0]

    x10 = x100 = x1000 = None
    eq_check = start_equity
    for i, r in enumerate(r_vals):
        # Replay with locked risk for milestone detection
        eq_check += eq_check * risk_pct * r
        eq_check = max(eq_check, 0.01)
        if x10 is None and eq_check >= start_equity * 10:
            x10 = i + 1
        if x100 is None and eq_check >= start_equity * 100:
            x100 = i + 1
        if x1000 is None and eq_check >= start_equity * 1000:
            x1000 = i + 1

    max_consec = 0
    cur_consec = 0
    for r in r_vals:
        if r <= 0:
            cur_consec += 1
            max_consec = max(max_consec, cur_consec)
        else:
            cur_consec = 0

    monthly: Dict[str, List[float]] = {}
    for t in closed_trades:
        mk = t.entry_time.strftime("%Y-%m")
        if mk not in monthly:
            monthly[mk] = []
        monthly[mk].append(t.r_multiple)

    return {
        "trades_taken": len(closed_trades),
        "trades_skipped": skipped_count,
        "trades_available": len(all_trades),
        "wr": len(winners) / len(r_vals) if r_vals else 0,
        "avg_r": statistics.mean(r_vals) if r_vals else 0,
        "total_r": sum(r_vals) if r_vals else 0,
        "avg_win": statistics.mean(winners) if winners else 0,
        "avg_loss": statistics.mean(losers) if losers else 0,
        "max_dd": max_dd,
        "final_eq": equity,
        "x10": x10,
        "x100": x100,
        "x1000": x1000,
        "max_consec_loss": max_consec,
        "r_vals": r_vals,
        "monthly": monthly,
        "eq_snapshots": eq_snapshots,
        "closed_trades": closed_trades,
    }


def monte_carlo_dd(r_vals: List[float], n_trials: int = 2000,
                   start: float = 150.0, risk_pct: float = 0.08) -> dict:
    """Monte Carlo shuffle — focus on DD distribution."""
    max_dds = []
    x10_trades = []
    finals = []
    bust_count = 0

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
        finals.append(eq)
        if x10:
            x10_trades.append(x10)

    max_dds.sort()
    finals.sort()
    return {
        "median_dd": max_dds[len(max_dds) // 2],
        "p75_dd": max_dds[int(len(max_dds) * 0.75)],
        "p90_dd": max_dds[int(len(max_dds) * 0.90)],
        "p95_dd": max_dds[int(len(max_dds) * 0.95)],
        "p99_dd": max_dds[int(len(max_dds) * 0.99)],
        "worst_dd": max_dds[-1],
        "bust_pct": bust_count / n_trials,
        "x10_pct": len(x10_trades) / n_trials,
        "x10_median": sorted(x10_trades)[len(x10_trades) // 2] if x10_trades else None,
        "median_final": finals[len(finals) // 2],
        "p10_final": finals[int(len(finals) * 0.10)],
    }


# ═══════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════

def main():
    t0 = time.time()
    random.seed(42)

    print("=" * 80)
    print("  REALISTIC BACKTEST — CORRECTED")
    print("  128 pairs x 6 months | MAX 2 CONCURRENT positions")
    print("  Fixes: position cap, proper compounding, daily limits")
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
    print(f"\n    Done: {total_candles:,} candles across {len(pair_data)} pairs\n")

    # ═══════════════════════════════════════════════════
    #  SECTION 1: GENERATE ALL POTENTIAL TRADES
    # ═══════════════════════════════════════════════════
    print("=" * 80)
    print("  SECTION 1: TRADE GENERATION")
    print("=" * 80)

    configs = [
        {"label": "act=0.85, dist=0.15R", "act": 0.85, "dist": 0.15},
        {"label": "act=0.85, dist=0.20R", "act": 0.85, "dist": 0.20},
        {"label": "act=0.95, dist=0.15R", "act": 0.95, "dist": 0.15},
        {"label": "act=0.95, dist=0.20R (CURRENT)", "act": 0.95, "dist": 0.20},
        {"label": "act=0.95, dist=0.25R", "act": 0.95, "dist": 0.25},
        {"label": "act=0.95, dist=0.30R", "act": 0.95, "dist": 0.30},
        {"label": "act=1.00, dist=0.20R", "act": 1.00, "dist": 0.20},
    ]

    all_config_trades: Dict[str, List[Trade]] = {}

    for cfg in configs:
        all_trades = []
        for pair, candles in pair_data.items():
            trades = generate_pair_trades(
                pair, candles,
                trail_activation_r=cfg["act"],
                trail_distance_r=cfg["dist"],
                require_retest=True,
                fee_per_trade_r=0.04,
                wick_penalty_r=0.0,
            )
            all_trades.extend(trades)

        closed = [t for t in all_trades if t.is_closed and t.r_multiple is not None]
        all_config_trades[cfg["label"]] = all_trades
        r_vals = [t.r_multiple for t in closed]
        wr = sum(1 for r in r_vals if r > 0) / len(r_vals) if r_vals else 0
        avg_r = statistics.mean(r_vals) if r_vals else 0

        print(f"  {cfg['label']:>32s}: {len(closed):>5d} potential trades, "
              f"WR={wr:.1%}, AvgR={avg_r:+.4f}")

    # ═══════════════════════════════════════════════════
    #  SECTION 2: REALISTIC EQUITY SIMULATION
    #  MAX 2 concurrent, 6 trades/day cap
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 2: REALISTIC EQUITY SIM (MAX 2 concurrent, 6/day)")
    print("  Parameter sweep: trail × risk — sorted by lowest MAX DD")
    print("=" * 80)

    sweep_results = []
    risk_levels = [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10]

    for cfg in configs:
        all_trades = all_config_trades[cfg["label"]]

        for risk in risk_levels:
            sim = simulate_equity(
                all_trades,
                start_equity=150.0,
                risk_pct=risk,
                max_concurrent=2,
                max_per_day=6,
            )

            sweep_results.append({
                "config": cfg["label"],
                "risk": risk,
                "taken": sim["trades_taken"],
                "skipped": sim["trades_skipped"],
                "available": sim["trades_available"],
                "wr": sim["wr"],
                "avg_r": sim["avg_r"],
                "max_dd": sim["max_dd"],
                "final_eq": sim["final_eq"],
                "x10": sim["x10"],
                "x100": sim["x100"],
                "max_consec": sim["max_consec_loss"],
                "r_vals": sim["r_vals"],
                "monthly": sim["monthly"],
            })

    # Sort by max DD ascending
    sweep_results.sort(key=lambda r: r["max_dd"])

    # Show all that reach x10
    x10_results = [r for r in sweep_results if r["x10"] is not None]

    print(f"\n  {'Config':>32s}  {'Risk':>4s}  {'Taken':>5s}  {'Skip':>5s}  "
          f"{'WR':>5s}  {'AvgR':>7s}  {'MaxDD':>6s}  {'FinalEq':>10s}  "
          f"{'x10':>5s}  {'Consec':>6s}")
    print(f"  {'-'*32}  {'-'*4}  {'-'*5}  {'-'*5}  "
          f"{'-'*5}  {'-'*7}  {'-'*6}  {'-'*10}  {'-'*5}  {'-'*6}")

    print(f"\n  === CONFIGS THAT REACH x10 (sorted by lowest DD) ===")
    for r in x10_results[:25]:
        x10s = f"{r['x10']}t" if r['x10'] else "-"
        print(f"  {r['config']:>32s}  {r['risk']:>3.0%}  {r['taken']:>5d}  "
              f"{r['skipped']:>5d}  {r['wr']:>4.1%}  {r['avg_r']:>+.4f}  "
              f"{r['max_dd']:>5.1%}  ${r['final_eq']:>9,.0f}  {x10s:>5s}  "
              f"{r['max_consec']:>6d}")

    if not x10_results:
        print(f"\n  NO configs reach x10 with concurrent limits!")
        print(f"\n  === TOP 10 BY FINAL EQUITY ===")
        by_eq = sorted(sweep_results, key=lambda r: -r["final_eq"])
        for r in by_eq[:10]:
            print(f"  {r['config']:>32s}  {r['risk']:>3.0%}  {r['taken']:>5d}  "
                  f"WR={r['wr']:.1%}  AvgR={r['avg_r']:+.4f}  "
                  f"DD={r['max_dd']:.1%}  Eq=${r['final_eq']:,.0f}")

    # ═══════════════════════════════════════════════════
    #  SECTION 3: MONTHLY BREAKDOWN for top 3
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 3: MONTHLY BREAKDOWN — TOP 3 CONFIGS")
    print("=" * 80)

    best_list = x10_results[:3] if x10_results else sorted(sweep_results, key=lambda r: -r["final_eq"])[:3]

    for idx, best in enumerate(best_list):
        print(f"\n  Config #{idx+1}: {best['config']}, risk={best['risk']:.0%}")
        print(f"  Taken: {best['taken']}, Skipped: {best['skipped']}, "
              f"MaxDD: {best['max_dd']:.1%}, Final: ${best['final_eq']:,.0f}")

        if best["monthly"]:
            eq = 150.0
            print(f"  {'Month':>8s}  {'Trades':>6s}  {'WR':>5s}  {'AvgR':>7s}  "
                  f"{'TotR':>7s}  {'StartEq':>9s}  {'EndEq':>9s}  {'Return':>7s}  {'DD':>7s}")
            print(f"  {'-'*8}  {'-'*6}  {'-'*5}  {'-'*7}  "
                  f"{'-'*7}  {'-'*9}  {'-'*9}  {'-'*7}  {'-'*7}")

            risk = best["risk"]
            for mk in sorted(best["monthly"].keys()):
                rv = best["monthly"][mk]
                w = sum(1 for r in rv if r > 0)
                wr = w / len(rv) if rv else 0
                avg = statistics.mean(rv) if rv else 0
                tot = sum(rv)

                start_eq = eq
                peak_eq = eq
                m_dd = 0.0
                for r in rv:
                    eq *= (1 + risk * r)
                    eq = max(eq, 0.01)
                    if eq > peak_eq:
                        peak_eq = eq
                    dd = (peak_eq - eq) / peak_eq if peak_eq > 0 else 0
                    m_dd = max(m_dd, dd)

                ret = (eq - start_eq) / start_eq if start_eq > 0 else 0
                print(f"  {mk:>8s}  {len(rv):>6d}  {wr:>4.1%}  {avg:>+.4f}  "
                      f"{tot:>+6.1f}  ${start_eq:>8,.0f}  ${eq:>8,.0f}  "
                      f"{ret:>+6.1%}  {m_dd:>6.1%}")

            # Best/worst month
            month_totals = {mk: sum(rv) for mk, rv in best["monthly"].items()}
            worst_m = min(month_totals, key=month_totals.get)
            best_m = max(month_totals, key=month_totals.get)
            print(f"  -> Worst: {worst_m} ({month_totals[worst_m]:+.1f}R)")
            print(f"  -> Best:  {best_m} ({month_totals[best_m]:+.1f}R)")

    # ═══════════════════════════════════════════════════
    #  SECTION 4: WICK PENALTY SENSITIVITY (top 3)
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 4: WICK PENALTY SENSITIVITY")
    print("  How much worse when real wicks hit your stops?")
    print("=" * 80)

    wick_levels = [0.0, 0.03, 0.05, 0.08, 0.10, 0.15]

    for idx, best in enumerate(best_list):
        # Find the matching config
        cfg = None
        for c in configs:
            if c["label"] == best["config"]:
                cfg = c
                break
        if not cfg:
            continue

        risk = best["risk"]
        print(f"\n  Config #{idx+1}: {best['config']}, risk={risk:.0%}")
        print(f"  {'WickPen':>8s}  {'Taken':>5s}  {'WR':>5s}  {'AvgR':>7s}  "
              f"{'MaxDD':>6s}  {'FinalEq':>10s}  {'x10':>5s}")
        print(f"  {'-'*8}  {'-'*5}  {'-'*5}  {'-'*7}  "
              f"{'-'*6}  {'-'*10}  {'-'*5}")

        for wp in wick_levels:
            all_trades = []
            for pair, candles in pair_data.items():
                trades = generate_pair_trades(
                    pair, candles,
                    trail_activation_r=cfg["act"],
                    trail_distance_r=cfg["dist"],
                    require_retest=True,
                    fee_per_trade_r=0.04,
                    wick_penalty_r=wp,
                )
                all_trades.extend(trades)

            sim = simulate_equity(all_trades, 150.0, risk, max_concurrent=2, max_per_day=6)
            x10s = f"{sim['x10']}t" if sim['x10'] else "never"
            print(f"  {wp:>7.2f}R  {sim['trades_taken']:>5d}  {sim['wr']:>4.1%}  "
                  f"{sim['avg_r']:>+.4f}  {sim['max_dd']:>5.1%}  "
                  f"${sim['final_eq']:>9,.0f}  {x10s:>5s}")

    # ═══════════════════════════════════════════════════
    #  SECTION 5: MONTE CARLO (top 3)
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 5: MONTE CARLO DD DISTRIBUTION (2000 trials)")
    print("  What DD to actually expect? (based on REALISTIC trade set)")
    print("=" * 80)

    for idx, best in enumerate(best_list):
        r_vals = best["r_vals"]
        risk = best["risk"]
        if not r_vals:
            continue

        print(f"\n  Config #{idx+1}: {best['config']}, risk={risk:.0%}, "
              f"{len(r_vals)} trades")

        mc = monte_carlo_dd(r_vals, 2000, 150.0, risk)
        print(f"    Median DD:    {mc['median_dd']:>6.1%}")
        print(f"    75th %%ile:    {mc['p75_dd']:>6.1%}")
        print(f"    90th %%ile:    {mc['p90_dd']:>6.1%}")
        print(f"    95th %%ile:    {mc['p95_dd']:>6.1%}  <- plan for this")
        print(f"    99th %%ile:    {mc['p99_dd']:>6.1%}")
        print(f"    Worst:        {mc['worst_dd']:>6.1%}")
        print(f"    Bust (<$10):  {mc['bust_pct']:>6.1%}")
        print(f"    x10 chance:   {mc['x10_pct']:>6.1%}")
        if mc["x10_median"]:
            print(f"    x10 median:   {mc['x10_median']}t (~{mc['x10_median']//3:.0f} days)")
        print(f"    Median final: ${mc['median_final']:>,.0f}")
        print(f"    10th %%ile eq:  ${mc['p10_final']:>,.0f}")

    # ═══════════════════════════════════════════════════
    #  SECTION 6: 3-MONTH ROLLING WINDOWS (consistency check)
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 6: 3-MONTH ROLLING WINDOWS")
    print("=" * 80)

    if best_list:
        best = best_list[0]
        cfg = None
        for c in configs:
            if c["label"] == best["config"]:
                cfg = c
                break

        if cfg:
            risk = best["risk"]
            print(f"  Using: {best['config']}, risk={risk:.0%}\n")

            windows = [
                ("Aug-Oct 2025", datetime(2025, 8, 1, tzinfo=timezone.utc),
                 datetime(2025, 11, 1, tzinfo=timezone.utc)),
                ("Sep-Nov 2025", datetime(2025, 9, 1, tzinfo=timezone.utc),
                 datetime(2025, 12, 1, tzinfo=timezone.utc)),
                ("Oct-Dec 2025", datetime(2025, 10, 1, tzinfo=timezone.utc),
                 datetime(2026, 1, 1, tzinfo=timezone.utc)),
                ("Nov-Jan 2026", datetime(2025, 11, 1, tzinfo=timezone.utc),
                 datetime(2026, 2, 1, tzinfo=timezone.utc)),
                ("Dec-Feb 2026", datetime(2025, 12, 1, tzinfo=timezone.utc),
                 datetime(2026, 3, 1, tzinfo=timezone.utc)),
            ]

            print(f"  {'Window':>14s}  {'Pairs':>5s}  {'Taken':>5s}  {'Skip':>5s}  "
                  f"{'WR':>5s}  {'AvgR':>7s}  {'MaxDD':>6s}  {'x10':>5s}  {'EndEq':>9s}")
            print(f"  {'-'*14}  {'-'*5}  {'-'*5}  {'-'*5}  "
                  f"{'-'*5}  {'-'*7}  {'-'*6}  {'-'*5}  {'-'*9}")

            for wname, wstart, wend in windows:
                all_trades = []
                pairs_used = 0
                for pair, candles in pair_data.items():
                    wc = [c for c in candles if wstart <= c.date < wend]
                    if len(wc) < 100:
                        continue
                    pairs_used += 1
                    trades = generate_pair_trades(
                        pair, wc,
                        trail_activation_r=cfg["act"],
                        trail_distance_r=cfg["dist"],
                        require_retest=True,
                        fee_per_trade_r=0.04,
                    )
                    all_trades.extend(trades)

                sim = simulate_equity(all_trades, 150.0, risk, max_concurrent=2, max_per_day=6)
                x10s = f"{sim['x10']}t" if sim['x10'] else "never"
                print(f"  {wname:>14s}  {pairs_used:>5d}  {sim['trades_taken']:>5d}  "
                      f"{sim['trades_skipped']:>5d}  {sim['wr']:>4.1%}  "
                      f"{sim['avg_r']:>+.4f}  {sim['max_dd']:>5.1%}  "
                      f"{x10s:>5s}  ${sim['final_eq']:>8,.0f}")

    # ═══════════════════════════════════════════════════
    #  SECTION 7: SESSION + PAIR BREAKDOWN
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 7: SESSION & PAIR PERFORMANCE")
    print("=" * 80)

    if best_list and best_list[0].get("r_vals"):
        best = best_list[0]
        cfg = None
        for c in configs:
            if c["label"] == best["config"]:
                cfg = c
                break

        if cfg:
            # Re-run to get full trade objects
            all_trades = []
            for pair, candles in pair_data.items():
                trades = generate_pair_trades(
                    pair, candles,
                    trail_activation_r=cfg["act"],
                    trail_distance_r=cfg["dist"],
                    require_retest=True,
                    fee_per_trade_r=0.04,
                )
                all_trades.extend(trades)

            sim = simulate_equity(all_trades, 150.0, best["risk"],
                                  max_concurrent=2, max_per_day=6)
            closed = sim.get("closed_trades", [])

            # Session breakdown
            for sess in ["asia", "london", "ny"]:
                st = [t for t in closed if t.session_name == sess]
                if not st:
                    continue
                rv = [t.r_multiple for t in st]
                w = sum(1 for r in rv if r > 0)
                wr = w / len(rv) if rv else 0
                avg = statistics.mean(rv) if rv else 0
                wins = [r for r in rv if r > 0]
                losses = [r for r in rv if r <= 0]
                avg_w = statistics.mean(wins) if wins else 0
                avg_l = statistics.mean(losses) if losses else 0
                exits = {}
                for t in st:
                    exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1
                payoff = avg_w / abs(avg_l) if avg_l < 0 else 999
                print(f"\n  {sess.upper():>8s}: {len(rv)} trades, WR={wr:.1%}, "
                      f"AvgR={avg:+.4f}, TotalR={sum(rv):+.1f}")
                print(f"           AvgWin={avg_w:+.3f}, AvgLoss={avg_l:+.3f}, "
                      f"Payoff={payoff:.2f}x")
                print(f"           Exits: {exits}")

            # Top/bottom pairs
            pair_stats: Dict[str, dict] = {}
            for t in closed:
                p = t.pair.replace("/USDT:USDT", "")
                if p not in pair_stats:
                    pair_stats[p] = {"trades": 0, "wins": 0, "total_r": 0.0}
                pair_stats[p]["trades"] += 1
                pair_stats[p]["total_r"] += t.r_multiple
                if t.r_multiple > 0:
                    pair_stats[p]["wins"] += 1

            sorted_pairs = sorted(pair_stats.items(), key=lambda x: x[1]["total_r"], reverse=True)
            profitable = sum(1 for _, s in sorted_pairs if s["total_r"] > 0)
            total_r = sum(s["total_r"] for _, s in sorted_pairs)

            print(f"\n  TOP 10 PAIRS:")
            for p, s in sorted_pairs[:10]:
                wr = s["wins"] / s["trades"] if s["trades"] > 0 else 0
                avg = s["total_r"] / s["trades"]
                print(f"    {p:>15s}  {s['trades']:>3d}t  WR={wr:.0%}  "
                      f"AvgR={avg:+.3f}  TotR={s['total_r']:+.1f}")

            print(f"\n  WORST 10 PAIRS:")
            for p, s in sorted_pairs[-10:]:
                wr = s["wins"] / s["trades"] if s["trades"] > 0 else 0
                avg = s["total_r"] / s["trades"]
                print(f"    {p:>15s}  {s['trades']:>3d}t  WR={wr:.0%}  "
                      f"AvgR={avg:+.3f}  TotR={s['total_r']:+.1f}")

            print(f"\n  Profitable: {profitable}/{len(sorted_pairs)} pairs ({profitable/len(sorted_pairs)*100:.0f}%)")
            if total_r != 0:
                top10_r = sum(s["total_r"] for _, s in sorted_pairs[:10])
                print(f"  Top 10 contribute: {top10_r:+.1f}R ({top10_r/total_r*100:.0f}% of {total_r:+.1f}R total)")

    # ═══════════════════════════════════════════════════
    #  EXECUTIVE SUMMARY
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  EXECUTIVE SUMMARY — REALISTIC NUMBERS")
    print("=" * 80)

    if best_list:
        b = best_list[0]
        print(f"\n  BEST CONFIG (lowest DD that reaches x10):")
        print(f"    Trail config:       {b['config']}")
        print(f"    Risk per trade:     {b['risk']:.0%}")
        print(f"    Retest required:    YES")
        print(f"    Max concurrent:     2")
        print(f"    Daily cap:          6 trades/day")
        print(f"    Fee assumption:     0.04R")
        print(f"    ─────────────────────────")
        print(f"    Trades taken:       {b['taken']} (out of {b['available']} available, {b['skipped']} skipped)")
        print(f"    Win rate:           {b['wr']:.1%}")
        print(f"    Avg R per trade:    {b['avg_r']:+.4f}")
        print(f"    Total R:            {b.get('r_vals', []) and sum(b['r_vals']):+.1f}")
        print(f"    Max drawdown:       {b['max_dd']:.1%}")
        print(f"    Max consec losses:  {b['max_consec']}")
        x10s = f"{b['x10']}t (~{b['x10']//3:.0f}d at ~3t/day)" if b['x10'] else "never"
        x100s = f"{b['x100']}t" if b.get('x100') else "never"
        print(f"    Reach x10 ($1,500): {x10s}")
        print(f"    Reach x100 ($15K):  {x100s}")
        print(f"    Final equity:       ${b['final_eq']:,.0f}")
    else:
        print("\n  No configs available to summarize.")

    elapsed = time.time() - t0
    print(f"\n  Runtime: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"  Data: {len(pair_data)} pairs, {total_candles:,} candles\n")


if __name__ == "__main__":
    main()
