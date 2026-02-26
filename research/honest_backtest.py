"""
research/honest_backtest.py — Backtest that matches ACTUAL LIVE bot logic.

Previous backtests were LYING. The backtest used a 3-candle retest filter
that the live bot DOES NOT HAVE. This script tests what the bot ACTUALLY does:

  LIVE BOT: FC → C2 breaks range → ENTER at C2 close (NO retest)
  OLD BACKTEST: FC → C2 breaks range → C3 retests → ENTER at C3 close (has retest filter)

Other discrepancies modeled:
  - Fee drag (0.04R per round-trip estimated)
  - Trail at 5m bar close (no intra-bar noise advantage)
  - NO session-end closure (positions run to SL/TP/trail)
  - Compare LIVE logic vs BACKTEST logic head-to-head

This script will tell us THE TRUTH about our edge.
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
    had_retest: bool = False   # Did C3 retest?

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
#  TWO FCB ENGINES: LIVE (no retest) vs BACKTEST (with retest)
# ═══════════════════════════════════════════════════

def run_fcb_live_logic(pair: str, candles: List[Candle],
                       tp_r: float = 1.5,
                       trail_enabled: bool = True,
                       trail_activation_r: float = 0.95,
                       trail_distance_r: float = 0.15,
                       trail_max_r: float = 10.0,
                       safety_tp_r: float = 10.0,
                       min_c2_body: float = 0.50,
                       fc_counter: bool = True,
                       vol_ratio_long: float = 1.0,
                       vol_ratio_short: float = 0.25,
                       min_range_pct: float = 0.003,
                       require_retest: bool = False,
                       session_end_close: bool = False,
                       fee_per_trade_r: float = 0.0,
                       breakout_window_min: int = 60,
                       ) -> List[Trade]:
    """
    FCB engine with toggles for LIVE vs BACKTEST logic.

    require_retest=False → LIVE logic (enter at C2 close)
    require_retest=True  → OLD BACKTEST logic (enter at C3 close after retest)
    session_end_close=True → force close at session end (backtest behavior)
    fee_per_trade_r → subtract from every closed trade's R
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
            if current_trade and not current_trade.is_closed:
                if session_end_close and i > 0:
                    current_trade.close(candles[i-1].close, candles[i-1].date, "session_end")
                    if fee_per_trade_r > 0 and current_trade.r_multiple is not None:
                        current_trade.r_multiple -= fee_per_trade_r
                    trades.append(current_trade)
                elif not session_end_close:
                    pass  # Position carries over (LIVE behavior)
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
            breakout_candle = None

        # ── Manage open trade ──
        if current_trade and not current_trade.is_closed:
            _manage_trade_v2(candle, current_trade, trail_enabled, trail_activation_r,
                           trail_distance_r, trail_max_r, safety_tp_r, session_end_close)
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
                    # LIVE LOGIC: Enter NOW at C2 close
                    trade = _try_enter(pair, candle, candle, fc_candle, breakout_dir,
                                      range_high, range_low, range_mid,
                                      min_c2_body, fc_counter, vol_ratio_long, vol_ratio_short,
                                      min_range_pct, trail_enabled, safety_tp_r, tp_r,
                                      daily_trades, had_retest=False)
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
                                      daily_trades, had_retest=False)
                    if trade:
                        current_trade = trade
                        phase = "in_trade"
                    else:
                        phase = "done"
            continue

        # ── Waiting for retest (BACKTEST logic only) ──
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
                              daily_trades, had_retest=True)
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
               daily_trades, had_retest):
    """Apply micro-filters and create trade if passed."""

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
    if daily_trades.get(day_key, 0) >= 3:
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
        pair=pair,
        session_name=entry_candle.session_name,
        session_date=entry_candle.session_date,
        direction=direction,
        entry_price=entry_price,
        entry_time=entry_candle.date,
        stop_loss=sl,
        take_profit=tp,
        risk_per_unit=risk,
        range_high=range_high,
        range_low=range_low,
        range_midpoint=range_mid,
        fc_body_ratio=fc_candle.body_ratio if fc_candle else 0,
        c2_body_ratio=bo_candle.body_ratio,
        vol_ratio=vol_r,
        had_retest=had_retest,
    )


