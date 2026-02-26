"""
research/session_sim.py — SESSION-FAITHFUL BACKTEST

Mirrors the live bot EXACTLY:
  1. Day = 3 sessions: asia(0-8), london(8-16), ny(16-24)
  2. Each session: capture FC (candle 0), scan for C2 breakout, wait C3 retest
  3. MAX 2 concurrent positions across ALL pairs at any moment
  4. 1 entry per pair per session (can_trade check)
  5. Trades that are still open carry into next session (use up a slot)
  6. After each session: resolve exits, update equity, rebalance risk
  7. Risk % is based on equity AT ENTRY TIME (not at trade close)
  8. Daily resets at midnight UTC (counters, session tracking)

This is NOT a "generate all trades then simulate" approach.
This walks through every 5-minute candle chronologically, exactly like the bot.
"""

from __future__ import annotations
import csv, glob, math, os, sys, time, statistics, random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT))


# ═══════════════════════════════════════════════
#  CONFIG — mirrors live/config.py exactly
# ═══════════════════════════════════════════════
SESSIONS = {
    "asia":   (0, 8),
    "london": (8, 16),
    "ny":     (16, 24),
}
SESSION_ORDER = ["asia", "london", "ny"]


@dataclass
class SimConfig:
    risk_pct: float = 0.08
    max_concurrent: int = 2
    max_per_session_per_pair: int = 1   # can_trade: 1 entry per pair per session
    max_per_day_per_pair: int = 6
    trail_activation_r: float = 0.95
    trail_distance_r: float = 0.20
    trail_max_r: float = 10.0
    safety_tp_r: float = 10.0
    min_c2_body: float = 0.50
    fc_counter: bool = True
    vol_ratio_long: float = 1.0
    vol_ratio_short: float = 0.25
    min_range_pct: float = 0.003
    require_retest: bool = True
    fee_per_trade_r: float = 0.04
    breakout_window_candles: int = 12  # 60min / 5min = 12 candles
    wick_penalty_r: float = 0.0
    start_equity: float = 150.0
    label: str = ""


@dataclass
class Candle:
    dt: datetime
    o: float
    h: float
    l: float
    c: float
    v: float
    body_ratio: float = 0.0
    candle_dir: int = 0  # 1=bull, -1=bear, 0=doji


@dataclass
class OpenTrade:
    pair: str
    session: str
    direction: str
    entry_price: float
    entry_time: datetime
    stop_loss: float
    risk_per_unit: float  # |entry - SL|
    dollar_risk: float    # equity * risk_pct at entry
    entry_equity: float
    range_high: float
    range_low: float
    range_mid: float
    trail_active: bool = False
    trail_stop: Optional[float] = None
    peak_price: Optional[float] = None
    peak_r: float = 0.0
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    r_multiple: Optional[float] = None

    @property
    def is_open(self):
        return self.exit_price is None

    def close(self, price, time, reason):
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


# ═══════════════════════════════════════════════
#  DATA LOADING
# ═══════════════════════════════════════════════

