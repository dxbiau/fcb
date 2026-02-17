"""
live/trade_logger.py — Structured JSONL trade logger for analysis.

Captures EVERY data point needed to synthesize a superior config from live results.
Writes one JSON object per line to live/logs/trades.jsonl — ultra-compact, grep-friendly.

Event types:
  ENTRY           — trade opened (full context: FC, slip, pair class, risk, equity)
  GUARDIAN_SL     — SL ratcheted (tier or trail, peak R, new SL)
  TRAIL_ACTIVATE  — trail engaged at activation threshold
  C3_CHECK        — C3 fakeout check result (body%, reversal detected?)
  EXIT            — trade closed (close price, peak R, exit R, reason, duration)
  SKIP            — breakout detected but trade skipped (reason)
  SESSION_OPEN    — session boundary marker
  SESSION_CLOSE   — session boundary with aggregate stats

Data points captured per trade lifecycle (for analysis):
  ┌─────────────────────────────────────────────────────┐
  │ ENTRY CONTEXT                                       │
  │  fc_high, fc_low, fc_range_pct, fc_midpoint         │
  │  direction, entry_price, fill_price, slip_r          │
  │  sl, tp, exchange_tp, risk_per_unit, fee_r           │
  │  qty, risk_usd, risk_pct, pair_class (A/B)           │
  │  equity_before, session, c2_body_ratio               │
  ├─────────────────────────────────────────────────────┤
  │ GUARDIAN TRAIL                                       │
  │  every SL ratchet with timestamp, peak_r, current_r  │
  │  trail activation timestamp and R level               │
  │  tier progression (T0→T1→T2→trail)                    │
  ├─────────────────────────────────────────────────────┤
  │ EXIT CONTEXT                                         │
  │  close_price, peak_r, exit_r, exit_reason             │
  │  duration_seconds, equity_after, pnl_r, pnl_usd      │
  │  guardian_closed (bool), trail_was_active (bool)       │
  │  r_left_on_table (peak_r - exit_r)                    │
  └─────────────────────────────────────────────────────┘

Analysis possible at 500 trades:
  - Per-pair edge (which pairs carry the system)
  - Per-session split (Asia vs London vs NY)
  - Optimal trail distance from peak→exit scatter
  - FC range vs outcome (best range bands)
  - Slip impact on expectancy
  - C3 fakeout precision/recall
  - Time-in-trade vs R correlation
  - Guardian tier effectiveness
  - Scale-out net benefit
  - Pair class A/B calibration

At 1,000+ trades: per-pair-per-session, regime detection, adaptive sizing.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from live.config import LOG_DIR

TRADE_JSONL = os.path.join(LOG_DIR, "trades.jsonl")
_lock = threading.Lock()


def _emit(event: dict):
    """Append one JSON line to the trade log. Thread-safe."""
    event["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    os.makedirs(os.path.dirname(TRADE_JSONL), exist_ok=True)
    with _lock:
        with open(TRADE_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")


# ═══════════════════════════════════════════════════════════
#  ENTRY EVENT
# ═══════════════════════════════════════════════════════════

def log_entry(
    symbol: str, session: str, direction: str,
    fill_price: float, qty: float,
    sl: float, tp: float, exchange_tp: float,
    risk_per_unit: float, fee_r: float,
    risk_usd: float, risk_pct: float,
    equity: float, pair_class: str,
    fc_high: float, fc_low: float, fc_range_pct: float, fc_midpoint: float,
    slip_r: float = 0.0,
    c2_close: float = 0.0, c2_body_ratio: float = 0.0,
    order_id: str = "",
    # ── NEW: enriched context fields ──
    fc_open: float = 0.0, fc_close: float = 0.0, fc_volume: float = 0.0,
    c2_open: float = 0.0, c2_high: float = 0.0, c2_low: float = 0.0,
    c2_volume: float = 0.0,
    bid: float = 0.0, ask: float = 0.0,
    open_positions: int = 0, entries_today: int = 0,
    consec_wins: int = 0, consec_losses: int = 0,
    live_wins: int = 0, live_losses: int = 0,
    day_of_week: int = -1,
):
    """Log trade entry with complete first-candle and market context."""
    d = {
        "e": "ENTRY",
        "sym": symbol,
        "ses": session,
        "dir": direction,
        "fill": round(fill_price, 8),
        "qty": round(qty, 6),
        "sl": round(sl, 8),
        "tp": round(tp, 8),
        "xtp": round(exchange_tp, 8),
        "rpu": round(risk_per_unit, 8),
        "fee_r": round(fee_r, 4),
        "risk$": round(risk_usd, 2),
        "risk%": round(risk_pct, 4),
        "eq": round(equity, 2),
        "cls": pair_class,
        # First candle (full OHLCV)
        "fc_o": round(fc_open, 8),
        "fc_c": round(fc_close, 8),
        "fc_h": round(fc_high, 8),
        "fc_l": round(fc_low, 8),
        "fc_rng": round(fc_range_pct, 6),
        "fc_mid": round(fc_midpoint, 8),
        "fc_vol": round(fc_volume, 2),
        # Candle 2 (full OHLCV)
        "c2_o": round(c2_open, 8),
        "c2_h": round(c2_high, 8),
        "c2_l": round(c2_low, 8),
        "c2_cl": round(c2_close, 8),
        "c2_br": round(c2_body_ratio, 4),
        "c2_vol": round(c2_volume, 2),
        # Market microstructure
        "slip_r": round(slip_r, 4),
        "bid": round(bid, 8),
        "ask": round(ask, 8),
        "spread": round(ask - bid, 8) if (bid > 0 and ask > 0) else 0,
        # Portfolio context
        "open_pos": open_positions,
        "ent_today": entries_today,
        # Pair history
        "cw": consec_wins,
        "cl": consec_losses,
        "lw": live_wins,
        "ll": live_losses,
        # Temporal
        "dow": day_of_week,
        "oid": order_id,
    }
    _emit(d)


# ═══════════════════════════════════════════════════════════
#  GUARDIAN EVENTS
# ═══════════════════════════════════════════════════════════

def log_guardian_sl(
    symbol: str, current_r: float, peak_r: float,
    new_sl: float, old_sl: float, reason: str,
    # ── NEW: enriched context ──
    direction: str = "", session: str = "",
    current_price: float = 0.0, entry_price: float = 0.0,
    tier_idx: int = -1, polls: int = 0,
    secs_since_entry: float = 0.0,
):
    """Log every SL ratchet from the guardian."""
    _emit({
        "e": "GUARDIAN_SL",
        "sym": symbol,
        "dir": direction,
        "ses": session,
        "cur_r": round(current_r, 4),
        "peak_r": round(peak_r, 4),
        "new_sl": round(new_sl, 8),
        "old_sl": round(old_sl, 8),
        "rsn": reason,
        "price": round(current_price, 8),
        "entry": round(entry_price, 8),
        "tier": tier_idx,
        "polls": polls,
        "dur_s": round(secs_since_entry, 0),
    })


def log_trail_activate(
    symbol: str, current_r: float, peak_r: float,
    # ── NEW: enriched context ──
    direction: str = "", session: str = "",
    current_price: float = 0.0, entry_price: float = 0.0,
    secs_since_entry: float = 0.0,
):
    """Log trail engagement — the moment trailing starts."""
    _emit({
        "e": "TRAIL_ACTIVATE",
        "sym": symbol,
        "dir": direction,
        "ses": session,
        "cur_r": round(current_r, 4),
        "peak_r": round(peak_r, 4),
        "price": round(current_price, 8),
        "entry": round(entry_price, 8),
        "dur_s": round(secs_since_entry, 0),
    })


# ═══════════════════════════════════════════════════════════
#  C3 FAKEOUT CHECK
# ═══════════════════════════════════════════════════════════

def log_c3_check(
    symbol: str, direction: str, current_r: float,
    c3_body_pct: float, is_reversal: bool, action: str,
    # ── NEW: enriched context ──
    session: str = "",
    c3_open: float = 0.0, c3_close: float = 0.0,
    c3_high: float = 0.0, c3_low: float = 0.0,
    c3_volume: float = 0.0,
    entry_price: float = 0.0, peak_r: float = 0.0,
    elapsed_min: float = 0.0,
):
    """Log C3 fakeout check result."""
    _emit({
        "e": "C3_CHECK",
        "sym": symbol,
        "dir": direction,
        "ses": session,
        "cur_r": round(current_r, 4),
        "c3_body": round(c3_body_pct, 4),
        "rev": is_reversal,
        "act": action,
        "c3_o": round(c3_open, 8),
        "c3_c": round(c3_close, 8),
        "c3_h": round(c3_high, 8),
        "c3_l": round(c3_low, 8),
        "c3_vol": round(c3_volume, 2),
        "entry": round(entry_price, 8),
        "peak_r": round(peak_r, 4),
        "elap_m": round(elapsed_min, 1),
    })


# ═══════════════════════════════════════════════════════════
#  EXIT EVENT
# ═══════════════════════════════════════════════════════════

def log_exit(
    symbol: str, session: str, direction: str,
    entry_price: float, close_price: float,
    pnl_r: float, pnl_usd: float,
    peak_r: float, exit_r: float,
    duration_secs: float,
    equity_after: float,
    exit_reason: str,
    guardian_closed: bool = False,
    trail_was_active: bool = False,
    pair_class: str = "",
    fc_range_pct: float = 0.0,
    slip_r: float = 0.0,
    # ── NEW: enriched context fields ──
    qty: float = 0.0,
    risk_per_unit: float = 0.0,
    fee_r: float = 0.0,
    risk_pct: float = 0.0,
    original_sl: float = 0.0,
    final_sl: float = 0.0,
    peak_price: float = 0.0,
    entry_time: str = "",
    c3_exited: bool = False,
    c3_checked: bool = False,
    guardian_tier: int = -1,
    guardian_polls: int = 0,
    total_trades: int = 0,
    cumulative_r: float = 0.0,
    open_positions: int = 0,
    day_of_week: int = -1,
):
    """Log trade exit with full lifecycle data."""
    _emit({
        "e": "EXIT",
        "sym": symbol,
        "ses": session,
        "dir": direction,
        "entry": round(entry_price, 8),
        "close": round(close_price, 8),
        "pnl_r": round(pnl_r, 4),
        "pnl$": round(pnl_usd, 2),
        "peak_r": round(peak_r, 4),
        "exit_r": round(exit_r, 4),
        "left_r": round(peak_r - exit_r, 4),
        "dur_s": round(duration_secs, 0),
        "eq": round(equity_after, 2),
        "rsn": exit_reason,
        "gc": guardian_closed,
        "trail": trail_was_active,
        "cls": pair_class,
        "fc_rng": round(fc_range_pct, 6),
        "slip_r": round(slip_r, 4),
        # Sizing & fees
        "qty": round(qty, 6),
        "rpu": round(risk_per_unit, 8),
        "fee_r": round(fee_r, 4),
        "risk%": round(risk_pct, 4),
        # SL lifecycle
        "orig_sl": round(original_sl, 8),
        "final_sl": round(final_sl, 8),
        # Peak details
        "peak_px": round(peak_price, 8),
        # Timing
        "ent_ts": entry_time,
        # C3 detection
        "c3_exit": c3_exited,
        "c3_chk": c3_checked,
        # Guardian lifecycle
        "g_tier": guardian_tier,
        "g_polls": guardian_polls,
        # Portfolio context
        "trade_n": total_trades,
        "cum_r": round(cumulative_r, 3),
        "open_pos": open_positions,
        "dow": day_of_week,
    })


# ═══════════════════════════════════════════════════════════
#  SKIP EVENT
# ═══════════════════════════════════════════════════════════

def log_skip(
    symbol: str, session: str, direction: str,
    fc_high: float, fc_low: float, fc_range_pct: float,
    slip_r: float, reason: str,
    c2_close: float = 0.0,
    # ── NEW: enriched context ──
    pair_class: str = "",
    equity: float = 0.0,
    risk_pct: float = 0.0,
    fc_open: float = 0.0, fc_close: float = 0.0, fc_volume: float = 0.0,
    fc_midpoint: float = 0.0,
    c2_open: float = 0.0, c2_high: float = 0.0, c2_low: float = 0.0,
    c2_body_ratio: float = 0.0, c2_volume: float = 0.0,
    signal_qty: float = 0.0, signal_fee_r: float = 0.0,
    open_positions: int = 0,
):
    """Log a skipped breakout signal (for counterfactual analysis)."""
    _emit({
        "e": "SKIP",
        "sym": symbol,
        "ses": session,
        "dir": direction,
        "fc_o": round(fc_open, 8),
        "fc_c": round(fc_close, 8),
        "fc_h": round(fc_high, 8),
        "fc_l": round(fc_low, 8),
        "fc_rng": round(fc_range_pct, 6),
        "fc_mid": round(fc_midpoint, 8),
        "fc_vol": round(fc_volume, 2),
        "slip_r": round(slip_r, 4),
        "rsn": reason,
        "c2_o": round(c2_open, 8),
        "c2_h": round(c2_high, 8),
        "c2_l": round(c2_low, 8),
        "c2_cl": round(c2_close, 8),
        "c2_br": round(c2_body_ratio, 4),
        "c2_vol": round(c2_volume, 2),
        "cls": pair_class,
        "eq": round(equity, 2),
        "risk%": round(risk_pct, 4),
        "sig_qty": round(signal_qty, 6),
        "sig_fee": round(signal_fee_r, 4),
        "open_pos": open_positions,
    })


# ═══════════════════════════════════════════════════════════
#  SESSION MARKERS
# ═══════════════════════════════════════════════════════════

def log_session_open(
    session: str, equity: float, pair_count: int,
    # ── NEW: enriched context ──
    pending_positions: int = 0,
    total_trades: int = 0,
    day_start_equity: float = 0.0,
    entries_today: int = 0,
    wins_today: int = 0, losses_today: int = 0,
    class_a_count: int = 0, class_b_count: int = 0,
):
    """Log session open boundary."""
    _emit({
        "e": "SESSION_OPEN",
        "ses": session,
        "eq": round(equity, 2),
        "pairs": pair_count,
        "pend": pending_positions,
        "tot_trades": total_trades,
        "day_eq": round(day_start_equity, 2),
        "ent_today": entries_today,
        "w_today": wins_today,
        "l_today": losses_today,
        "cls_a": class_a_count,
        "cls_b": class_b_count,
    })


def log_session_close(
    session: str, equity: float,
    entries: int, wins: int, losses: int,
    pnl_r: float = 0.0, pnl_usd: float = 0.0,
    # ── NEW: enriched context ──
    pending_positions: int = 0,
    total_trades: int = 0,
    skips: int = 0,
):
    """Log session close with aggregate stats."""
    _emit({
        "e": "SESSION_CLOSE",
        "ses": session,
        "eq": round(equity, 2),
        "entries": entries,
        "wins": wins,
        "losses": losses,
        "pnl_r": round(pnl_r, 4),
        "pnl$": round(pnl_usd, 2),
        "pend": pending_positions,
        "tot_trades": total_trades,
        "skips": skips,
    })


# ═══════════════════════════════════════════════════════════
#  HEARTBEAT (periodic equity snapshot)
# ═══════════════════════════════════════════════════════════

def log_heartbeat(
    equity: float, pending: int, session: str = "",
    # ── NEW: enriched context ──
    total_trades: int = 0, total_pnl_r: float = 0.0,
    wins_today: int = 0, losses_today: int = 0,
    position_details: list = None,
):
    """Equity snapshot for equity curve reconstruction."""
    d = {
        "e": "HEARTBEAT",
        "eq": round(equity, 2),
        "pend": pending,
        "ses": session,
        "tot_trades": total_trades,
        "tot_r": round(total_pnl_r, 3),
        "w_today": wins_today,
        "l_today": losses_today,
    }
    # Per-position R snapshots (lightweight: sym + current_r only)
    if position_details:
        d["pos"] = position_details[:10]  # cap at 10 to keep line short
    _emit(d)


# ═══════════════════════════════════════════════════════════
#  PAIR CLASS CHANGE
# ═══════════════════════════════════════════════════════════

def log_pair_class_change(
    symbol: str, old_class: str, new_class: str,
    consec_wins: int = 0, consec_losses: int = 0,
    live_wins: int = 0, live_losses: int = 0,
    trigger: str = "",
):
    """Log promotion/demotion of pair class."""
    _emit({
        "e": "CLASS_CHANGE",
        "sym": symbol,
        "old": old_class,
        "new": new_class,
        "cw": consec_wins,
        "cl": consec_losses,
        "lw": live_wins,
        "ll": live_losses,
        "trigger": trigger,
    })


# ═══════════════════════════════════════════════════════════
#  DAY ROLLOVER
# ═══════════════════════════════════════════════════════════

def log_day_rollover(
    date: str, start_equity: float, end_equity: float,
    total_entries: int = 0, wins: int = 0, losses: int = 0,
    pnl_r: float = 0.0, pnl_usd: float = 0.0,
):
    """Log end-of-day summary when day rolls over."""
    _emit({
        "e": "DAY_ROLLOVER",
        "date": date,
        "start_eq": round(start_equity, 2),
        "end_eq": round(end_equity, 2),
        "entries": total_entries,
        "wins": wins,
        "losses": losses,
        "pnl_r": round(pnl_r, 4),
        "pnl$": round(pnl_usd, 2),
    })


# ═══════════════════════════════════════════════════════════
#  READER — parse the JSONL for analysis / dashboard
# ═══════════════════════════════════════════════════════════

def read_all_events() -> list:
    """Read all events from the JSONL file. Returns list of dicts."""
    if not os.path.exists(TRADE_JSONL):
        return []
    events = []
    with open(TRADE_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # skip corrupted lines
    return events


def read_trades() -> list:
    """Read only completed trade pairs (ENTRY + EXIT matched by symbol+session window).

    Returns list of dicts with merged entry+exit data — ready for analysis.
    """
    events = read_all_events()
    entries = {}  # key: symbol → latest unmatched entry
    trades = []

    for ev in events:
        etype = ev.get("e")

        if etype == "ENTRY":
            sym = ev.get("sym", "")
            entries[sym] = ev

        elif etype == "EXIT":
            sym = ev.get("sym", "")
            entry_ev = entries.pop(sym, None)
            trade = {
                # Entry data
                "symbol": sym,
                "session": ev.get("ses", ""),
                "direction": ev.get("dir", ""),
                "entry_price": ev.get("entry", 0),
                "close_price": ev.get("close", 0),
                "entry_time": entry_ev.get("ts", "") if entry_ev else "",
                "exit_time": ev.get("ts", ""),
                # First candle
                "fc_high": entry_ev.get("fc_h", 0) if entry_ev else 0,
                "fc_low": entry_ev.get("fc_l", 0) if entry_ev else 0,
                "fc_range_pct": entry_ev.get("fc_rng", 0) if entry_ev else ev.get("fc_rng", 0),
                # Sizing
                "qty": entry_ev.get("qty", 0) if entry_ev else 0,
                "risk_pct": entry_ev.get("risk%", 0) if entry_ev else 0,
                "risk_usd": entry_ev.get("risk$", 0) if entry_ev else 0,
                "pair_class": entry_ev.get("cls", "") if entry_ev else ev.get("cls", ""),
                "equity_before": entry_ev.get("eq", 0) if entry_ev else 0,
                "equity_after": ev.get("eq", 0),
                # Slip
                "slip_r": entry_ev.get("slip_r", 0) if entry_ev else ev.get("slip_r", 0),
                "c2_body_ratio": entry_ev.get("c2_br", 0) if entry_ev else 0,
                # Outcome
                "pnl_r": ev.get("pnl_r", 0),
                "pnl_usd": ev.get("pnl$", 0),
                "peak_r": ev.get("peak_r", 0),
                "exit_r": ev.get("exit_r", 0),
                "r_left_on_table": ev.get("left_r", 0),
                "duration_secs": ev.get("dur_s", 0),
                "exit_reason": ev.get("rsn", ""),
                "guardian_closed": ev.get("gc", False),
                "trail_was_active": ev.get("trail", False),
            }
            trades.append(trade)

    return trades


def compute_stats(trades: list) -> dict:
    """Compute performance statistics from trade list.

    Returns a dict suitable for JSON serialization / dashboard display.
    """
    if not trades:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0, "expectancy_r": 0, "total_pnl_r": 0, "total_pnl_usd": 0,
            "profit_factor": 0, "avg_win_r": 0, "avg_loss_r": 0,
            "avg_peak_r": 0, "avg_r_left": 0,
            "best_r": 0, "worst_r": 0,
            "avg_duration_min": 0,
            "by_session": {}, "by_pair": {},
        }

    total = len(trades)
    wins = [t for t in trades if t["pnl_r"] > 0]
    losses = [t for t in trades if t["pnl_r"] <= 0]
    n_wins = len(wins)
    n_losses = len(losses)

    total_pnl_r = sum(t["pnl_r"] for t in trades)
    total_pnl_usd = sum(t["pnl_usd"] for t in trades)
    gross_wins = sum(t["pnl_r"] for t in wins) if wins else 0
    gross_losses = abs(sum(t["pnl_r"] for t in losses)) if losses else 0

    avg_win = gross_wins / n_wins if n_wins else 0
    avg_loss = gross_losses / n_losses if n_losses else 0

    stats = {
        "total_trades": total,
        "wins": n_wins,
        "losses": n_losses,
        "win_rate": round(n_wins / total * 100, 1) if total else 0,
        "expectancy_r": round(total_pnl_r / total, 4) if total else 0,
        "total_pnl_r": round(total_pnl_r, 3),
        "total_pnl_usd": round(total_pnl_usd, 2),
        "profit_factor": round(gross_wins / gross_losses, 2) if gross_losses > 0 else float("inf"),
        "avg_win_r": round(avg_win, 3),
        "avg_loss_r": round(avg_loss, 3),
        "avg_peak_r": round(sum(t["peak_r"] for t in trades) / total, 3) if total else 0,
        "avg_r_left": round(sum(t["r_left_on_table"] for t in trades) / total, 3) if total else 0,
        "best_r": round(max(t["pnl_r"] for t in trades), 3) if trades else 0,
        "worst_r": round(min(t["pnl_r"] for t in trades), 3) if trades else 0,
        "avg_duration_min": round(sum(t["duration_secs"] for t in trades) / total / 60, 1) if total else 0,
    }

    # ── Per-session breakdown ──
    by_session = {}
    for ses in set(t["session"] for t in trades):
        ses_trades = [t for t in trades if t["session"] == ses]
        ses_wins = [t for t in ses_trades if t["pnl_r"] > 0]
        ses_pnl = sum(t["pnl_r"] for t in ses_trades)
        by_session[ses] = {
            "trades": len(ses_trades),
            "wins": len(ses_wins),
            "wr": round(len(ses_wins) / len(ses_trades) * 100, 1) if ses_trades else 0,
            "pnl_r": round(ses_pnl, 3),
            "exp_r": round(ses_pnl / len(ses_trades), 4) if ses_trades else 0,
        }
    stats["by_session"] = by_session

    # ── Per-pair breakdown (top/bottom 10) ──
    by_pair_all = {}
    for sym in set(t["symbol"] for t in trades):
        sym_trades = [t for t in trades if t["symbol"] == sym]
        sym_pnl = sum(t["pnl_r"] for t in sym_trades)
        sym_wins = [t for t in sym_trades if t["pnl_r"] > 0]
        by_pair_all[sym] = {
            "trades": len(sym_trades),
            "wins": len(sym_wins),
            "wr": round(len(sym_wins) / len(sym_trades) * 100, 1) if sym_trades else 0,
            "pnl_r": round(sym_pnl, 3),
        }
    # Sort by pnl_r, keep top 10 and bottom 10
    sorted_pairs = sorted(by_pair_all.items(), key=lambda x: x[1]["pnl_r"], reverse=True)
    stats["by_pair"] = dict(sorted_pairs[:10] + sorted_pairs[-10:]) if len(sorted_pairs) > 20 else dict(sorted_pairs)

    # ── Trail analysis ──
    trailed = [t for t in trades if t["trail_was_active"]]
    if trailed:
        stats["trail"] = {
            "count": len(trailed),
            "avg_peak_r": round(sum(t["peak_r"] for t in trailed) / len(trailed), 3),
            "avg_exit_r": round(sum(t["exit_r"] for t in trailed) / len(trailed), 3),
            "avg_left_r": round(sum(t["r_left_on_table"] for t in trailed) / len(trailed), 3),
            "pnl_r": round(sum(t["pnl_r"] for t in trailed), 3),
        }

    # ── Backtest comparison (expected vs actual) ──
    from live.config import (
        BACKTEST_WR, BACKTEST_EXPECTANCY_R, BACKTEST_PF,
        BACKTEST_AVG_WIN_R, BACKTEST_AVG_LOSS_R,
    )
    stats["expected"] = {
        "wr": BACKTEST_WR,
        "exp_r": BACKTEST_EXPECTANCY_R,
        "pf": BACKTEST_PF,
        "avg_win_r": BACKTEST_AVG_WIN_R,
        "avg_loss_r": BACKTEST_AVG_LOSS_R,
    }

    # ── Equity curve (from heartbeats + trades) ──
    curve = []
    for t in trades:
        curve.append({
            "ts": t["exit_time"],
            "eq": t["equity_after"],
            "pnl_r": t["pnl_r"],
        })
    stats["equity_curve"] = curve

    return stats