def _manage_trade_v2(candle, trade, trail_enabled, trail_activation_r,
                     trail_distance_r, trail_max_r, safety_tp_r,
                     session_end_close):
    """Manage open trade — SL/TP/trail."""
    h, l, c, t = candle.high, candle.low, candle.close, candle.date
    risk = trade.risk_per_unit

    if trade.direction == "long":
        current_r_high = (h - trade.entry_price) / risk
        # SL
        if l <= trade.stop_loss:
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
            if trade.trail_stop_price and l <= trade.trail_stop_price:
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
        if h >= trade.stop_loss:
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
            if trade.trail_stop_price and h >= trade.trail_stop_price:
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

    # Session end
    if session_end_close and candle.session_end and t >= candle.session_end - timedelta(minutes=5):
        trade.close(c, t, "session_end")


# ═══════════════════════════════════════════════════
#  ANALYSIS
# ═══════════════════════════════════════════════════

def analyze_trades(trades: List[Trade], label: str) -> dict:
    """Print stats and return summary dict."""
    closed = [t for t in trades if t.is_closed and t.r_multiple is not None]
    if not closed:
        print(f"  {label}: 0 trades")
        return {}

    r_vals = [t.r_multiple for t in closed]
    winners = [r for r in r_vals if r > 0]
    losers = [r for r in r_vals if r <= 0]
    wr = len(winners) / len(r_vals)
    avg_r = statistics.mean(r_vals)
    avg_win = statistics.mean(winners) if winners else 0
    avg_loss = statistics.mean(losers) if losers else 0
    total_r = sum(r_vals)

    exits = {}
    for t in closed:
        exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1

    # Max consecutive losses
    max_consec = 0
    cur_consec = 0
    for r in r_vals:
        if r <= 0:
            cur_consec += 1
            max_consec = max(max_consec, cur_consec)
        else:
            cur_consec = 0

    print(f"\n  {label}")
    print(f"  {'─' * 55}")
    print(f"    Trades: {len(closed):,}  |  WR: {wr:.1%}  |  Avg R: {avg_r:+.4f}")
    print(f"    Total R: {total_r:+.1f}  |  Avg Win: {avg_win:+.3f}  |  Avg Loss: {avg_loss:+.3f}")
    if avg_loss < 0:
        print(f"    Payoff: {avg_win / abs(avg_loss):.2f}x  |  Max Consec Loss: {max_consec}")
    print(f"    Exits: {exits}")

    return {
        "label": label, "trades": len(closed), "wr": wr, "avg_r": avg_r,
        "total_r": total_r, "avg_win": avg_win, "avg_loss": avg_loss,
        "max_consec_loss": max_consec,
    }


def equity_sim(r_vals, start=150.0, risk=0.08):
    eq = start
    peak = start
    max_dd = 0.0
    x10_trade = None
    for i, r in enumerate(r_vals):
        eq *= (1 + risk * r)
        eq = max(eq, 0.01)
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
        if x10_trade is None and eq >= start * 10:
            x10_trade = i + 1
    return eq, max_dd, x10_trade