def load_all_pairs() -> Dict[str, List[Candle]]:
    """Load all pair CSVs, return {pair: [candles sorted by time]}"""
    pattern = str(DATA_DIR / "bybit_futures_*.csv")
    pair_data = {}
    files = sorted(glob.glob(pattern))

    for fpath in files:
        base = os.path.basename(fpath).replace(".csv", "")
        parts = base.split("_")
        if len(parts) >= 6 and parts[0] == "bybit" and parts[1] == "futures":
            symbol_parts = parts[2:-3]
            symbol = "_".join(symbol_parts) if symbol_parts else parts[2]
            pair = f"{symbol}/USDT:USDT"
        else:
            continue

        candles = []
        with open(fpath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                dt_str = row["date"].replace("+00:00", "").replace("Z", "")
                try:
                    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    dt = dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                o, h, l, c, v = (float(row["open"]), float(row["high"]),
                                 float(row["low"]), float(row["close"]),
                                 float(row["volume"]))
                cd = Candle(dt=dt, o=o, h=h, l=l, c=c, v=v)
                full_range = h - l
                if full_range > 0:
                    cd.body_ratio = abs(c - o) / full_range
                cd.candle_dir = 1 if c > o else (-1 if c < o else 0)
                candles.append(cd)

        candles.sort(key=lambda x: x.dt)
        if candles:
            pair_data[pair] = candles

    return pair_data


def build_time_index(pair_data: Dict[str, List[Candle]]) -> Dict[str, Dict[datetime, int]]:
    """Build {pair: {datetime: index}} for O(1) candle lookups."""
    idx = {}
    for pair, candles in pair_data.items():
        idx[pair] = {c.dt: i for i, c in enumerate(candles)}
    return idx


def get_candle_at(pair: str, dt: datetime, pair_data, time_idx) -> Optional[Candle]:
    """Get candle for pair at exact datetime."""
    i = time_idx.get(pair, {}).get(dt)
    if i is not None:
        return pair_data[pair][i]
    return None


def get_candle_after(pair: str, dt: datetime, pair_data, time_idx, offset: int = 1) -> Optional[Candle]:
    """Get candle N positions after dt."""
    i = time_idx.get(pair, {}).get(dt)
    if i is not None and i + offset < len(pair_data[pair]):
        return pair_data[pair][i + offset]
    return None


# ═══════════════════════════════════════════════
#  SESSION SIMULATOR — the core engine
# ═══════════════════════════════════════════════

def simulate(pair_data: Dict[str, List[Candle]], cfg: SimConfig) -> dict:
    """
    Walk through every day × session × candle, exactly like the live bot.

    Returns full statistics including per-session equity snapshots.
    """
    time_idx = build_time_index(pair_data)

    # Find date range across all pairs
    all_dts = []
    for candles in pair_data.values():
        if candles:
            all_dts.append(candles[0].dt)
            all_dts.append(candles[-1].dt)
    if not all_dts:
        return _empty_result(cfg)

    start_date = min(all_dts).replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = max(all_dts).replace(hour=0, minute=0, second=0, microsecond=0)

    # State
    equity = cfg.start_equity
    peak_equity = cfg.start_equity
    max_dd = 0.0
    open_positions: Dict[str, OpenTrade] = {}  # pair -> trade
    closed_trades: List[OpenTrade] = []

    # Tracking
    session_entries_today: Dict[str, Set[str]] = {}  # session -> set of pairs entered
    daily_pair_counts: Dict[str, int] = {}  # pair -> count today

    eq_snapshots: List[Tuple[datetime, float, str]] = []
    session_stats: Dict[str, dict] = {s: {"trades": 0, "wins": 0, "losses": 0,
                                           "total_r": 0.0} for s in SESSION_ORDER}

    # Walk day by day
    current_day = start_date
    while current_day <= end_date:
        day_str = current_day.strftime("%Y-%m-%d")

        # ── Daily reset ──
        session_entries_today.clear()
        daily_pair_counts.clear()

        for sess_name in SESSION_ORDER:
            sess_start_h, sess_end_h = SESSIONS[sess_name]
            session_entries_today[sess_name] = set()

            sess_start = current_day.replace(hour=sess_start_h, minute=0,
                                             second=0, microsecond=0)
            sess_end = current_day.replace(hour=0, minute=0, second=0,
                                           microsecond=0) + timedelta(hours=sess_end_h)

            # ── Step 1: Identify FC time (minute 0 of session) ──
            fc_time = sess_start

            # Collect FCs for all pairs that have data at this time
            pair_fcs: Dict[str, Candle] = {}
            for pair in pair_data:
                fc = get_candle_at(pair, fc_time, pair_data, time_idx)
                if fc is None:
                    continue
                # Validate: range must be >= min_range_pct
                mid = (fc.h + fc.l) / 2
                if mid <= 0:
                    continue
                range_pct = (fc.h - fc.l) / mid
                if range_pct < cfg.min_range_pct:
                    continue
                pair_fcs[pair] = fc

            # ── Step 2: Manage open positions through this session ──
            # Walk candle by candle through the session
            candle_count = (sess_end_h - sess_start_h) * 12  # 5min candles
            for c_idx in range(candle_count):
                candle_time = sess_start + timedelta(minutes=c_idx * 5)

                # Manage all open trades
                for pair in list(open_positions.keys()):
                    trade = open_positions[pair]
                    candle = get_candle_at(pair, candle_time, pair_data, time_idx)
                    if candle is None:
                        continue

                    _manage_open_trade(candle, trade, cfg)

                    if not trade.is_open:
                        # Trade closed
                        if cfg.fee_per_trade_r > 0:
                            trade.r_multiple -= cfg.fee_per_trade_r
                        pnl = trade.dollar_risk * trade.r_multiple
                        equity += pnl
                        equity = max(equity, 0.01)

                        if equity > peak_equity:
                            peak_equity = equity
                        dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
                        max_dd = max(max_dd, dd)

                        closed_trades.append(trade)
                        del open_positions[pair]

                        # Track session stats
                        session_stats[sess_name]["trades"] += 1
                        session_stats[sess_name]["total_r"] += trade.r_multiple
                        if trade.r_multiple > 0:
                            session_stats[sess_name]["wins"] += 1
                        else:
                            session_stats[sess_name]["losses"] += 1

                # ── Step 3: Look for entries (only during breakout window) ──
                # FC is candle 0. C2 starts at candle 1 (minute 5).
                # With retest, need C2 (breakout) + C3 (retest) = candle 2+ (minute 10+)
                # Breakout window: candle 1 to candle breakout_window_candles
                if c_idx < 1 or c_idx >= cfg.breakout_window_candles:
                    continue

                # Check each pair for breakout + retest
                for pair, fc in pair_fcs.items():
                    # ── can_trade check ──
                    if pair in session_entries_today[sess_name]:
                        continue
                    if daily_pair_counts.get(pair, 0) >= cfg.max_per_day_per_pair:
                        continue
                    if pair in open_positions:
                        continue
                    if len(open_positions) >= cfg.max_concurrent:
                        break  # no slots — skip all remaining pairs

                    # Get current candle (potential C2)
                    c2 = get_candle_at(pair, candle_time, pair_data, time_idx)
                    if c2 is None:
                        continue

                    # Check breakout
                    direction = None
                    if c2.c > fc.h:
                        direction = "long"
                    elif c2.c < fc.l:
                        direction = "short"

                    if direction is None:
                        continue

                    # ── Micro-filters on C2 ──
                    if cfg.min_c2_body > 0 and c2.body_ratio < cfg.min_c2_body:
                        continue
                    if cfg.fc_counter:
                        if direction == "long" and fc.candle_dir > 0:
                            continue
                        if direction == "short" and fc.candle_dir < 0:
                            continue
                    vol_r = c2.v / fc.v if fc.v > 0 else 1.0
                    if direction == "long" and cfg.vol_ratio_long > 0 and vol_r < cfg.vol_ratio_long:
                        continue
                    if direction == "short" and cfg.vol_ratio_short > 0 and vol_r < cfg.vol_ratio_short:
                        continue

                    # ── C3 Retest check ──
                    if cfg.require_retest:
                        c3 = get_candle_after(pair, candle_time, pair_data, time_idx, offset=1)
                        if c3 is None:
                            continue
                        if direction == "long":
                            retest_ok = (c3.l <= fc.h and c3.c > fc.h)
                        else:
                            retest_ok = (c3.h >= fc.l and c3.c < fc.l)
                        if not retest_ok:
                            continue
                        # Entry at C3 close
                        entry_price = c3.c
                        entry_time = c3.dt
                    else:
                        entry_price = c2.c
                        entry_time = c2.dt

                    # ── Compute SL & risk ──
                    sl = (fc.h + fc.l) / 2  # midpoint
                    risk_per_unit = abs(entry_price - sl)
                    if risk_per_unit <= 0:
                        continue

                    # ── Position limit re-check after potential exits ──
                    if len(open_positions) >= cfg.max_concurrent:
                        break

                    if equity < 5.0:
                        break

                    # ── ENTER ──
                    dollar_risk = equity * cfg.risk_pct
                    trade = OpenTrade(
                        pair=pair, session=sess_name, direction=direction,
                        entry_price=entry_price, entry_time=entry_time,
                        stop_loss=sl, risk_per_unit=risk_per_unit,
                        dollar_risk=dollar_risk, entry_equity=equity,
                        range_high=fc.h, range_low=fc.l,
                        range_mid=(fc.h + fc.l) / 2,
                    )
                    open_positions[pair] = trade
                    session_entries_today[sess_name].add(pair)
                    daily_pair_counts[pair] = daily_pair_counts.get(pair, 0) + 1

            # ── End of session: record equity snapshot ──
            eq_snapshots.append((sess_end, equity, sess_name))

        # Next day
        current_day += timedelta(days=1)

    # ── Close any remaining open trades at last available price ──
    for pair, trade in list(open_positions.items()):
        if pair in pair_data and pair_data[pair]:
            last_c = pair_data[pair][-1]
            trade.close(last_c.c, last_c.dt, "data_end")
            if cfg.fee_per_trade_r > 0:
                trade.r_multiple -= cfg.fee_per_trade_r
            pnl = trade.dollar_risk * trade.r_multiple
            equity += pnl
            equity = max(equity, 0.01)
            closed_trades.append(trade)

    return _compute_stats(closed_trades, equity, peak_equity, max_dd,
                          eq_snapshots, session_stats, cfg)


def _manage_open_trade(candle: Candle, trade: OpenTrade, cfg: SimConfig):
    """Manage a single open trade against one candle. Mirrors live Guardian."""
    h, l = candle.h, candle.l
    t = candle.dt
    risk = trade.risk_per_unit
    wp = cfg.wick_penalty_r * risk  # wick penalty buffer

    if trade.direction == "long":
        current_r = (h - trade.entry_price) / risk if risk > 0 else 0

        # SL hit (conservative: low touches stop + wick buffer)
        if l <= trade.stop_loss + wp:
            trade.close(trade.stop_loss, t, "sl")
            return

        if trade.trail_active:
            # Update peak
            if h > (trade.peak_price or 0):
                trade.peak_price = h
                trade.peak_r = current_r
                trade.trail_stop = h - cfg.trail_distance_r * risk
            # Max R cap
            if current_r >= cfg.trail_max_r:
                trade.close(trade.entry_price + cfg.trail_max_r * risk, t, "max_r")
                return
            # Trail stop hit
            if trade.trail_stop and l <= trade.trail_stop + wp:
                trade.close(trade.trail_stop, t, "trail")
                return
        else:
            if current_r >= cfg.trail_activation_r:
                trade.trail_active = True
                trade.peak_price = h
                trade.peak_r = current_r
                trade.trail_stop = h - cfg.trail_distance_r * risk
                trade.stop_loss = trade.entry_price  # Move to breakeven

        if current_r > trade.peak_r:
            trade.peak_r = current_r

    else:  # SHORT
        current_r = (trade.entry_price - l) / risk if risk > 0 else 0

        if h >= trade.stop_loss - wp:
            trade.close(trade.stop_loss, t, "sl")
            return

        if trade.trail_active:
            if l < (trade.peak_price or float('inf')):
                trade.peak_price = l
                trade.peak_r = current_r
                trade.trail_stop = l + cfg.trail_distance_r * risk
            if current_r >= cfg.trail_max_r:
                trade.close(trade.entry_price - cfg.trail_max_r * risk, t, "max_r")
                return
            if trade.trail_stop and h >= trade.trail_stop - wp:
                trade.close(trade.trail_stop, t, "trail")
                return
        else:
            if current_r >= cfg.trail_activation_r:
                trade.trail_active = True
                trade.peak_price = l
                trade.peak_r = current_r
                trade.trail_stop = l + cfg.trail_distance_r * risk
                trade.stop_loss = trade.entry_price

        if current_r > trade.peak_r:
            trade.peak_r = current_r


def _compute_stats(closed_trades, final_equity, peak_equity, max_dd,
                   eq_snapshots, session_stats, cfg) -> dict:
    """Compute all statistics from closed trades."""
    r_vals = [t.r_multiple for t in closed_trades if t.r_multiple is not None]
    winners = [r for r in r_vals if r > 0]
    losers = [r for r in r_vals if r <= 0]

    # Milestones
    x10 = x100 = x1000 = None
    eq = cfg.start_equity
    for i, t in enumerate(closed_trades):
        if t.r_multiple is None:
            continue
        eq += t.dollar_risk * t.r_multiple
        eq = max(eq, 0.01)
        if x10 is None and eq >= cfg.start_equity * 10:
            x10 = i + 1
        if x100 is None and eq >= cfg.start_equity * 100:
            x100 = i + 1
        if x1000 is None and eq >= cfg.start_equity * 1000:
            x1000 = i + 1

    # Consecutive losses
    max_consec = cur_consec = 0
    for r in r_vals:
        if r <= 0:
            cur_consec += 1
            max_consec = max(max_consec, cur_consec)
        else:
            cur_consec = 0

    # Monthly
    monthly: Dict[str, List[float]] = {}
    for t in closed_trades:
        if t.r_multiple is None:
            continue
        mk = t.entry_time.strftime("%Y-%m")
        if mk not in monthly:
            monthly[mk] = []
        monthly[mk].append(t.r_multiple)

    # Pair breakdown
    pair_stats: Dict[str, dict] = {}
    for t in closed_trades:
        if t.r_multiple is None:
            continue
        p = t.pair.replace("/USDT:USDT", "")
        if p not in pair_stats:
            pair_stats[p] = {"trades": 0, "wins": 0, "total_r": 0.0}
        pair_stats[p]["trades"] += 1
        pair_stats[p]["total_r"] += t.r_multiple
        if t.r_multiple > 0:
            pair_stats[p]["wins"] += 1

    return {
        "trades": len(r_vals),
        "wr": len(winners) / len(r_vals) if r_vals else 0,
        "avg_r": statistics.mean(r_vals) if r_vals else 0,
        "total_r": sum(r_vals),
        "avg_win": statistics.mean(winners) if winners else 0,
        "avg_loss": statistics.mean(losers) if losers else 0,
        "max_dd": max_dd,
        "final_eq": final_equity,
        "x10": x10,
        "x100": x100,
        "x1000": x1000,
        "max_consec_loss": max_consec,
        "r_vals": r_vals,
        "monthly": monthly,
        "eq_snapshots": eq_snapshots,
        "session_stats": session_stats,
        "pair_stats": pair_stats,
        "closed_trades": closed_trades,
        "config": cfg,
    }


def _empty_result(cfg):
    return {"trades": 0, "wr": 0, "avg_r": 0, "total_r": 0, "max_dd": 0,
            "final_eq": cfg.start_equity, "x10": None, "r_vals": [],
            "monthly": {}, "config": cfg}


# ═══════════════════════════════════════════════
#  MONTE CARLO
# ═══════════════════════════════════════════════

def monte_carlo(r_vals, dollar_risks, n_trials=2000, start=150.0):
    """Monte Carlo with actual dollar risks (not %, because risk was locked at entry)."""
    if not r_vals:
        return {}
    combined = list(zip(r_vals, dollar_risks))
    max_dds = []
    x10_trades = []
    finals = []
    bust = 0

    for _ in range(n_trials):
        random.shuffle(combined)
        eq = start
        peak = start
        mdd = 0.0
        x10 = None
        for i, (r, dr) in enumerate(combined):
            # Scale dollar_risk proportionally to current equity vs original entry equity
            # This approximates "what if these trades happened in different order"
            pnl = (eq / start) * dr * r   # scale by equity growth
            eq += pnl
            eq = max(eq, 0.01)
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0
            mdd = max(mdd, dd)
            if x10 is None and eq >= start * 10:
                x10 = i + 1
            if eq < 5:
                bust += 1
                break
        max_dds.append(mdd)
        finals.append(eq)
        if x10:
            x10_trades.append(x10)

    max_dds.sort()
    finals.sort()
    n = len(max_dds)
    return {
        "median_dd": max_dds[n // 2],
        "p75_dd": max_dds[int(n * 0.75)],
        "p90_dd": max_dds[int(n * 0.90)],
        "p95_dd": max_dds[int(n * 0.95)],
        "p99_dd": max_dds[int(n * 0.99)],
        "worst_dd": max_dds[-1],
        "bust_pct": bust / n_trials,
        "x10_pct": len(x10_trades) / n_trials,
        "x10_median": sorted(x10_trades)[len(x10_trades) // 2] if x10_trades else None,
        "median_final": finals[n // 2],
        "p10_final": finals[int(n * 0.10)],
    }


# ═══════════════════════════════════════════════
#  MAIN — RUN ALL ANALYSES
# ═══════════════════════════════════════════════

def print_config_result(res, indent="  "):
    """Print a single config result."""
    cfg = res["config"]
    x10s = f"{res['x10']}t" if res.get("x10") else "never"
    x100s = f"{res.get('x100', None) or 'never'}"
    print(f"{indent}Trades:         {res['trades']}")
    print(f"{indent}Win Rate:       {res['wr']:.1%}")
    print(f"{indent}Avg R:          {res['avg_r']:+.4f}")
    print(f"{indent}Total R:        {res['total_r']:+.1f}")
    print(f"{indent}Avg Win/Loss:   {res.get('avg_win', 0):+.3f} / "
          f"{res.get('avg_loss', 0):+.3f}")
    print(f"{indent}Max DD:         {res['max_dd']:.1%}")
    print(f"{indent}Max Consec Loss:{res.get('max_consec_loss', 0)}")
    print(f"{indent}x10:            {x10s}")
    print(f"{indent}Final Equity:   ${res['final_eq']:,.2f}")


def main():
    t0 = time.time()
    random.seed(42)

    print("=" * 80)
    print("  SESSION-FAITHFUL BACKTEST")
    print("  Mirrors live bot: 3 sessions/day, equity rebalance between sessions")
    print("  MAX 2 concurrent, 1 per pair per session, C3 retest required")
    print("=" * 80)

    # ── Load data ──
    print("\n  Loading data...")
    pair_data = load_all_pairs()
    total_candles = sum(len(c) for c in pair_data.values())
    print(f"  {len(pair_data)} pairs, {total_candles:,} candles\n")

    # ═══════════════════════════════════════════════════
    #  SECTION 1: PARAMETER SWEEP (risk × trail)
    #  Sorted by lowest max DD
    # ═══════════════════════════════════════════════════
    print("=" * 80)
    print("  SECTION 1: SWEEP — risk x trail (sorted by lowest DD)")
    print("  Session-by-session with equity rebalance")
    print("=" * 80)

    trail_configs = [
        ("act=0.85 dist=0.15R", 0.85, 0.15),
        ("act=0.85 dist=0.20R", 0.85, 0.20),
        ("act=0.95 dist=0.15R", 0.95, 0.15),
        ("act=0.95 dist=0.20R *", 0.95, 0.20),  # current
        ("act=0.95 dist=0.25R", 0.95, 0.25),
        ("act=0.95 dist=0.30R", 0.95, 0.30),
        ("act=1.00 dist=0.20R", 1.00, 0.20),
    ]
    risk_levels = [0.02, 0.03, 0.04, 0.05, 0.06, 0.08]

    all_results = []
    total_combos = len(trail_configs) * len(risk_levels)
    combo = 0

    for t_label, t_act, t_dist in trail_configs:
        for risk in risk_levels:
            combo += 1
            sys.stdout.write(f"\r  Running {combo}/{total_combos}: "
                             f"{t_label} risk={risk:.0%}          ")
            sys.stdout.flush()

            cfg = SimConfig(
                risk_pct=risk,
                trail_activation_r=t_act,
                trail_distance_r=t_dist,
                label=f"{t_label} risk={risk:.0%}",
            )
            res = simulate(pair_data, cfg)
            all_results.append(res)

    print(f"\r  Done: {total_combos} configurations tested.               \n")

    # Sort by max DD
    all_results.sort(key=lambda r: r["max_dd"])

    x10_results = [r for r in all_results if r.get("x10")]
    no_x10 = [r for r in all_results if not r.get("x10")]

    print(f"\n  === CONFIGS THAT REACH x10 (sorted by lowest DD) ===")
    print(f"  {'Config':>28s}  {'Risk':>4s}  {'#':>4s}  {'WR':>5s}  "
          f"{'AvgR':>7s}  {'TotR':>6s}  {'MaxDD':>6s}  {'x10':>5s}  "
          f"{'Consec':>6s}  {'FinalEq':>10s}")
    print(f"  {'-'*28}  {'-'*4}  {'-'*4}  {'-'*5}  "
          f"{'-'*7}  {'-'*6}  {'-'*6}  {'-'*5}  {'-'*6}  {'-'*10}")

    for res in x10_results[:30]:
        cfg = res["config"]
        x10s = f"{res['x10']}t" if res['x10'] else "-"
        # Extract trail label and risk from the compound label
        parts = cfg.label.rsplit(" risk=", 1)
        trail_label = parts[0] if parts else cfg.label
        print(f"  {trail_label:>28s}  {cfg.risk_pct:>3.0%}  {res['trades']:>4d}  "
              f"{res['wr']:>4.1%}  {res['avg_r']:>+.4f}  {res['total_r']:>+5.0f}  "
              f"{res['max_dd']:>5.1%}  {x10s:>5s}  {res.get('max_consec_loss',0):>6d}  "
              f"${res['final_eq']:>9,.0f}")

    if not x10_results:
        print(f"\n  NO configs reach x10!")
        print(f"\n  === TOP 15 BY FINAL EQUITY ===")
        by_eq = sorted(all_results, key=lambda r: -r["final_eq"])
        for res in by_eq[:15]:
            cfg = res["config"]
            parts = cfg.label.rsplit(" risk=", 1)
            trail_label = parts[0] if parts else cfg.label
            print(f"  {trail_label:>28s}  {cfg.risk_pct:>3.0%}  {res['trades']:>4d}  "
                  f"WR={res['wr']:.1%}  AvgR={res['avg_r']:+.4f}  "
                  f"DD={res['max_dd']:.1%}  Eq=${res['final_eq']:,.0f}")

    # ═══════════════════════════════════════════════════
    #  SECTION 2: DETAILED BREAKDOWN — TOP 3
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 2: DETAILED BREAKDOWN — TOP 3 CONFIGS")
    print("=" * 80)

    best = x10_results[:3] if x10_results else sorted(all_results, key=lambda r: -r["final_eq"])[:3]

    for idx, res in enumerate(best):
        cfg = res["config"]
        print(f"\n  ╔══ CONFIG #{idx+1}: {cfg.label} ══╗")
        print_config_result(res, indent="  ║ ")

        # Monthly breakdown with running equity
        if res.get("monthly"):
            print(f"  ║")
            print(f"  ║ MONTHLY:")
            print(f"  ║ {'Month':>8s}  {'#':>4s}  {'WR':>5s}  {'AvgR':>7s}  "
                  f"{'TotR':>6s}  {'StartEq':>9s}  {'EndEq':>9s}  {'Ret':>7s}")
            eq = cfg.start_equity
            for mk in sorted(res["monthly"].keys()):
                rv = res["monthly"][mk]
                w = sum(1 for r in rv if r > 0)
                wr = w / len(rv) if rv else 0
                avg = statistics.mean(rv) if rv else 0
                tot = sum(rv)
                s_eq = eq
                pk_eq = eq
                m_dd = 0.0
                for r in rv:
                    eq += eq * cfg.risk_pct * r
                    eq = max(eq, 0.01)
                    if eq > pk_eq:
                        pk_eq = eq
                    dd = (pk_eq - eq) / pk_eq if pk_eq > 0 else 0
                    m_dd = max(m_dd, dd)
                ret = (eq - s_eq) / s_eq if s_eq > 0 else 0
                print(f"  ║ {mk:>8s}  {len(rv):>4d}  {wr:>4.1%}  {avg:>+.4f}  "
                      f"{tot:>+5.1f}  ${s_eq:>8,.0f}  ${eq:>8,.0f}  {ret:>+6.1%}")

        # Session stats
        if res.get("session_stats"):
            print(f"  ║")
            print(f"  ║ SESSIONS:")
            for sn in SESSION_ORDER:
                ss = res["session_stats"][sn]
                if ss["trades"] == 0:
                    continue
                wr = ss["wins"] / ss["trades"] if ss["trades"] > 0 else 0
                avg = ss["total_r"] / ss["trades"] if ss["trades"] > 0 else 0
                print(f"  ║   {sn.upper():>7s}: {ss['trades']:>3d}t  "
                      f"WR={wr:.1%}  AvgR={avg:+.4f}  TotR={ss['total_r']:+.1f}")

        # Top/bottom pairs
        if res.get("pair_stats"):
            sorted_p = sorted(res["pair_stats"].items(),
                              key=lambda x: x[1]["total_r"], reverse=True)
            profitable = sum(1 for _, s in sorted_p if s["total_r"] > 0)
            print(f"  ║")
            print(f"  ║ PAIRS: {profitable}/{len(sorted_p)} profitable")
            print(f"  ║   TOP 5:")
            for p, s in sorted_p[:5]:
                wr = s["wins"] / s["trades"] if s["trades"] > 0 else 0
                print(f"  ║     {p:>12s}  {s['trades']:>2d}t  "
                      f"WR={wr:.0%}  TotR={s['total_r']:+.1f}")
            print(f"  ║   BOTTOM 5:")
            for p, s in sorted_p[-5:]:
                wr = s["wins"] / s["trades"] if s["trades"] > 0 else 0
                print(f"  ║     {p:>12s}  {s['trades']:>2d}t  "
                      f"WR={wr:.0%}  TotR={s['total_r']:+.1f}")

        print(f"  ╚{'═' * 60}╝")

    # ═══════════════════════════════════════════════════
    #  SECTION 3: WICK PENALTY (top 3)
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 3: WICK PENALTY SENSITIVITY")
    print("  What happens when stops get hit by wicks?")
    print("=" * 80)

    wick_levels = [0.0, 0.03, 0.05, 0.08, 0.10, 0.15]

    for idx, res in enumerate(best):
        cfg = res["config"]
        print(f"\n  Config #{idx+1}: {cfg.label}")
        print(f"  {'Wick':>6s}  {'#':>4s}  {'WR':>5s}  {'AvgR':>7s}  "
              f"{'MaxDD':>6s}  {'FinalEq':>10s}  {'x10':>5s}")
        print(f"  {'-'*6}  {'-'*4}  {'-'*5}  {'-'*7}  "
              f"{'-'*6}  {'-'*10}  {'-'*5}")

        for wp in wick_levels:
            wcfg = SimConfig(
                risk_pct=cfg.risk_pct,
                trail_activation_r=cfg.trail_activation_r,
                trail_distance_r=cfg.trail_distance_r,
                wick_penalty_r=wp,
                label=f"wp={wp}",
            )
            wr = simulate(pair_data, wcfg)
            x10s = f"{wr['x10']}t" if wr.get('x10') else "never"
            print(f"  {wp:>5.2f}R  {wr['trades']:>4d}  {wr['wr']:>4.1%}  "
                  f"{wr['avg_r']:>+.4f}  {wr['max_dd']:>5.1%}  "
                  f"${wr['final_eq']:>9,.0f}  {x10s:>5s}")

    # ═══════════════════════════════════════════════════
    #  SECTION 4: MONTE CARLO (top 3)
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 4: MONTE CARLO (2000 trials)")
    print("  What DD to plan for? (shuffled trade order)")
    print("=" * 80)

    for idx, res in enumerate(best):
        cfg = res["config"]
        r_vals = res["r_vals"]
        closed = res.get("closed_trades", [])
        d_risks = [t.dollar_risk for t in closed if t.r_multiple is not None]
        if not r_vals or not d_risks:
            continue

        print(f"\n  Config #{idx+1}: {cfg.label} ({len(r_vals)} trades)")
        mc = monte_carlo(r_vals, d_risks, 2000, cfg.start_equity)
        print(f"    Median DD:   {mc['median_dd']:>6.1%}")
        print(f"    75th %%ile:   {mc['p75_dd']:>6.1%}")
        print(f"    90th %%ile:   {mc['p90_dd']:>6.1%}")
        print(f"    95th %%ile:   {mc['p95_dd']:>6.1%}  <- PLAN FOR THIS")
        print(f"    99th %%ile:   {mc['p99_dd']:>6.1%}")
        print(f"    Worst:       {mc['worst_dd']:>6.1%}")
        print(f"    Bust (<$5):  {mc['bust_pct']:>6.1%}")
        print(f"    x10 chance:  {mc['x10_pct']:>6.1%}")
        if mc.get("x10_median"):
            print(f"    x10 median:  {mc['x10_median']}t")
        print(f"    Median eq:   ${mc['median_final']:>,.0f}")
        print(f"    10th %%ile:   ${mc['p10_final']:>,.0f}")

    # ═══════════════════════════════════════════════════
    #  SECTION 5: LOSS STREAKS — what it FEELS like
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  SECTION 5: LOSS STREAK REALITY CHECK")
    print("  What does a losing streak actually cost?")
    print("=" * 80)

    if best:
        res = best[0]
        cfg = res["config"]
        r_vals = res["r_vals"]

        # Find loss streaks
        streaks = []
        current_streak = []
        for r in r_vals:
            if r <= 0:
                current_streak.append(r)
            else:
                if current_streak:
                    streaks.append(list(current_streak))
                    current_streak = []
        if current_streak:
            streaks.append(current_streak)

        if streaks:
            streaks.sort(key=lambda s: -len(s))
            print(f"\n  Config: {cfg.label}")
            print(f"  Top 5 longest loss streaks:")
            for i, s in enumerate(streaks[:5]):
                total_r = sum(s)
                # DD from streak: (1 + risk*r1)(1 + risk*r2)... - 1
                mult = 1.0
                for r in s:
                    mult *= (1 + cfg.risk_pct * r)
                dd_pct = (1 - mult) * 100
                print(f"    #{i+1}: {len(s)} losses in a row | "
                      f"R={total_r:+.2f} | equity drop: {dd_pct:.1f}%")

    # ═══════════════════════════════════════════════════
    #  EXECUTIVE SUMMARY
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 80}")
    print("  EXECUTIVE SUMMARY")
    print("=" * 80)

    if best:
        res = best[0]
        cfg = res["config"]
        total_r = res["total_r"]
        print(f"\n  LOWEST-DD CONFIG THAT REACHES x10:")
        print(f"    Trail:       act={cfg.trail_activation_r}, dist={cfg.trail_distance_r}R")
        print(f"    Risk:        {cfg.risk_pct:.0%}")
        print(f"    Retest:      {'YES' if cfg.require_retest else 'NO'}")
        print(f"    Concurrent:  {cfg.max_concurrent}")
        print(f"    Fee:         {cfg.fee_per_trade_r}R")
        print(f"    ─────────────────")
        print(f"    Trades:      {res['trades']}")
        print(f"    Win rate:    {res['wr']:.1%}")
        print(f"    Avg R:       {res['avg_r']:+.4f}")
        print(f"    Total R:     {total_r:+.1f}")
        print(f"    Max DD:      {res['max_dd']:.1%}")
        print(f"    Consec loss: {res.get('max_consec_loss', 0)}")
        x10s = f"{res['x10']}t" if res.get('x10') else "never"
        print(f"    x10:         {x10s}")
        print(f"    Final eq:    ${res['final_eq']:,.2f}")

        if not x10_results:
            print(f"\n  WARNING: No config reaches x10 in 6 months!")
            print(f"  The strategy needs MORE EDGE (higher WR or bigger wins)")
            print(f"  or MORE TIME to compound.")

    elapsed = time.time() - t0
    print(f"\n  Runtime: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"  Data: {len(pair_data)} pairs, {total_candles:,} candles\n")


if __name__ == "__main__":
    main()