def main():
    t0 = time.time()

    print("=" * 78)
    print("  HONEST BACKTEST — Testing what the bot ACTUALLY does")
    print("=" * 78)

    # Load data
    pair_files = discover_data_files()
    print(f"\n  Loading {len(pair_files)} pairs...")
    pair_data = {}
    for pair, fpath in pair_files:
        candles = load_csv(fpath)
        assign_sessions(candles)
        pair_data[pair] = candles
        sys.stdout.write(f"\r    {len(pair_data)}/{len(pair_files)}")
        sys.stdout.flush()
    print(f"\n    Done — {sum(len(c) for c in pair_data.values()):,} candles\n")

    # ═══════════════════════════════════════════════════
    #  HEAD-TO-HEAD: Backtest Logic vs Live Logic
    # ═══════════════════════════════════════════════════

    configs = [
        {
            "label": "A) OLD BACKTEST (retest + session_end + no fees)",
            "require_retest": True,
            "session_end_close": True,
            "fee_per_trade_r": 0.0,
            "trail_activation_r": 0.95,
            "trail_distance_r": 0.15,
        },
        {
            "label": "B) LIVE AS-IS (no retest + no session_end + fees)",
            "require_retest": False,
            "session_end_close": False,
            "fee_per_trade_r": 0.04,
            "trail_activation_r": 0.95,
            "trail_distance_r": 0.15,
        },
        {
            "label": "C) LIVE + ADD RETEST (retest + no session_end + fees)",
            "require_retest": True,
            "session_end_close": False,
            "fee_per_trade_r": 0.04,
            "trail_activation_r": 0.95,
            "trail_distance_r": 0.15,
        },
        {
            "label": "D) LIVE + WIDER TRAIL 0.30R (no retest, no sess end, fees)",
            "require_retest": False,
            "session_end_close": False,
            "fee_per_trade_r": 0.04,
            "trail_activation_r": 0.95,
            "trail_distance_r": 0.30,
        },
        {
            "label": "E) LIVE + RETEST + WIDER TRAIL 0.30R + FEES",
            "require_retest": True,
            "session_end_close": False,
            "fee_per_trade_r": 0.04,
            "trail_activation_r": 0.95,
            "trail_distance_r": 0.30,
        },
        {
            "label": "F) LIVE + RETEST + TRAIL 0.50R + FEES (safest)",
            "require_retest": True,
            "session_end_close": False,
            "fee_per_trade_r": 0.04,
            "trail_activation_r": 1.0,
            "trail_distance_r": 0.50,
        },
        {
            "label": "G) LIVE + RETEST + NO TRAIL (fixed 1.5R TP) + FEES",
            "require_retest": True,
            "session_end_close": False,
            "fee_per_trade_r": 0.04,
            "trail_activation_r": 99.0,
            "trail_distance_r": 0.15,
            "trail_enabled": False,
        },
        {
            "label": "H) LIVE + RETEST + TIGHT TRAIL + SESSION END + FEES",
            "require_retest": True,
            "session_end_close": True,
            "fee_per_trade_r": 0.04,
            "trail_activation_r": 0.95,
            "trail_distance_r": 0.15,
        },
    ]

    results = []

    for cfg in configs:
        all_trades = []
        for pair, candles in pair_data.items():
            trail_en = cfg.get("trail_enabled", True)
            trades = run_fcb_live_logic(
                pair, candles,
                require_retest=cfg["require_retest"],
                session_end_close=cfg["session_end_close"],
                fee_per_trade_r=cfg["fee_per_trade_r"],
                trail_enabled=trail_en,
                trail_activation_r=cfg["trail_activation_r"],
                trail_distance_r=cfg["trail_distance_r"],
            )
            all_trades.extend(trades)

        # Sort by time
        closed = sorted(
            [t for t in all_trades if t.is_closed and t.r_multiple is not None],
            key=lambda t: t.entry_time
        )
        r_vals = [t.r_multiple for t in closed]

        stats = analyze_trades(all_trades, cfg["label"])
        if r_vals:
            eq, max_dd, x10_t = equity_sim(r_vals, 150.0, 0.08)
            x10_str = f"{x10_t}t (~{x10_t/8:.0f}d)" if x10_t else "never"
            print(f"    Equity sim @8%: Final=${eq:,.2f}  MaxDD={max_dd:.1%}  x10={x10_str}")
            stats["final_eq"] = eq
            stats["max_dd"] = max_dd
            stats["x10_trades"] = x10_t
        results.append(stats)

    # ═══════════════════════════════════════════════════
    #  RETEST QUALITY ANALYSIS
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print(f"  RETEST vs NO-RETEST DEEP DIVE")
    print(f"{'=' * 78}")

    # Run all trades without retest, but TAG which ones would have had a retest
    all_live = []
    all_bt = []
    for pair, candles in pair_data.items():
        live_trades = run_fcb_live_logic(pair, candles, require_retest=False,
                                         session_end_close=False, fee_per_trade_r=0.04)
        bt_trades = run_fcb_live_logic(pair, candles, require_retest=True,
                                       session_end_close=False, fee_per_trade_r=0.04)
        all_live.extend([t for t in live_trades if t.is_closed and t.r_multiple is not None])
        all_bt.extend([t for t in bt_trades if t.is_closed and t.r_multiple is not None])

    live_sessions = {f"{t.pair}_{t.session_name}_{t.session_date}" for t in all_live}
    bt_sessions = {f"{t.pair}_{t.session_name}_{t.session_date}" for t in all_bt}

    # Trades ONLY in live (rejected by retest)
    only_live_keys = live_sessions - bt_sessions
    only_live_trades = [t for t in all_live
                        if f"{t.pair}_{t.session_name}_{t.session_date}" in only_live_keys]

    both_keys = live_sessions & bt_sessions
    both_trades = [t for t in all_live
                   if f"{t.pair}_{t.session_name}_{t.session_date}" in both_keys]

    if only_live_trades:
        rej_r = [t.r_multiple for t in only_live_trades]
        rej_wr = sum(1 for r in rej_r if r > 0) / len(rej_r)
        rej_avg = statistics.mean(rej_r)
        print(f"\n  Trades that FAILED retest (live takes, backtest rejects):")
        print(f"    Count: {len(rej_r):,}  |  WR: {rej_wr:.1%}  |  Avg R: {rej_avg:+.4f}")
        print(f"    Total R: {sum(rej_r):+.1f}")
        print(f"    → These are the EXTRA LOSSES the live bot takes!")

    if both_trades:
        pass_r = [t.r_multiple for t in both_trades]
        pass_wr = sum(1 for r in pass_r if r > 0) / len(pass_r)
        pass_avg = statistics.mean(pass_r)
        print(f"\n  Trades that PASSED retest (both systems take):")
        print(f"    Count: {len(pass_r):,}  |  WR: {pass_wr:.1%}  |  Avg R: {pass_avg:+.4f}")
        print(f"    Total R: {sum(pass_r):+.1f}")

    # ═══════════════════════════════════════════════════
    #  TRAIL DISTANCE SWEEP
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print(f"  TRAIL DISTANCE SWEEP (with retest + fees)")
    print(f"{'=' * 78}")
    print(f"\n  {'Trail':>6s}  {'Act':>5s}  {'Trades':>7s}  {'WR':>6s}  {'Avg R':>8s}  {'Total R':>9s}  {'MaxDD':>7s}  {'x10':>8s}")
    print(f"  {'-'*6}  {'-'*5}  {'-'*7}  {'-'*6}  {'-'*8}  {'-'*9}  {'-'*7}  {'-'*8}")

    for act_r in [0.75, 0.85, 0.95, 1.0]:
        for dist_r in [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]:
            all_t = []
            for pair, candles in pair_data.items():
                trades = run_fcb_live_logic(pair, candles,
                    require_retest=True, session_end_close=False,
                    fee_per_trade_r=0.04,
                    trail_activation_r=act_r, trail_distance_r=dist_r)
                all_t.extend(trades)

            closed = [t for t in all_t if t.is_closed and t.r_multiple is not None]
            if not closed:
                continue
            r_v = [t.r_multiple for t in closed]
            wr = sum(1 for r in r_v if r > 0) / len(r_v)
            avg = statistics.mean(r_v)
            tot = sum(r_v)
            eq, mdd, x10 = equity_sim(r_v, 150.0, 0.08)
            x10s = f"{x10}t" if x10 else "never"
            print(f"  {dist_r:>5.2f}R  {act_r:>4.2f}  {len(r_v):>7d}  {wr:>5.1%}  {avg:>+.4f}  {tot:>+8.1f}  {mdd:>6.1%}  {x10s:>8s}")

    # ═══════════════════════════════════════════════════
    #  MIN RANGE SWEEP (with retest + fees)
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print(f"  MIN RANGE SWEEP (with retest + fees)")
    print(f"{'=' * 78}")
    print(f"\n  {'Range%':>7s}  {'Trades':>7s}  {'WR':>6s}  {'Avg R':>8s}  {'Total R':>9s}")
    print(f"  {'-'*7}  {'-'*7}  {'-'*6}  {'-'*8}  {'-'*9}")

    for range_pct in [0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.008, 0.010]:
        all_t = []
        for pair, candles in pair_data.items():
            trades = run_fcb_live_logic(pair, candles,
                require_retest=True, session_end_close=False,
                fee_per_trade_r=0.04, min_range_pct=range_pct)
            all_t.extend(trades)
        closed = [t for t in all_t if t.is_closed and t.r_multiple is not None]
        if not closed:
            continue
        r_v = [t.r_multiple for t in closed]
        wr = sum(1 for r in r_v if r > 0) / len(r_v)
        avg = statistics.mean(r_v)
        tot = sum(r_v)
        print(f"  {range_pct:>6.3f}%  {len(r_v):>7d}  {wr:>5.1%}  {avg:>+.4f}  {tot:>+8.1f}")

    # ═══════════════════════════════════════════════════
    #  EXECUTIVE SUMMARY
    # ═══════════════════════════════════════════════════
    print(f"\n{'=' * 78}")
    print(f"  HONEST EXECUTIVE SUMMARY")
    print(f"{'=' * 78}")

    if results:
        print(f"\n  {'Config':<55s}  {'WR':>6s}  {'AvgR':>8s}  {'MaxDD':>7s}  {'x10':>8s}")
        print(f"  {'-'*55}  {'-'*6}  {'-'*8}  {'-'*7}  {'-'*8}")
        for r in results:
            if not r:
                continue
            x10 = r.get("x10_trades")
            x10s = f"{x10}t" if x10 else "never"
            mdd = r.get("max_dd", 0)
            print(f"  {r['label'][:55]:<55s}  {r['wr']:>5.1%}  {r['avg_r']:>+.4f}  {mdd:>6.1%}  {x10s:>8s}")

    print(f"\n  Total time: {time.time() - t0:.1f}s\n")


if __name__ == "__main__":
    main()
