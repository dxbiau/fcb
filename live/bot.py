"""
live/bot.py — Main FCB live trading bot.

Lifecycle:
  1. Connect to Bybit, verify balance.
  2. Set leverage + margin mode on all pairs.
  3. Enter main loop:
     a. Determine current session (asia/london/ny).
     b. At session open (minute 0-4): capture first 5m candle for each pair.
     c. At candle 2 close (minute 10): check breakout for all pairs.
     d. Minutes 10-60: continuously re-scan untraded pairs every 5m candle
        close (breakouts can happen on C3, C4, ... C12, not only C2).
     e. Positions managed by exchange SL/TP — bot monitors and logs outcomes.
     f. Sleep until next session once breakout window closes.

The bot is designed to run 24/7.  State persists across restarts.
"""

import time, traceback, csv, math, os
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import ccxt

from live.config import (
    SESSIONS, PAIRS, ALL_PAIRS,
    RISK_PCT, SCALE_RISK_PCT, SPLIT_ENTRY, FEE_RATE, LEVERAGE,
    RISK_PCT_A, RISK_PCT_B, SCALE_RISK_PCT_A, SCALE_RISK_PCT_B,
    MAX_TRADES_SESSION, MAX_TRADES_DAY, MAX_CONCURRENT_POSITIONS, MAX_CONCURRENT_B,
    POLL_INTERVAL, TIMEFRAME, EQUITY_FLOOR, TRADE_LOG, TP_R,
    SCALE_OUT, SCALE_OUT_PCT,
    TRAIL_ENABLED, TRAIL_ACTIVATION_R, TRAIL_DISTANCE_R, EXCHANGE_TP_R,
    MICRO_FILTER_ENABLED, MIN_C2_BODY_RATIO, FC_COUNTER_5M,
    VOL_FILTER_ENABLED, MIN_VOL_RATIO_LONG, MIN_VOL_RATIO_SHORT,
    HYBRID_ENTRY, MAX_SLIP_R, SKIP_LOG,
    C3_EXIT, C3_REVERSAL_BODY_PCT, C3_MAX_R_TO_EXIT,
    BACKTEST_WR, BACKTEST_EXPECTANCY_R, BACKTEST_PF,
    BACKTEST_AVG_WIN_R, BACKTEST_AVG_LOSS_R,
    BACKTEST_TRADES_PER_DAY, BACKTEST_START_EQUITY,
    pairs_for_session as cfg_pairs_for_session,
    BREAKOUT_WINDOW_5M, API_DELAY_SECS,
    SPREAD_FILTER_ENABLED, MAX_SPREAD_PCT, MIN_TURNOVER_USDT,
    FUNDING_FILTER_ENABLED, FUNDING_EXTREME_RATE, FUNDING_EXTREME_NEG,
    MAX_C2_BODY_RATIO,
    C3_RETEST_REQUIRED,
)
from live.pair_scanner import scan_and_configure
from live import exchange as exch
from live import logger as log
from live.state import BotState
from live.profit_guardian import ProfitGuardian
from live.strategy import (
    FirstCandle, TradeSignal, ScaleSignal,
    capture_first_candle, check_breakout, compute_signal, compute_scale_signal,
)
from live import trades as trade_log
from live import trade_logger as tlog
from live.guardian import GuardianAgent
from live.session_reviewer import review_session as _journal_review
from live.session_reviewer import get_session_exits as _get_session_exits
from live.growth_tracker import (
    record_session_end as _growth_session_end,
    log_dashboard as _growth_dashboard,
    check_pace_alert as _growth_pace_alert,
    init_tracker as _growth_init,
)
from live.edge_score import (
    score_entry as _edge_score,
    should_block as _edge_block,
    format_score as _edge_fmt,
)
from live.pair_intel import get_congestion_zones_for_trade, get_sr_context_for_trade, PairProfile
from live.dynamic_engine import DynamicEngine


# ═══════════════════════════════════════════════════════════
#  ACTIVITY STATUS (read by dashboard)
# ═══════════════════════════════════════════════════════════

ACTIVITY_FILE = os.path.join("live", "logs", "bot_activity.json")

def _write_activity(phase: str, detail: str = "", session: str = "",
                    pairs: int = 0, positions: int = 0, next_session: str = "",
                    next_session_time: str = ""):
    """Write current bot activity to a JSON file for the dashboard.
    
    Called on every phase change so the dashboard can show exactly
    what the bot is doing right now.
    """
    try:
        data = {
            "phase": phase,
            "detail": detail,
            "session": session,
            "pairs": pairs,
            "positions": positions,
            "next_session": next_session,
            "next_session_time": next_session_time,
            "ts": datetime.now(timezone.utc).isoformat(),
            "uptime_since": _BOT_START_TIME,
        }
        os.makedirs(os.path.dirname(ACTIVITY_FILE), exist_ok=True)
        with open(ACTIVITY_FILE, "w") as f:
            import json as _json
            _json.dump(data, f)
    except Exception:
        pass  # Non-critical

_BOT_START_TIME = datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════
#  SESSION HELPERS
# ═══════════════════════════════════════════════════════════

def current_session() -> Optional[str]:
    """Return the currently active session name, or None if between sessions."""
    hour = datetime.now(timezone.utc).hour
    for name, (start, end) in SESSIONS.items():
        if end == 24:
            if hour >= start:
                return name
        else:
            if start <= hour < end:
                return name
    return None


def next_session_start() -> Tuple[str, datetime]:
    """Return (session_name, datetime) of the next session opening."""
    now = datetime.now(timezone.utc)
    hour = now.hour

    # Find next session that hasn't started yet
    ordered = sorted(SESSIONS.items(), key=lambda x: x[1][0])
    for name, (start, _) in ordered:
        if start > hour:
            # Today
            target = now.replace(hour=start, minute=0, second=0, microsecond=0)
            return name, target

    # Wrap to first session tomorrow
    name, (start, _) = ordered[0]
    target = (now + timedelta(days=1)).replace(hour=start, minute=0, second=0, microsecond=0)
    return name, target


def session_minute(session_name: str) -> int:
    """Minutes elapsed since current session started."""
    now = datetime.now(timezone.utc)
    start_h = SESSIONS[session_name][0]
    session_start = now.replace(hour=start_h, minute=0, second=0, microsecond=0)
    if now < session_start:
        session_start -= timedelta(days=1)
    return int((now - session_start).total_seconds() / 60)


def is_first_candle_time(session_name: str) -> bool:
    """True if we're in the first 5m candle window (minutes 0-4)."""
    m = session_minute(session_name)
    return 0 <= m < 5


def is_second_candle_closed(session_name: str) -> bool:
    """True if the second 5m candle has closed (minute >= 10)."""
    m = session_minute(session_name)
    return m >= 10


def wait_for_candle_close():
    """Wait until the current 5m candle closes (next :00 or :05 boundary)."""
    now = datetime.now(timezone.utc)
    minute = now.minute
    # Next 5-minute boundary
    next_boundary = ((minute // 5) + 1) * 5
    if next_boundary >= 60:
        target = (now + timedelta(hours=1)).replace(minute=0, second=2, microsecond=0)
    else:
        target = now.replace(minute=next_boundary, second=2, microsecond=0)

    wait = (target - now).total_seconds()
    if wait > 0:
        log.info(f"Waiting {wait:.0f}s for candle close at {target.strftime('%H:%M:%S')} UTC")
        time.sleep(wait)


# ═══════════════════════════════════════════════════════════
#  STARTUP REPORT — Persistent trade history from trades.csv
# ═══════════════════════════════════════════════════════════

def _startup_report(equity: float):
    """Parse trades.csv and print a comprehensive status dashboard.

    This runs on every startup so the operator always knows where
    things stand — regardless of state.json resets.
    """
    if not os.path.exists(TRADE_LOG):
        log.info("No trade history yet (trades.csv not found)")
        return

    # ── Parse trades.csv ──
    entries = {}   # order_id → entry row
    trades = []    # completed round-trips: {symbol, session, direction, entry_price, exit_price, result, r_multiple, date}
    recent = []    # last N completed trades for display

    try:
        with open(TRADE_LOG, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                action = row.get("action", "")
                oid = row.get("order_id", "")
                notes = row.get("notes", "")

                if action == "ENTRY" and "SCALE-IN" not in notes:
                    entries[oid] = row
                elif action == "EXIT":
                    entry = entries.get(oid)
                    if not entry:
                        continue

                    entry_price = float(entry["price"])
                    exit_price = float(row["price"])
                    direction = entry["direction"]
                    sl = float(entry.get("sl") or 0)
                    risk_per_unit = abs(entry_price - sl) if sl else 0

                    # Determine result
                    if "WIN" in notes:
                        result = "WIN"
                    elif "LOSS" in notes:
                        result = "LOSS"
                    else:
                        # Infer from P&L
                        if direction == "long":
                            result = "WIN" if exit_price > entry_price else "LOSS"
                        else:
                            result = "WIN" if exit_price < entry_price else "LOSS"

                    # Calculate R-multiple
                    if risk_per_unit > 0:
                        if direction == "long":
                            r_mult = (exit_price - entry_price) / risk_per_unit
                        else:
                            r_mult = (entry_price - exit_price) / risk_per_unit
                    else:
                        r_mult = 1.5 if result == "WIN" else -1.0

                    # Estimate USD P&L from equity_after
                    equity_after = float(row.get("equity_after") or 0)
                    equity_before = float(entry.get("equity_before") or 0)

                    tf = "5m"
                    tf_type = "5M"

                    trades.append({
                        "symbol": entry["symbol"].replace("/USDT:USDT", ""),
                        "symbol_full": entry["symbol"],
                        "session": entry["session"],
                        "direction": direction,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "result": result,
                        "r_mult": round(r_mult, 3),
                        "date": row["timestamp_utc"][:10],
                        "time": row["timestamp_utc"][11:16],
                        "equity_after": equity_after,
                        "pnl_usd": round(equity_after - equity_before, 2) if equity_after and equity_before else 0,
                        "tf": tf,
                        "tf_type": tf_type,
                    })
    except Exception as e:
        log.warning(f"Could not parse trade history: {e}")
        return

    if not trades:
        log.info("No completed trades in history yet")
        return

    # ── Compute stats ──
    total = len(trades)
    wins = sum(1 for t in trades if t["result"] == "WIN")
    losses = total - wins
    wr = (wins / total * 100) if total else 0
    r_values = [t["r_mult"] for t in trades]
    total_r = sum(r_values)
    avg_r = total_r / total if total else 0
    avg_win_r = sum(r for r in r_values if r > 0) / max(wins, 1)
    avg_loss_r = abs(sum(r for r in r_values if r <= 0) / max(losses, 1))
    pf = (sum(r for r in r_values if r > 0) / abs(sum(r for r in r_values if r <= 0))) if losses > 0 and sum(r for r in r_values if r <= 0) != 0 else float("inf")

    # Per-pair stats
    pair_stats = defaultdict(lambda: {"w": 0, "l": 0, "r": 0.0})
    for t in trades:
        ps = pair_stats[t["symbol"]]
        if t["result"] == "WIN":
            ps["w"] += 1
        else:
            ps["l"] += 1
        ps["r"] += t["r_mult"]

    # Per-session stats
    sess_stats = defaultdict(lambda: {"w": 0, "l": 0, "r": 0.0})
    for t in trades:
        ss = sess_stats[t["session"]]
        if t["result"] == "WIN":
            ss["w"] += 1
        else:
            ss["l"] += 1
        ss["r"] += t["r_mult"]

    # Streaks
    current_streak = 0
    streak_type = ""
    max_win_streak = 0
    max_loss_streak = 0
    cur_w = 0
    cur_l = 0
    for t in trades:
        if t["result"] == "WIN":
            cur_w += 1
            cur_l = 0
            max_win_streak = max(max_win_streak, cur_w)
        else:
            cur_l += 1
            cur_w = 0
            max_loss_streak = max(max_loss_streak, cur_l)
    # Current streak from last trades
    for t in reversed(trades):
        if not streak_type:
            streak_type = t["result"]
            current_streak = 1
        elif t["result"] == streak_type:
            current_streak += 1
        else:
            break

    # Days trading
    trade_dates = sorted(set(t["date"] for t in trades))
    days_active = len(trade_dates)
    trades_per_day = total / max(days_active, 1)

    # Starting equity (from first trade entry)
    start_equity = BACKTEST_START_EQUITY  # fallback = config value
    first_eq = trades[0].get("pnl_usd", 0)
    if trades[0].get("equity_after"):
        start_equity = trades[0]["equity_after"] - trades[0].get("pnl_usd", 0)

    # x1000 projection
    risk_pct = RISK_PCT_A
    if wr > 0 and avg_win_r > 0 and avg_loss_r > 0:
        g_win = (1 + risk_pct * avg_win_r) ** (wr / 100)
        g_loss = (1 - risk_pct * avg_loss_r) ** (1 - wr / 100)
        g = g_win * g_loss
        if g > 1.0:
            trades_to_x2 = int(math.ceil(math.log(2) / math.log(g)))
            trades_to_x5 = int(math.ceil(math.log(5) / math.log(g)))
            trades_to_x10 = int(math.ceil(math.log(10) / math.log(g)))
            trades_to_x1000 = int(math.ceil(math.log(1000) / math.log(g)))
            days_to_x10 = trades_to_x10 / max(trades_per_day, 0.1)
            days_to_x1000 = trades_to_x1000 / max(trades_per_day, 0.1)
        else:
            trades_to_x2 = trades_to_x5 = trades_to_x10 = trades_to_x1000 = None
            days_to_x10 = days_to_x1000 = None
    else:
        trades_to_x2 = trades_to_x5 = trades_to_x10 = trades_to_x1000 = None
        days_to_x10 = days_to_x1000 = None

    # Net P&L
    net_pnl = equity - start_equity

    # ── Print Report ──
    log.info("=" * 70)
    log.info("  TRADE HISTORY REPORT")
    log.info("=" * 70)
    log.info(f"  Period:   {trade_dates[0]} → {trade_dates[-1]} ({days_active} day{'s' if days_active != 1 else ''})")
    log.info(f"  Equity:   ${start_equity:.2f} → ${equity:.2f} (net: {'+' if net_pnl >= 0 else ''}{net_pnl:.2f})")
    log.info("-" * 70)
    log.info(f"  Trades:   {total} ({wins}W / {losses}L)")
    log.info(f"  Win Rate: {wr:.1f}%")
    log.info(f"  Expect:   {avg_r:+.3f}R per trade")
    log.info(f"  PF:       {pf:.2f}")
    log.info(f"  Total R:  {total_r:+.3f}R")
    log.info(f"  Avg Win:  +{avg_win_r:.3f}R | Avg Loss: -{avg_loss_r:.3f}R")
    log.info(f"  Streaks:  best={max_win_streak}W worst={max_loss_streak}L | current={current_streak}{streak_type[0] if streak_type else '?'}")
    log.info(f"  Pace:     {trades_per_day:.1f} trades/day")

    # Session breakdown
    log.info("-" * 70)
    log.info("  SESSION BREAKDOWN")
    for sname in ["asia", "london", "ny"]:
        ss = sess_stats.get(sname)
        if ss:
            st = ss["w"] + ss["l"]
            swr = ss["w"] / st * 100 if st else 0
            savg = ss["r"] / st if st else 0
            log.info(f"    {sname:<8} {st:>3} trades  {ss['w']:>2}W/{ss['l']:>2}L  "
                     f"WR={swr:>5.1f}%  E(R)={savg:+.3f}")

    # Micro-filter stats (from skipped_trades.csv)
    if os.path.exists(SKIP_LOG):
        try:
            skip_counts = defaultdict(int)
            with open(SKIP_LOG, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    skip_counts[row.get("reason", "unknown")] += 1
            total_skipped = sum(skip_counts.values())
            if total_skipped > 0:
                log.info("-" * 70)
                log.info(f"  MICRO-FILTER STATS  (taken={total} | filtered={total_skipped} | "
                         f"selectivity={total/(total+total_skipped)*100:.0f}%)")
                for reason, cnt in sorted(skip_counts.items(), key=lambda x: -x[1]):
                    log.info(f"    {reason:<30} {cnt:>4} blocked")
        except Exception:
            pass

    # Per-pair breakdown (sorted by total R)
    log.info("-" * 70)
    log.info("  PAIR PERFORMANCE")
    sorted_pairs = sorted(pair_stats.items(), key=lambda x: x[1]["r"], reverse=True)
    for pname, ps in sorted_pairs:
        pt = ps["w"] + ps["l"]
        pwr = ps["w"] / pt * 100 if pt else 0
        pavg = ps["r"] / pt if pt else 0
        marker = "★" if ps["r"] > 0 else "✗"
        log.info(f"    {marker} {pname:<16} {pt:>2} trades  {ps['w']:>2}W/{ps['l']:>2}L  "
                 f"WR={pwr:>5.1f}%  totalR={ps['r']:+.3f}  E(R)={pavg:+.3f}")

    # Recent trades (with timeframe tag)
    log.info("-" * 70)
    log.info("  RECENT TRADES")
    for t in trades[-10:]:
        icon = "✓" if t["result"] == "WIN" else "✗"
        tf_tag = f"[{t.get('tf', '5m')}]"
        log.info(f"    {icon} {t['date']} {t['time']}  {t['symbol']:<16} "
                 f"{t['direction']:<5} {t['session']:<7} {tf_tag:<5} "
                 f"R={t['r_mult']:+.3f}  ${t['equity_after']:.2f}")

    # Projections
    log.info("-" * 70)
    log.info("  PROJECTIONS (at current stats, {:.0f}% WR, {:.1f} trades/day, {}% risk)".format(
        wr, trades_per_day, int(risk_pct * 100)))
    if trades_to_x10 is not None:
        log.info(f"    x2:    {trades_to_x2:>6} trades  (~{trades_to_x2/max(trades_per_day,0.1):.0f} days)")
        log.info(f"    x5:    {trades_to_x5:>6} trades  (~{trades_to_x5/max(trades_per_day,0.1):.0f} days)")
        log.info(f"    x10:   {trades_to_x10:>6} trades  (~{days_to_x10:.0f} days)")
        log.info(f"    x1000: {trades_to_x1000:>6} trades  (~{days_to_x1000:.0f} days)")
    else:
        log.info("    Insufficient data or negative expectancy — no projection available")
    log.info("=" * 70)

    # ── Performance Tracker: Expected vs Actual ──
    # ANSI colours
    G  = "\033[38;2;34;139;34m"   # forest green
    BG = "\033[1;38;2;34;139;34m" # bold forest green
    UP = "\033[32m"               # bright green (ahead)
    DN = "\033[31m"               # red (behind)
    DM = "\033[2m"                # dim
    RS = "\033[0m"                # reset

    # Backtest baseline growth rate per trade
    bt_g_win  = (1 + risk_pct * BACKTEST_AVG_WIN_R) ** (BACKTEST_WR / 100)
    bt_g_loss = (1 - risk_pct * BACKTEST_AVG_LOSS_R) ** (1 - BACKTEST_WR / 100)
    bt_g = bt_g_win * bt_g_loss
    bt_trades_x10 = int(math.ceil(math.log(10) / math.log(bt_g))) if bt_g > 1 else 9999
    bt_days_x10   = bt_trades_x10 / max(BACKTEST_TRADES_PER_DAY, 0.1)
    bt_trades_x1000 = int(math.ceil(math.log(1000) / math.log(bt_g))) if bt_g > 1 else 9999
    bt_days_x1000   = bt_trades_x1000 / max(BACKTEST_TRADES_PER_DAY, 0.1)

    # Live growth rate
    live_g = 1.0
    if wr > 0 and avg_win_r > 0 and avg_loss_r > 0:
        _lg_win  = (1 + risk_pct * avg_win_r) ** (wr / 100)
        _lg_loss = (1 - risk_pct * avg_loss_r) ** (1 - wr / 100)
        live_g = _lg_win * _lg_loss

    # Expected equity after N trades at backtest rate
    bt_expected_equity = start_equity * (bt_g ** total)
    equity_delta = equity - bt_expected_equity

    # Days elapsed since first trade
    from datetime import date as _date
    first_date = _date.fromisoformat(trade_dates[0])
    today = _date.fromisoformat(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    days_elapsed = max((today - first_date).days, 1)

    # Progress percentages
    pct_trades = (total / bt_trades_x10 * 100) if bt_trades_x10 else 0
    pct_days   = (days_elapsed / bt_days_x10 * 100) if bt_days_x10 else 0

    # Delta helpers
    def _arrow(live_val, bt_val, fmt=".3f", suffix="", pp=False):
        d = live_val - bt_val
        if pp:
            return f"{UP}▲ +{d:.1f}pp{RS}" if d >= 0 else f"{DN}▼ {d:.1f}pp{RS}"
        return f"{UP}▲ {d:+{fmt}}{suffix}{RS}" if d >= 0 else f"{DN}▼ {d:+{fmt}}{suffix}{RS}"

    def _pct_arrow(live_val, bt_val):
        if bt_val == 0:
            return f"{UP}▲{RS}"
        d = (live_val - bt_val) / bt_val * 100
        return f"{UP}▲ {d:+.0f}%{RS}" if d >= 0 else f"{DN}▼ {d:+.0f}%{RS}"

    # Overall status assessment (4 dimensions)
    score = sum([
        wr > BACKTEST_WR,
        avg_r > BACKTEST_EXPECTANCY_R,
        pf > BACKTEST_PF,
        live_g > bt_g if live_g > 1 else False,
    ])
    if score >= 3:
        status_icon  = f"{UP}🟢{RS}"
        status_label = f"{UP}OUTPERFORMING{RS}"
        status_note  = f"live exceeds backtest on {score}/4 metrics"
    elif score >= 2:
        status_icon  = "\033[33m🟡\033[0m"
        status_label = "\033[33mON TRACK\033[0m"
        status_note  = f"broadly matching backtest expectations"
    else:
        status_icon  = f"{DN}🔴{RS}"
        status_label = f"{DN}BEHIND{RS}"
        status_note  = f"below backtest on {4-score}/4 metrics — review needed"

    # Effective pace comparison
    live_trades_x10 = trades_to_x10 if trades_to_x10 else None
    live_days_x10   = days_to_x10 if days_to_x10 else None

    # Speed ratio
    if live_g > 1 and bt_g > 1:
        speed_ratio = math.log(live_g) / math.log(bt_g)
        speed_str = f"{speed_ratio:.1f}x faster" if speed_ratio >= 1 else f"{1/speed_ratio:.1f}x slower"
        speed_clr = UP if speed_ratio >= 1 else DN
    else:
        speed_str = "N/A"
        speed_clr = DM

    # Print the tracker
    log.info(f"{BG}{'=' * 70}{RS}")
    log.info(f"{BG}  FCB PERFORMANCE TRACKER -- Expected vs Actual{RS}")
    log.info(f"{BG}  5m FCB | Dynamic Scanner | Micro-filters: ON{RS}")
    log.info(f"{BG}{'=' * 70}{RS}")
    log.info(f"{G}{'':>18}{'BACKTEST':>12}    {'LIVE':>12}    DELTA{RS}")
    log.info(f"{G}  Win Rate:{RS}     {BACKTEST_WR:>10.1f}%    {wr:>10.1f}%     {_arrow(wr, BACKTEST_WR, pp=True)}")
    log.info(f"{G}  Expectancy:{RS}   {BACKTEST_EXPECTANCY_R:>+10.3f}R    {avg_r:>+10.3f}R     {_arrow(avg_r, BACKTEST_EXPECTANCY_R, suffix='R')}")
    log.info(f"{G}  PF:{RS}           {BACKTEST_PF:>10.2f}     {pf:>10.2f}      {_pct_arrow(pf, BACKTEST_PF)}")
    log.info(f"{G}  Growth/trade:{RS} {bt_g:>10.5f}     {live_g:>10.5f}      {speed_clr}{speed_str}{RS}")
    log.info(f"{G}{'-' * 70}{RS}")
    log.info(f"{G}  EQUITY CURVE{RS}")
    log.info(f"    Bot start:        ${start_equity:>10,.2f}")
    log.info(f"    Expected now:     ${bt_expected_equity:>10,.2f}  {DM}(after {total} trades at backtest rate){RS}")
    log.info(f"    Actual now:       ${equity:>10,.2f}  {_arrow(equity, bt_expected_equity, fmt='.2f', suffix='')}")
    pnl_pct = (equity - start_equity) / start_equity * 100
    bt_pnl_pct = (bt_expected_equity - start_equity) / start_equity * 100
    log.info(f"    Return:           {pnl_pct:>+9.2f}%      {DM}(expected: {bt_pnl_pct:+.2f}%){RS}")
    log.info(f"{G}{'-' * 70}{RS}")
    log.info(f"{G}  ROAD TO x10  (${start_equity:.0f} -> ${start_equity*10:,.0f})  |  x1000  (${start_equity:.0f} -> ${start_equity*1000:,.0f}){RS}")
    log.info(f"    Trades done:      {total:>6} / {bt_trades_x10} (x10)   {total:>6} / {bt_trades_x1000} (x1000)")
    pct_x1000 = (total / bt_trades_x1000 * 100) if bt_trades_x1000 else 0
    log.info(f"    Progress:         {pct_trades:>5.1f}% to x10          {pct_x1000:>5.1f}% to x1000")
    log.info(f"    Days elapsed:     {days_elapsed:>6} / {bt_days_x10:.0f} (x10)   {days_elapsed:>6} / {bt_days_x1000:.0f} (x1000)")
    if live_trades_x10 and live_days_x10:
        live_trades_x1000_val = trades_to_x1000 if trades_to_x1000 else None
        live_days_x1000_val = days_to_x1000 if days_to_x1000 else None
        pace_arrow = UP if live_trades_x10 < bt_trades_x10 else DN
        log.info(f"    At LIVE pace:     {live_trades_x10:>6} trades  (~{live_days_x10:.0f} days) to x10")
        if live_trades_x1000_val and live_days_x1000_val:
            log.info(f"                      {live_trades_x1000_val:>6} trades  (~{live_days_x1000_val:.0f} days) to x1000")
        log.info(f"    At BACKTEST pace: {bt_trades_x10:>6} trades  (~{bt_days_x10:.0f} days) to x10")
        log.info(f"                      {bt_trades_x1000:>6} trades  (~{bt_days_x1000:.0f} days) to x1000")
        saved = bt_days_x10 - live_days_x10
        if saved >= 0:
            log.info(f"    Time saved:       {pace_arrow}{saved:>+.0f} days faster to x10{RS}")
        else:
            log.info(f"    Time added:       {pace_arrow}{saved:>+.0f} days slower to x10{RS}")
    else:
        log.info(f"    At BACKTEST pace: {bt_trades_x10:>6} trades  (~{bt_days_x10:.0f} days) to x10")
        log.info(f"                      {bt_trades_x1000:>6} trades  (~{bt_days_x1000:.0f} days) to x1000")
        log.info(f"    Live projection:  insufficient data")
    log.info(f"{G}{'-' * 70}{RS}")
    log.info(f"    Status:  {status_icon}  {status_label}  — {status_note}")
    log.info(f"{BG}{'═' * 70}{RS}")


# ═══════════════════════════════════════════════════════════
#  CORE BOT
# ═══════════════════════════════════════════════════════════

class FCBBot:
    def __init__(self):
        self.ex = None
        self.state = BotState()
        self.first_candles: Dict[str, FirstCandle] = {}  # keyed by symbol
        self.market_info: Dict[str, Dict] = {}
        self.guardian: Optional[GuardianAgent] = None
        self.profit_guardian: Optional[ProfitGuardian] = None
        self._running = True
        self._5m_entered: Dict[str, set] = {}  # session → set of pairs already entered
        self._5m_initial_done: Dict[str, bool] = {}  # session → initial C2 scan done
        self._scanned_sessions: Dict[str, list] = {}  # session → scanned pair list
        self.pair_profiles: Dict[str, PairProfile] = {}  # symbol → intel profile from scanner
        self._pending_retests: Dict[str, dict] = {}  # pair → C2 breakout info awaiting C3 retest
        self._retest_attempted: set = set()  # pairs that already had breakout+retest attempt this session

        # Dynamic Hybrid Engine — real-time adaptive intelligence
        try:
            from live.config import DYN_ENABLED, DYN_START_EQUITY
            if DYN_ENABLED:
                self.dynamic = DynamicEngine(start_equity=DYN_START_EQUITY)
            else:
                self.dynamic = None
        except (ImportError, AttributeError):
            self.dynamic = None

    def connect(self):
        """Connect to exchange and prepare all pairs."""
        log.info("=" * 70)
        log.info("  FCB LIVE BOT — Starting")
        log.info("=" * 70)

        _write_activity("STARTING", "Connecting to Bybit...")
        self.ex = exch.create_exchange()

        # Verify balance
        equity = exch.get_equity(self.ex)
        self.state.update_equity(equity)
        log.info(f"Account equity: ${equity:.2f} USDT")

        if equity < 10:
            log.critical(f"Equity too low (${equity:.2f}). Fund the account first.")
            self._running = False
            return

        # Set leverage + margin mode for all pairs
        # Skip pairs whose max leverage < required (instead of clamping)
        log.info(f"Configuring {len(ALL_PAIRS)} pairs (leverage={LEVERAGE}x, isolated margin)...")
        excluded_pairs = []
        for pair in ALL_PAIRS:
            try:
                exch.set_leverage(self.ex, pair, LEVERAGE)
                exch.set_margin_mode(self.ex, pair, "isolated")
                info = exch.get_market_info(self.ex, pair)
                self.market_info[pair] = info
                log.debug(f"  {pair}: OK | min_qty={info['min_qty']} "
                          f"price_prec={info['price_precision']} "
                          f"amt_prec={info['amount_precision']}")
            except ValueError as e:
                # Leverage too low — exclude this pair entirely
                excluded_pairs.append(pair)
                log.warning(f"  {pair}: EXCLUDED — {e}")
            except Exception as e:
                log.error(f"  {pair}: FAILED to configure — {e}")

        if excluded_pairs:
            log.info(f"Excluded {len(excluded_pairs)} pairs (max leverage < {LEVERAGE}x): "
                     f"{', '.join(excluded_pairs[:10])}{'...' if len(excluded_pairs) > 10 else ''}")

        log.info(f"Ready. {len(self.market_info)} pairs configured.")
        _write_activity("READY", f"{len(self.market_info)} pairs configured",
                        positions=len(self.state.pending_entries))

        # Initialize Guardian Agent (pre-entry checks, health, anomalies)
        self.guardian = GuardianAgent(self.ex, self.state)
        log.info("Guardian Agent initialised")

        # Start Profit Guardian v2 (real-time position intelligence daemon)
        self.profit_guardian = ProfitGuardian(self.state)
        self.profit_guardian.start()
        log.info("Profit Guardian v2 daemon started")

        # Log pair class summary
        log.info(self.state.pair_class_summary())

        # Cancel any stale limit orders from a previous session/crash
        self._cleanup_stale_orders()

        # Print trade history report from persistent trades.csv
        equity = exch.get_equity(self.ex)
        _startup_report(equity)

        # x10 Growth Tracker dashboard
        try:
            if not os.path.exists("live/growth_state.json"):
                _growth_init(start_equity=equity)
            _growth_dashboard(equity)
            alert = _growth_pace_alert(equity)
            if alert:
                log.warning(f"  GROWTH ALERT: {alert}")
        except Exception as e:
            log.warning(f"Growth tracker error: {e}")

        # Sync state totals from trades.csv so state.json
        # reflects reality even after a restart/state reset
        self._sync_state_from_history()

    def run(self):
        """Main bot loop — runs forever."""
        self.connect()

        while self._running:
            try:
                self._tick()
            except KeyboardInterrupt:
                log.info("Shutdown requested (Ctrl+C)")
                self._running = False
            except Exception as e:
                log.error(f"Tick error: {e}")
                log.debug(traceback.format_exc())
                time.sleep(30)

        # Stop Profit Guardian daemon
        if self.profit_guardian:
            self.profit_guardian.stop()
        log.info("Bot stopped.")

    def _cleanup_stale_orders(self):
        """Cancel all open limit orders on startup.

        If the bot crashed mid-session, scale-in limit orders may still be
        sitting on Bybit.  We cancel them all to start clean.
        Also reconciles pending_entries scale statuses.
        """
        cancelled = 0
        for pair in ALL_PAIRS:
            try:
                open_orders = exch.get_open_orders(self.ex, pair)
                for order in open_orders:
                    oid = order.get("id", "")
                    otype = order.get("type", "").lower()
                    if otype == "limit":
                        exch.cancel_order(self.ex, oid, pair)
                        cancelled += 1
            except Exception as e:
                log.debug(f"  cleanup {pair}: {e}")

        # Mark any pending scale-in entries as cancelled
        for entry in self.state.pending_entries:
            if entry.get("scale_status") == "pending":
                entry["scale_status"] = "cancelled"

        if cancelled:
            log.info(f"Startup cleanup: cancelled {cancelled} stale limit order(s)")
            self.state._save()
        else:
            log.info("Startup cleanup: no stale orders found")

    def _sync_state_from_history(self):
        """Restore state totals from trades.csv so they survive restarts."""
        if not os.path.exists(TRADE_LOG):
            return
        try:
            wins = 0
            losses = 0
            total_r = 0.0
            entries = {}
            with open(TRADE_LOG, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    action = row.get("action", "")
                    oid = row.get("order_id", "")
                    notes = row.get("notes", "")
                    if action == "ENTRY" and "SCALE-IN" not in notes:
                        entries[oid] = row
                    elif action == "EXIT":
                        entry = entries.get(oid)
                        if not entry:
                            continue
                        entry_price = float(entry["price"])
                        exit_price = float(row["price"])
                        direction = entry["direction"]
                        sl = float(entry.get("sl") or 0)
                        risk_per_unit = abs(entry_price - sl) if sl else 0
                        if "WIN" in notes:
                            wins += 1
                        elif "LOSS" in notes:
                            losses += 1
                        else:
                            if direction == "long":
                                wins += 1 if exit_price > entry_price else 0
                                losses += 0 if exit_price > entry_price else 1
                            else:
                                wins += 1 if exit_price < entry_price else 0
                                losses += 0 if exit_price < entry_price else 1
                        if risk_per_unit > 0:
                            if direction == "long":
                                total_r += (exit_price - entry_price) / risk_per_unit
                            else:
                                total_r += (entry_price - exit_price) / risk_per_unit

            self.state.total_wins = wins
            self.state.total_losses = losses
            self.state.total_trades = wins + losses
            self.state.total_pnl_r = round(total_r, 3)
            self.state._save()
            log.info(f"State synced from history: {wins + losses} trades, "
                     f"{wins}W/{losses}L, {total_r:+.3f}R")
        except Exception as e:
            log.warning(f"Could not sync state from history: {e}")

    def _tick(self):
        """One iteration of the main loop."""
        self.state.check_new_day()

        # Reset 5m continuous scan + scanner tracking per session
        if self.state.date != getattr(self, '_5m_last_date', None):
            self._5m_entered.clear()
            self._5m_initial_done.clear()
            self._scanned_sessions.clear()
            self._pending_retests.clear()
            self._retest_attempted.clear()
            self._5m_last_date = self.state.date

        # Check equity floor
        if self.state.equity_floor_hit:
            log.critical("Equity floor hit — bot is stopped. Fund account to resume.")
            self._running = False
            return

        sess = current_session()

        if sess is None:
            # No active session — this shouldn't happen with full 24h coverage
            name, when = next_session_start()
            wait = (when - datetime.now(timezone.utc)).total_seconds()
            log.info(f"No active session. Next: {name} in {wait/60:.0f} min")
            _write_activity("IDLE", f"No active session",
                            next_session=name,
                            next_session_time=when.isoformat(),
                            positions=len(self.state.pending_entries))
            time.sleep(min(wait, 300))
            return

        # Dynamic pair scanning — scan before each session
        if sess not in self._scanned_sessions:
            log.info(f"━━━ SCANNING PAIRS for {sess.upper()} session ━━━")
            try:
                scanned_syms, scanned_pairs, scanned_profiles = scan_and_configure(self.ex, self.state, session=sess)
                self._scanned_sessions[sess] = scanned_syms
                # Store intel profiles for entry-time context awareness
                self.pair_profiles.update(scanned_profiles)
                # Update market_info for newly discovered pairs
                for sym, cls in scanned_pairs:
                    if sym not in self.market_info:
                        try:
                            info = exch.get_market_info(self.ex, sym)
                            self.market_info[sym] = info
                        except Exception as e:
                            log.warning(f"  {sym}: market info failed — {e}")
                        time.sleep(API_DELAY_SECS)
            except Exception as e:
                log.error(f"Scanner failed: {e} — falling back to static pairs")
                self._scanned_sessions[sess] = cfg_pairs_for_session(sess)

        session_pairs = self._scanned_sessions.get(sess, [])
        if not session_pairs:
            log.warning(f"No pairs for {sess} — sleeping")
            time.sleep(60)
            return

        minute = session_minute(sess)

        # ── Phase 1: Capture first candles (minute 5 — after first candle closes) ──
        if 5 <= minute < 10:
            _write_activity("CAPTURING", f"Reading first candles",
                            session=sess, pairs=len(session_pairs),
                            positions=len(self.state.pending_entries))
            # Check if positions from previous sessions have closed
            self._resolve_positions()
            # Guardian health check on wake-up
            if self.guardian:
                self.guardian.full_health_check()
            self._capture_first_candles(sess, session_pairs)
            # Wait for candle 2 to close
            wait_for_candle_close()
            return

        # ── Phase 2: Continuous breakout scanning (5m only) ──
        outer_window = BREAKOUT_WINDOW_5M

        if 10 <= minute < outer_window:
            # Initialize tracking for this session
            if sess not in self._5m_entered:
                self._5m_entered[sess] = set()

            # ── 5m scanning (only within 5m window) ──
            if minute < BREAKOUT_WINDOW_5M:
                if sess not in self._5m_initial_done:
                    # First scan at minute 10 (C2 close)
                    _write_activity("SCANNING", f"Checking 5m breakouts (C2)",
                                    session=sess, pairs=len(session_pairs),
                                    positions=len(self.state.pending_entries))
                    self._check_breakouts(sess, session_pairs)
                    self._5m_initial_done[sess] = True

                    # Print 5m session report
                    self._print_session_report(sess)
                else:
                    # Subsequent scans — check remaining pairs that haven't broken out
                    remaining = [p for p in session_pairs
                                 if p not in self._5m_entered.get(sess, set())
                                 and self.first_candles.get(p) is not None
                                 and self.first_candles[p].valid
                                 and self.state.can_trade(p, sess, MAX_TRADES_SESSION, MAX_TRADES_DAY)]

                    if remaining:
                        _write_activity("SCANNING", f"Re-scanning {len(remaining)} pairs (C{minute//5+1})",
                                        session=sess, pairs=len(remaining),
                                        positions=len(self.state.pending_entries))
                        log.info(f"━━━ {sess.upper()} — Continuous scan: {len(remaining)} pairs "
                                 f"waiting for breakout (minute {minute}) ━━━")
                        self._check_breakouts(sess, remaining)
                    else:
                        log.debug(f"All pairs entered or filtered — no remaining 5m targets")

                    # Also resolve any positions
                    self._resolve_positions()

            # Wait for next 5m candle close, then loop back
            wait_for_candle_close()
            return

        # ── Phase 3: All windows closed — transition to monitoring ──
        if minute >= outer_window and sess in self._5m_initial_done:
            # Log 5m stats if not already logged
            if not getattr(self, '_5m_window_logged', {}).get(sess):
                entered_5m = len(self._5m_entered.get(sess, set()))
                total_5m = sum(1 for p in session_pairs
                               if self.first_candles.get(p) and self.first_candles[p].valid)
                log.info(f"━━━ {sess.upper()} — 5m window closed "
                         f"({BREAKOUT_WINDOW_5M}m). Entered {entered_5m}/{total_5m} valid pairs ━━━")

            # End-of-session flow
            if sess == "ny":
                self._print_daily_report()
                self._check_equity_floor()
                if self.guardian:
                    s = self.state.daily_summary()
                    self.guardian.session_debrief(
                        sess, s["entries_today"], s["wins"], s["losses"], []
                    )
            self._monitor_and_sleep(sess)
            return

        # ── Before first candle: wait for it ──
        if minute < 5:
            wait = (5 - minute) * 60
            log.info(f"Session {sess} started {minute}m ago. "
                     f"Waiting {wait/60:.1f}m for first candle to close...")
            _write_activity("WAITING", f"First candle closing in {wait/60:.0f}m",
                            session=sess, pairs=len(session_pairs),
                            positions=len(self.state.pending_entries))
            time.sleep(wait)
            return

        # ── Check if any open positions have closed, then sleep ──
        self._resolve_positions()
        self._sleep_until_next_session()

    def _capture_first_candles(self, session: str, pairs: List[str]):
        """Fetch the first 5m candle for each pair in this session."""
        # New session = new FCs → clear retest state from previous session
        self._pending_retests.clear()
        self._retest_attempted.clear()
        log.info(f"━━━ SESSION {session.upper()} — Capturing first candles for {len(pairs)} pairs ━━━")

        # Update equity
        try:
            equity = exch.get_equity(self.ex)
            self.state.update_equity(equity)
            log.info(f"Current equity: ${equity:.2f}")
            log.session_start(session, equity, len(pairs))

            # ── Initialize Dynamic Engine for this session ──
            if self.dynamic:
                try:
                    _btc_tk = self.ex.fetch_ticker("BTC/USDT:USDT")
                    _d_btc_chg = float(_btc_tk.get("percentage", 0) or 0)
                    _d_btc_price = float(_btc_tk.get("last", 0))
                except Exception:
                    _d_btc_chg, _d_btc_price = 0.0, 0.0
                self.dynamic.session_start(
                    session=session, equity=equity,
                    btc_chg=_d_btc_chg, btc_price=_d_btc_price,
                )
                log.info(f"  DYN ENGINE: {self.dynamic.status_line(equity)}")

            # Structured JSONL session open
            _cls_a = sum(1 for p in pairs if self.state.get_pair_class(p) == "A")
            _cls_b = len(pairs) - _cls_a
            tlog.log_session_open(
                session=session, equity=equity, pair_count=len(pairs),
                pending_positions=len(self.state.pending_entries),
                total_trades=self.state.total_trades,
                day_start_equity=self.state.day_start_equity,
                entries_today=self.state.entries_today,
                wins_today=self.state.wins_today,
                losses_today=self.state.losses_today,
                class_a_count=_cls_a, class_b_count=_cls_b,
            )
        except Exception as e:
            log.warning(f"Could not fetch equity: {e}")

        self.first_candles.clear()

        for pair in pairs:
            if pair not in self.market_info:
                log.warning(f"  {pair}: not configured, skipping")
                continue

            if not self.state.can_trade(pair, session, MAX_TRADES_SESSION, MAX_TRADES_DAY):
                log.debug(f"  {pair}: already traded this session/day, skipping")
                continue

            try:
                candles = exch.fetch_latest_candles(self.ex, pair, n=3)
                if not candles:
                    log.warning(f"  {pair}: no candles returned")
                    continue

                # The first candle of this session = most recent one that aligns
                # with session start hour. We take the latest closed candle.
                fc_data = candles[-1]  # Last closed candle = first candle of session
                fc = capture_first_candle(pair, session, fc_data)
                self.first_candles[pair] = fc

                status = "VALID" if fc.valid else f"SKIP (range={fc.range_pct*100:.2f}%)"
                log.info(f"  {pair}: H={fc.high:.6f} L={fc.low:.6f} "
                         f"range={fc.range_pct*100:.3f}% [{status}]")

            except Exception as e:
                log.error(f"  {pair}: error fetching candle — {e}")

            time.sleep(API_DELAY_SECS)  # rate-limit pacing

        valid = sum(1 for fc in self.first_candles.values() if fc.valid)
        log.info(f"Captured {len(self.first_candles)} candles, {valid} valid (range >= 0.3%)")

    def _check_breakouts(self, session: str, pairs: List[str]):
        """Check candle 2 closes for breakouts and place trades."""
        log.info(f"━━━ SESSION {session.upper()} — Checking breakouts ━━━")

        equity = self.state.equity
        if equity <= 0:
            try:
                equity = exch.get_equity(self.ex)
                self.state.update_equity(equity)
            except:
                log.error("Cannot determine equity — skipping all trades")
                return

        # ── Learning agent: capture BTC context once per batch ──
        _btc_price, _btc_chg = 0.0, 0.0
        try:
            _btc_tk = self.ex.fetch_ticker("BTC/USDT:USDT")
            _btc_price = float(_btc_tk.get("last", 0))
            _btc_chg = float(_btc_tk.get("percentage", 0) or 0)
        except Exception:
            pass
        _sess_start_h = SESSIONS.get(session, (0, 0))[0]
        _now = datetime.now(timezone.utc)
        _mins_into = (_now.hour - _sess_start_h) * 60 + _now.minute
        if _mins_into < 0:
            _mins_into += 24 * 60  # handle midnight wrap

        entries = 0

        for pair in pairs:
            # ══════════════════════════════════════════════════════
            #  C3 RETEST GATE — check pending retests from last scan
            # ══════════════════════════════════════════════════════
            _is_retest_entry = False
            _retest_data = self._pending_retests.pop(pair, None)
            _c3_candles = None
            _c3 = None

            if _retest_data is not None:
                # This pair had a C2 breakout on previous scan — check C3
                try:
                    _c3_candles = exch.fetch_latest_candles(self.ex, pair, n=6)
                    time.sleep(API_DELAY_SECS)
                    if not _c3_candles:
                        log.info(f"  {pair}: C3 retest — no candle data, discarding")
                        continue
                    _c3 = _c3_candles[-1]
                    _fc_r = _retest_data["fc"]
                    _dir_r = _retest_data["direction"]
                    if _dir_r == "long":
                        _retest_ok = (_c3["low"] <= _fc_r.high
                                      and _c3["close"] > _fc_r.high)
                    else:
                        _retest_ok = (_c3["high"] >= _fc_r.low
                                      and _c3["close"] < _fc_r.low)
                    if _retest_ok:
                        log.info(f"  ✅ {pair}: C3 RETEST CONFIRMED — "
                                 f"{_dir_r} entry at C3 close={_c3['close']:.6f}")
                        _is_retest_entry = True
                    else:
                        log.info(f"  ❌ {pair}: C3 retest FAILED — no entry "
                                 f"(C3: L={_c3['low']:.6f} H={_c3['high']:.6f} "
                                 f"C={_c3['close']:.6f})")
                        continue
                except Exception as e:
                    log.warning(f"  {pair}: C3 retest check error: {e}")
                    continue
            elif C3_RETEST_REQUIRED and pair in self._retest_attempted:
                # Already had breakout+retest this session — one shot per FC
                continue

            if _is_retest_entry:
                fc = _retest_data["fc"]
            else:
                fc = self.first_candles.get(pair)
            if fc is None or not fc.valid:
                continue

            if not self.state.can_trade(pair, session, MAX_TRADES_SESSION, MAX_TRADES_DAY):
                continue

            # ── Position cap ──
            if len(self.state.pending_entries) >= MAX_CONCURRENT_POSITIONS:
                log.info(f"  █ Position cap reached ({MAX_CONCURRENT_POSITIONS}) — "
                         f"skipping remaining pairs")
                break

            # ── Determine pair class and risk tier ──
            pair_class = self.state.get_pair_class(pair)
            risk_pct = RISK_PCT_A if pair_class == "A" else RISK_PCT_B
            scale_risk_pct = SCALE_RISK_PCT_A if pair_class == "A" else SCALE_RISK_PCT_B

            # ── B-class slot cap (margin reservation for A) ──
            if pair_class == "B":
                b_open = sum(1 for e in self.state.pending_entries
                             if isinstance(e, dict) and e.get("pair_class") == "B")
                if b_open >= MAX_CONCURRENT_B:
                    log.info(f"  {pair}: B-class cap ({MAX_CONCURRENT_B}) — "
                             f"reserving slots for A entries")
                    continue

            try:
                if _is_retest_entry:
                    # ── RETEST ENTRY: C3 confirmed — use stored C2 data ──
                    candles = _c3_candles       # C3 + history (for trend alignment)
                    candle2 = _retest_data["c2_candle"]  # original C2 for micro-filters
                    c2_close = _c3["close"]     # enter at C3 close (better entry, closer to FC)
                    direction = _retest_data["direction"]
                    log.info(f"  🔄 {pair}: RETEST ENTRY — "
                             f"C2 was {_retest_data['c2_close']:.6f}, "
                             f"C3 entry={c2_close:.6f} ({direction})")
                else:
                    # ── NORMAL: Fetch latest candles and check for breakout ──
                    # [pre3, pre2, pre1, FC, C2] = 5 candles minimum
                    candles = exch.fetch_latest_candles(self.ex, pair, n=6)
                    time.sleep(API_DELAY_SECS)  # rate-limit pacing
                    if not candles:
                        continue

                    candle2 = candles[-1]
                    c2_close = candle2["close"]

                    direction = check_breakout(fc, c2_close)
                    if direction is None:
                        # Volume intel for watchlist awareness
                        c2_vol = candle2.get("volume", 0)
                        fc_vol = fc.volume if fc.volume > 0 else 1
                        vr = c2_vol / fc_vol if fc_vol > 0 else 0
                        dist_high = abs(c2_close - fc.high) / fc.high * 100 if fc.high > 0 else 0
                        dist_low = abs(c2_close - fc.low) / fc.low * 100 if fc.low > 0 else 0
                        near = "HIGH" if dist_high < dist_low else "LOW"
                        near_pct = min(dist_high, dist_low)
                        vol_flag = " ⚡VOL" if vr >= 1.5 and near_pct < 0.3 else ""
                        log.info(f"  {pair}: no breakout (close={c2_close:.6f}, "
                                 f"range=[{fc.low:.6f}, {fc.high:.6f}]) "
                                 f"vol={vr:.1f}x near_{near}={near_pct:.2f}%{vol_flag}")
                        continue

                    # ── C3 RETEST: queue breakout for next scan ──
                    if C3_RETEST_REQUIRED:
                        self._pending_retests[pair] = {
                            "fc": fc,
                            "direction": direction,
                            "c2_candle": candle2,
                            "c2_close": c2_close,
                            "candles": candles,
                            "pair_class": pair_class,
                        }
                        self._retest_attempted.add(pair)
                        log.info(f"  ⏳ {pair}: {direction.upper()} breakout at "
                                 f"{c2_close:.6f} — queued for C3 retest confirmation")
                        continue

                # Compute signal with position sizing
                info = self.market_info.get(pair, {})
                signal = compute_signal(
                    fc=fc,
                    direction=direction,
                    entry_price=c2_close,
                    equity=equity,
                    contract_size=info.get("contract_size") or 1,
                    price_precision=info.get("price_precision") or 4,
                    qty_precision=info.get("amount_precision") or 2,
                    min_qty=info.get("min_qty") or 0.001,
                    min_notional=info.get("min_notional") or 5.0,
                    risk_pct=risk_pct,
                )

                if signal is None:
                    log.warning(f"  {pair}: signal rejected (position too small)")
                    continue

                # ═══════════════════════════════════════════════════
                #  PRE-ENTRY CONFIDENCE CHECK — never trade blind
                # ═══════════════════════════════════════════════════

                # ── TREND ALIGNMENT from pre-FC candles ──
                # Oracle's 3rd strongest weight (+0.153). Use 3 pre-FC candles
                # to determine if recent momentum matches breakout direction.
                _trend_aligned = False
                _pre_fc_candles = candles[:-2]  # everything before FC and C2
                if len(_pre_fc_candles) >= 2:
                    # Net direction of the 2-3 candles preceding FC
                    _pre_net = _pre_fc_candles[-1]["close"] - _pre_fc_candles[0]["open"]
                    if direction == "long" and _pre_net > 0:
                        _trend_aligned = True
                    elif direction == "short" and _pre_net < 0:
                        _trend_aligned = True

                # ── INTEL CONTEXT from pair profile (scanner phase) ──
                _pair_profile = self.pair_profiles.get(pair)
                _ctx_flags = []
                _ctx_score = 0  # context score: positive=confident, negative=hostile
                _has_intel = _pair_profile is not None

                if _has_intel:
                    # 1. Congestion zone blocking TP path?
                    _tp_price = signal.take_profit
                    _blocking_zones = get_congestion_zones_for_trade(
                        _pair_profile, c2_close, _tp_price, signal.stop_loss
                    )
                    if _blocking_zones:
                        _strongest = max(_blocking_zones, key=lambda z: z.strength)
                        _ctx_flags.append(
                            f"TP_BLOCKED(zone@{_strongest.midpoint:.4f} "
                            f"str={_strongest.strength:.0%})"
                        )
                        _ctx_score -= 15  # congestion in profit path

                    # 2. Volatility regime
                    if _pair_profile.atr_ratio < 0.7:
                        _ctx_flags.append(f"DEAD_VOL(atr×{_pair_profile.atr_ratio:.2f})")
                        _ctx_score -= 15  # dead vol = trail captures nothing
                    elif _pair_profile.atr_ratio >= 1.3:
                        _ctx_flags.append(f"HOT_VOL(atr×{_pair_profile.atr_ratio:.2f})")
                        _ctx_score += 10  # elevated vol = bigger R-moves
                    elif _pair_profile.atr_ratio >= 1.0:
                        _ctx_score += 3   # normal — mild positive

                    # 3. Breakout follow-through quality
                    if _pair_profile.breakout_follow_pct < 0.20:
                        _ctx_flags.append(f"BAD_FOLLOW({_pair_profile.breakout_follow_pct:.0%})")
                        _ctx_score -= 15  # <20% follow = this pair doesn't breakout
                    elif _pair_profile.breakout_follow_pct < 0.30:
                        _ctx_flags.append(f"WEAK_FOLLOW({_pair_profile.breakout_follow_pct:.0%})")
                        _ctx_score -= 5
                    elif _pair_profile.breakout_follow_pct >= 0.45:
                        _ctx_flags.append(f"STRONG_FOLLOW({_pair_profile.breakout_follow_pct:.0%})")
                        _ctx_score += 10
                    else:
                        _ctx_score += 3   # normal follow

                    # 4. Session momentum alignment
                    if _pair_profile.session_trend_strength >= 0.4:
                        _ctx_flags.append(f"SESS_TREND({_pair_profile.session_trend_strength:.0%})")
                        _ctx_score += 5
                    elif _pair_profile.session_trend_strength < 0.15:
                        _ctx_flags.append(f"SESS_CHOP({_pair_profile.session_trend_strength:.0%})")
                        _ctx_score -= 5   # pair chops in this session

                    # 5. POC proximity (volume magnet)
                    if _pair_profile.poc_distance_pct < 0.3:
                        _ctx_flags.append(f"AT_POC({_pair_profile.poc_distance_pct:.1f}%)")
                        _ctx_score -= 10  # sitting on max-volume level
                    elif _pair_profile.poc_distance_pct > 2.0:
                        _ctx_score += 5   # well clear of volume gravity

                    # 6. Congestion right at current price
                    if _pair_profile.zone_near_entry:
                        _ctx_flags.append("CONGESTION_AT_ENTRY")
                        _ctx_score -= 10

                    # 7. High reversal rate
                    if _pair_profile.breakout_reversal_pct > 0.40:
                        _ctx_flags.append(f"HIGH_REVERSAL({_pair_profile.breakout_reversal_pct:.0%})")
                        _ctx_score -= 10

                    # 8. Support & Resistance context
                    _sr_ctx = get_sr_context_for_trade(
                        _pair_profile, c2_close, signal.take_profit,
                        signal.stop_loss, direction
                    )
                    _ctx_score += _sr_ctx["score_adj"]
                    _ctx_flags.extend(_sr_ctx["flags"])
                    if _sr_ctx["blocking"]:
                        _n_blockers = len(_sr_ctx["blocking"])
                        _max_str = max(l.strength_score for l in _sr_ctx["blocking"])
                        log.info(f"  S/R {pair}: {_n_blockers} level(s) blocking TP path "
                                 f"(max_str={_max_str:.2f})")
                    if _sr_ctx.get("at_level"):
                        _lev = _sr_ctx["at_level"]
                        log.info(f"  S/R {pair}: entry near {_lev.level_type} "
                                 f"@ {_lev.price:.4f} ({_lev.strength}, {_lev.touches}t)")
                else:
                    # No intel data — flag it. B-class without intel = unknown risk.
                    _ctx_flags.append("NO_INTEL")
                    if pair_class == "B":
                        _ctx_score -= 10  # B-class without intel = risky blind entry

                # ── BTC regime — alts follow BTC, ignore at your peril ──
                # _btc_chg is 24h % change, fetched once per session scan
                if _btc_chg <= -5.0 and direction == "long":
                    _ctx_flags.append(f"BTC_CRASH({_btc_chg:+.1f}%)")
                    _ctx_score -= 20  # alt longs during BTC crash → near-certain loss
                elif _btc_chg <= -3.0 and direction == "long":
                    _ctx_flags.append(f"BTC_DUMP({_btc_chg:+.1f}%)")
                    _ctx_score -= 10  # BTC weak, alt longs risky
                elif _btc_chg >= 5.0 and direction == "short":
                    _ctx_flags.append(f"BTC_RALLY({_btc_chg:+.1f}%)")
                    _ctx_score -= 15  # shorting alts during BTC rip
                elif _btc_chg >= 3.0 and direction == "short":
                    _ctx_flags.append(f"BTC_PUMP({_btc_chg:+.1f}%)")
                    _ctx_score -= 8   # BTC strong, alt shorts headwind
                elif abs(_btc_chg) <= 1.5:
                    _ctx_score += 3   # calm BTC → alts trade on own merit

                # ── Trend alignment scoring (asymmetric: counter-trend is a real penalty) ──
                if _trend_aligned:
                    _ctx_flags.append("TREND_ALIGNED")
                    _ctx_score += 8   # momentum backing the breakout
                else:
                    _ctx_flags.append("COUNTER_TREND")
                    _ctx_score -= 5   # fighting the trend = lower probability

                # ── COMPOSITE CONFIDENCE GRADE ──
                _ctx_grade = ("STRONG" if _ctx_score >= 15
                              else "GOOD" if _ctx_score >= 5
                              else "NEUTRAL" if _ctx_score >= -5
                              else "WEAK" if _ctx_score >= -15
                              else "HOSTILE")
                _ctx_str = " | ".join(_ctx_flags) if _ctx_flags else "no_data"
                log.info(f"  CTX {pair}: {_ctx_grade} ({_ctx_score:+d}) | {_ctx_str}")

                # ═══════════════════════════════════════════════════
                #  CONFIDENCE GATE — hard-block hostile context
                # ═══════════════════════════════════════════════════
                # Don't enter blindly. If everything says the environment is
                # against us, stepping aside is smarter than entering small.
                if _ctx_score <= -25:
                    log.info(f"  BLOCK {pair}: HOSTILE context ({_ctx_score:+d}) "
                             f"— environment too dangerous, skipping")
                    self._log_skipped_trade(
                        pair, session, direction, c2_close, fc,
                        signal, 0.0, f"context_hostile_{_ctx_score}",
                    )
                    continue

                # ── CONTEXT RISK ADJUSTMENT ──
                if _ctx_score >= 15:
                    _ctx_risk_mult = 1.0     # strong conviction — full size
                elif _ctx_score >= 5:
                    _ctx_risk_mult = 1.0     # good — full size
                elif _ctx_score >= -5:
                    _ctx_risk_mult = 0.85    # neutral — slight caution
                elif _ctx_score >= -15:
                    _ctx_risk_mult = 0.65    # weak — meaningful reduction
                else:
                    _ctx_risk_mult = 0.50    # hostile but not blocked (score -16 to -24)

                risk_pct = risk_pct * _ctx_risk_mult
                if _ctx_risk_mult < 1.0:
                    log.info(f"  CTX {pair}: risk ×{_ctx_risk_mult:.0%} "
                             f"({_ctx_grade})")

                # Recompute signal with context-adjusted risk
                if _ctx_risk_mult < 1.0:
                    signal = compute_signal(
                        fc=fc,
                        direction=direction,
                        entry_price=c2_close,
                        equity=equity,
                        contract_size=info.get("contract_size") or 1,
                        price_precision=info.get("price_precision") or 4,
                        qty_precision=info.get("amount_precision") or 2,
                        min_qty=info.get("min_qty") or 0.001,
                        min_notional=info.get("min_notional") or 5.0,
                        risk_pct=risk_pct,
                    )
                    if signal is None:
                        log.warning(f"  {pair}: too small after context adjustment")
                        continue

                # ── ENTRY INTELLIGENCE: Measure slip (log only, never skip) ──
                fc_boundary = fc.high if direction == "long" else fc.low
                if direction == "long":
                    slip_r = (c2_close - fc.high) / signal.risk_per_unit
                else:
                    slip_r = (fc.low - c2_close) / signal.risk_per_unit

                c2_body = abs(candle2["close"] - candle2["open"])
                c2_range = candle2["high"] - candle2["low"]
                c2_body_ratio = c2_body / c2_range if c2_range > 0 else 0

                # ── FC lean direction for micro-filter ──
                fc_body_dir = "long" if fc.close > fc.open else "short"
                fc_is_counter = (fc_body_dir != direction)  # FC leans opposite breakout

                # ── VOLUME CONFIRMATION ──
                c2_vol = candle2.get("volume", 0)
                fc_vol = fc.volume if fc.volume > 0 else 1
                vol_ratio = c2_vol / fc_vol if fc_vol > 0 else 0
                vol_confirm = vol_ratio >= 1.0  # breakout candle has higher vol than FC
                vol_tag = "📈" if vol_confirm else "📉"

                slip_tag = "⚡" if slip_r <= 0.3 else ("⚠" if slip_r <= MAX_SLIP_R else "🔥")
                log.info(f"  {slip_tag} {pair}: slip={slip_r:.3f}R | "
                         f"body={c2_body_ratio:.0%} | "
                         f"fc_counter={'Y' if fc_is_counter else 'N'} | "
                         f"vol={vol_tag}{vol_ratio:.1f}x | "
                         f"{'STRONG' if c2_body_ratio >= 0.6 and slip_r <= 0.5 and vol_confirm else 'ENTER'}")

                # ── FOMO SPIKE FILTER (5m) ──
                # Live data: c2_body > 0.85 → 100% losers (panic candles that reverse)
                if c2_body_ratio > MAX_C2_BODY_RATIO:
                    log.info(f"  ✋ {pair}: FILTERED — FOMO spike "
                             f"body={c2_body_ratio:.0%} > {MAX_C2_BODY_RATIO:.0%} max")
                    self._log_skipped_trade(
                        pair, session, direction, c2_close, fc,
                        signal, slip_r, "fomo_spike",
                    )
                    continue

                # ── MICRO-FILTER GATE (5m) ──
                # Sweep proved: c2_body>=0.5 + fc_counter → WR 45.3%, x1000 in 280t
                if MICRO_FILTER_ENABLED:
                    if c2_body_ratio < MIN_C2_BODY_RATIO:
                        log.info(f"  ✋ {pair}: FILTERED — weak C2 body "
                                 f"{c2_body_ratio:.0%} < {MIN_C2_BODY_RATIO:.0%}")
                        self._log_skipped_trade(
                            pair, session, direction, c2_close, fc,
                            signal, slip_r, "micro_weak_c2_body",
                        )
                        continue
                    if FC_COUNTER_5M and not fc_is_counter:
                        log.info(f"  ✋ {pair}: FILTERED — FC leaned {fc_body_dir} "
                                 f"(same as {direction}), need counter-lean")
                        self._log_skipped_trade(
                            pair, session, direction, c2_close, fc,
                            signal, slip_r, "micro_fc_not_counter",
                        )
                        continue

                # ── VOLUME FILTER (5m) ──
                # Live data: low-vol longs (vol<1x FC) mostly lose.
                # Shorts can work with less volume (gravity assists).
                if VOL_FILTER_ENABLED:
                    min_vol = MIN_VOL_RATIO_LONG if direction == "long" else MIN_VOL_RATIO_SHORT
                    if vol_ratio < min_vol:
                        log.info(f"  ✋ {pair}: FILTERED — weak volume "
                                 f"{vol_ratio:.1f}x < {min_vol:.1f}x min for {direction}")
                        self._log_skipped_trade(
                            pair, session, direction, c2_close, fc,
                            signal, slip_r, f"vol_too_low_{direction}",
                        )
                        continue

                # ── FUNDING RATE BIAS FILTER (5m) ──
                # Skip trades going WITH the over-leveraged crowd
                if FUNDING_FILTER_ENABLED:
                    try:
                        _fr = exch.get_funding_rate(self.ex, pair)
                        if direction == "long" and _fr >= FUNDING_EXTREME_RATE:
                            log.info(f"  ✋ {pair}: FILTERED — funding={_fr*100:.3f}% "
                                     f"(crowd is long, long=trap)")
                            self._log_skipped_trade(
                                pair, session, direction, c2_close, fc,
                                signal, slip_r, "funding_bias_long",
                            )
                            continue
                        if direction == "short" and _fr <= FUNDING_EXTREME_NEG:
                            log.info(f"  ✋ {pair}: FILTERED — funding={_fr*100:.3f}% "
                                     f"(crowd is short, short=trap)")
                            self._log_skipped_trade(
                                pair, session, direction, c2_close, fc,
                                signal, slip_r, "funding_bias_short",
                            )
                            continue
                    except Exception:
                        pass  # fail-open: if API fails, allow trade

                if HYBRID_ENTRY and slip_r > MAX_SLIP_R:
                    log.info(f"  ✋ {pair}: SKIPPED — slip={slip_r:.3f}R > {MAX_SLIP_R}R")
                    self._log_skipped_trade(
                        pair, session, direction, c2_close, fc,
                        signal, slip_r, "slip_exceeded",
                    )
                    tlog.log_skip(
                        symbol=pair, session=session, direction=direction,
                        fc_high=fc.high, fc_low=fc.low, fc_range_pct=fc.range_pct,
                        slip_r=slip_r, reason="slip_exceeded", c2_close=c2_close,
                        pair_class=pair_class, equity=equity, risk_pct=risk_pct,
                        fc_open=fc.open, fc_close=fc.close, fc_volume=fc.volume,
                        fc_midpoint=fc.midpoint,
                        c2_open=candle2["open"], c2_high=candle2["high"],
                        c2_low=candle2["low"], c2_body_ratio=c2_body_ratio,
                        c2_volume=candle2.get("volume", 0),
                        signal_qty=signal.position_size, signal_fee_r=signal.fee_r,
                        open_positions=len(self.state.pending_entries),
                    )
                    continue

                # ── DIRECTION CONFIDENCE → RISK SIZING ──
                # Oracle: per-trade structural quality scoring.
                # Compute additional oracle features from FC + C2 candle data.
                fc_range = fc.high - fc.low
                fc_body_size = abs(fc.close - fc.open)
                fc_lower_wick = min(fc.open, fc.close) - fc.low
                _fc_lower_wick_pct = fc_lower_wick / fc_range if fc_range > 0 else 0

                # C2 momentum: how far past FC boundary did C2 close?
                if direction == "long":
                    _c2_momentum = (c2_close - fc.high) / fc_range if fc_range > 0 else 0
                    # C2 against-wick: lower wick on C2 (against long direction)
                    _c2_against_wick = min(candle2["open"], candle2["close"]) - candle2["low"]
                else:
                    _c2_momentum = (fc.low - c2_close) / fc_range if fc_range > 0 else 0
                    # C2 against-wick: upper wick on C2 (against short direction)
                    _c2_against_wick = candle2["high"] - max(candle2["open"], candle2["close"])

                _c2_against_wick_pct = _c2_against_wick / c2_range if c2_range > 0 else 0

                _edge = _edge_score(
                    direction=direction,
                    session=session,
                    fc_range_pct=fc.range_pct,
                    c2_body_ratio=c2_body_ratio,
                    fee_r=signal.fee_r,
                    vol_ratio=vol_ratio,
                    slip_r=slip_r,
                    minutes_into_session=_mins_into,
                    is_15m=False,
                    # Oracle structural features
                    c2_body=c2_body,
                    fc_body=fc_body_size,
                    fc_is_counter=fc_is_counter,
                    trend_aligned=_trend_aligned,  # computed from pre-FC candle
                    c3_hold_strength=0.0,  # no C3 in live (enter at C2)
                    c2_momentum=_c2_momentum,
                    fc_lower_wick_pct=_fc_lower_wick_pct,
                    c2_against_wick_pct=_c2_against_wick_pct,
                )
                log.info(f"  >> {pair}: {_edge_fmt(_edge)}")

                # Apply risk multiplier — confident = full size, unsure = small
                risk_pct = risk_pct * _edge["risk_mult"]

                # ═══════════════════════════════════════════════════
                #  DYNAMIC ENGINE — real-time adaptive intelligence
                # ═══════════════════════════════════════════════════
                _dyn_mult = 1.0
                _dyn_flags = []
                if self.dynamic:
                    _dyn_take, _dyn_mult, _dyn_flags = self.dynamic.evaluate_entry(
                        pair=pair,
                        pair_class=pair_class,
                        direction=direction,
                        ctx_score=_ctx_score,
                        ctx_grade=_ctx_grade,
                        edge_tier=_edge["tier"],
                        edge_risk_mult=_edge["risk_mult"],
                        equity=equity,
                    )
                    _dyn_str = " | ".join(_dyn_flags) if _dyn_flags else "ok"
                    log.info(f"  DYN {pair}: {'TAKE' if _dyn_take else 'BLOCK'} "
                             f"×{_dyn_mult:.2f} | {_dyn_str}")
                    log.info(f"  DYN STATUS: {self.dynamic.status_line(equity)}")

                    if not _dyn_take:
                        log.info(f"  ✋ {pair}: DYNAMIC ENGINE BLOCKED — {_dyn_str}")
                        self._log_skipped_trade(
                            pair, session, direction, c2_close, fc,
                            signal, slip_r, f"dynamic_block_{_dyn_flags[0] if _dyn_flags else 'unknown'}",
                        )
                        continue

                    # Apply dynamic multiplier to risk
                    if _dyn_mult != 1.0:
                        risk_pct = risk_pct * _dyn_mult

                if _edge["risk_mult"] < 1.0 or _ctx_risk_mult < 1.0 or _dyn_mult != 1.0:
                    signal = compute_signal(
                        fc=fc,
                        direction=direction,
                        entry_price=c2_close,
                        equity=equity,
                        contract_size=info.get("contract_size") or 1,
                        price_precision=info.get("price_precision") or 4,
                        qty_precision=info.get("amount_precision") or 2,
                        min_qty=info.get("min_qty") or 0.001,
                        min_notional=info.get("min_notional") or 5.0,
                        risk_pct=risk_pct,
                    )
                    if signal is None:
                        log.warning(f"  {pair}: sized too small after confidence adj")
                        continue

                # Round to exchange precision
                sl_price = exch.round_price(self.ex, pair, signal.stop_loss)
                tp_price = exch.round_price(self.ex, pair, signal.take_profit)
                qty = exch.round_qty(self.ex, pair, signal.position_size)

                # ── EXCHANGE TP: fixed 1.5R take profit on exchange ──
                base_tp_price = tp_price  # store the 1.5R level for reference
                if direction == "long":
                    far_tp = c2_close + EXCHANGE_TP_R * signal.risk_per_unit
                else:
                    far_tp = c2_close - EXCHANGE_TP_R * signal.risk_per_unit
                exchange_tp = exch.round_price(self.ex, pair, far_tp)
                if TRAIL_ENABLED:
                    log.info(f"  ↳ TRAIL MODE: exchange TP={exchange_tp} ({EXCHANGE_TP_R}R safety net), "
                             f"Guardian trails SL at peak-{TRAIL_DISTANCE_R}R once R>={TRAIL_ACTIVATION_R}")
                else:
                    log.info(f"  ↳ FIXED TP: {exchange_tp} ({EXCHANGE_TP_R}R) — "
                             f"progressive SL tiers active for loss protection")

                if qty <= 0:
                    log.warning(f"  {pair}: qty rounds to 0, skipping")
                    continue

                side = "buy" if direction == "long" else "sell"

                # Guard: check for existing open position on this pair
                try:
                    existing = exch.get_open_positions(self.ex, pair)
                    if existing:
                        log.warning(f"  {pair}: already has open position, skipping new entry")
                        continue
                except Exception:
                    pass  # If check fails, proceed cautiously

                # Guardian pre-entry check (margin + duplicate)
                if self.guardian:
                    needed_margin = (qty * c2_close) / LEVERAGE
                    ok, reason = self.guardian.pre_entry_check(pair, needed_margin)
                    if not ok:
                        log.warning(f"  {pair}: GUARDIAN BLOCKED — {reason}")
                        continue

                log.info(f"  ★ BREAKOUT {pair} [Class {pair_class}]: {direction.upper()} "
                         f"entry~{c2_close:.6f} SL={sl_price} TP={exchange_tp} "
                         f"qty={qty} risk=${equity * risk_pct:.2f} "
                         f"feeR={signal.fee_r:.3f} edge={_edge['score']}/10 [TRAIL v3]")
                log.audit("BREAKOUT_DETECTED", symbol=pair, direction=direction,
                          c2_close=f"{c2_close:.6f}", fc_high=f"{fc.high:.6f}",
                          fc_low=f"{fc.low:.6f}", range_pct=f"{fc.range_pct*100:.3f}%",
                          trail_mode="guardian_v3",
                          edge_score=_edge["score"],
                          edge_flags=",".join(_edge["flags"]),
                          risk_mult=f"{_edge['risk_mult']:.2f}",
                          context_grade=_ctx_grade,
                          context_score=_ctx_score,
                          context_risk_mult=f"{_ctx_risk_mult:.2f}",
                          trend_aligned=_trend_aligned,
                          context_flags=",".join(_ctx_flags),
                          btc_regime=f"{_btc_chg:+.1f}%",
                          dyn_mult=f"{_dyn_mult:.2f}",
                          dyn_flags=",".join(_dyn_flags))

                # Place the order
                _bid, _ask = 0.0, 0.0
                try:
                    _ticker = exch.get_ticker(self.ex, pair)
                    _bid = float(_ticker.get("bid", 0) or 0)
                    _ask = float(_ticker.get("ask", 0) or 0)
                except Exception:
                    pass

                # ── LIQUIDITY CHECK: skip thin order books (SL slippage protection) ──
                if SPREAD_FILTER_ENABLED and _bid > 0 and _ask > 0:
                    _mid = (_bid + _ask) / 2
                    _spread_pct = (_ask - _bid) / _mid * 100
                    _turnover = float(_ticker.get("quoteVolume", 0) or 0)
                    if _spread_pct > MAX_SPREAD_PCT:
                        log.info(f"  ✋ {pair}: LIQUIDITY SKIP — spread={_spread_pct:.3f}% > {MAX_SPREAD_PCT}% (thin book)")
                        self._log_skipped_trade(
                            pair, session, direction, c2_close, fc,
                            signal, slip_r, f"spread_too_wide",
                        )
                        continue
                    if _turnover < MIN_TURNOVER_USDT:
                        log.info(f"  ✋ {pair}: LIQUIDITY SKIP — 24h vol=${_turnover/1e6:.1f}M < ${MIN_TURNOVER_USDT/1e6:.0f}M min")
                        self._log_skipped_trade(
                            pair, session, direction, c2_close, fc,
                            signal, slip_r, f"turnover_too_low",
                        )
                        continue

                order = exch.place_market_order(
                    self.ex, pair, side, qty, sl_price, exchange_tp
                )

                # Record IMMEDIATELY — before scale-in, to survive crashes
                order_id = order.get("id", "unknown")
                fill_price = float(order.get("average") or order.get("price") or c2_close)

                # ── POST-FILL: Recalculate TP & risk from ACTUAL fill price ──
                # Pre-fill values used c2_close (estimate). Now we have the real fill.
                actual_risk = abs(fill_price - signal.stop_loss)
                if actual_risk > 0:
                    risk_per_unit = actual_risk
                else:
                    risk_per_unit = signal.risk_per_unit  # fallback

                if direction == "long":
                    actual_tp = fill_price + EXCHANGE_TP_R * risk_per_unit
                    actual_base_tp = fill_price + TP_R * risk_per_unit
                else:
                    actual_tp = fill_price - EXCHANGE_TP_R * risk_per_unit
                    actual_base_tp = fill_price - TP_R * risk_per_unit
                actual_tp = exch.round_price(self.ex, pair, actual_tp)
                actual_base_tp = exch.round_price(self.ex, pair, actual_base_tp)
                actual_fee_r = 2.0 * FEE_RATE * fill_price / risk_per_unit

                # Update exchange SL/TP if fill differs from estimate
                if fill_price != c2_close:
                    try:
                        exch.set_trading_stop(
                            self.ex, pair, side=direction,
                            sl_price=sl_price, tp_price=actual_tp,
                        )
                        log.info(f"  ↳ POST-FILL: TP adjusted {exchange_tp} → {actual_tp} "
                                 f"(fill={fill_price:.6f} vs est={c2_close:.6f}, "
                                 f"risk={risk_per_unit:.6f})")
                    except Exception as e:
                        log.warning(f"  {pair}: post-fill TP update failed — {e}")

                exchange_tp = actual_tp
                base_tp_price = actual_base_tp

                trade_log.log_entry(
                    symbol=pair, session=session, direction=direction,
                    price=fill_price, qty=qty, sl=sl_price, tp=exchange_tp,
                    risk_per_unit=risk_per_unit, fee_r=actual_fee_r,
                    order_id=order_id, equity=equity,
                    notes=f"fc_high={fc.high} fc_low={fc.low} TRAIL_v3 ctx={_ctx_grade}({_ctx_score:+d}) trend={'Y' if _trend_aligned else 'N'}",
                )
                log.position_opened(pair, direction, fill_price, qty,
                                    sl_price, exchange_tp, equity * risk_pct)
                tlog.log_entry(
                    symbol=pair, session=session, direction=direction,
                    fill_price=fill_price, qty=qty,
                    sl=sl_price, tp=base_tp_price, exchange_tp=exchange_tp,
                    risk_per_unit=risk_per_unit, fee_r=actual_fee_r,
                    risk_usd=equity * risk_pct, risk_pct=risk_pct,
                    equity=equity, pair_class=pair_class,
                    fc_high=fc.high, fc_low=fc.low,
                    fc_range_pct=fc.range_pct, fc_midpoint=fc.midpoint,
                    slip_r=slip_r, c2_close=c2_close,
                    c2_body_ratio=c2_body_ratio, order_id=order_id,
                    fc_open=fc.open, fc_close=fc.close, fc_volume=fc.volume,
                    c2_open=candle2["open"], c2_high=candle2["high"],
                    c2_low=candle2["low"], c2_volume=candle2.get("volume", 0),
                    bid=_bid, ask=_ask,
                    open_positions=len(self.state.pending_entries),
                    entries_today=self.state.entries_today,
                    consec_wins=self.state.pair_classes.get(pair, {}).get("consec_wins", 0),
                    consec_losses=self.state.pair_classes.get(pair, {}).get("consec_losses", 0),
                    live_wins=self.state.pair_classes.get(pair, {}).get("live_wins", 0),
                    live_losses=self.state.pair_classes.get(pair, {}).get("live_losses", 0),
                    day_of_week=datetime.now(timezone.utc).weekday(),
                    btc_price=_btc_price, btc_change_pct=_btc_chg,
                    sim_breakouts=entries, mins_into_session=_mins_into,
                    edge_score=_edge["score"],
                    edge_flags=",".join(_edge["flags"]),
                    edge_risk_mult=_edge["risk_mult"],
                )

                entry_data = {
                    "symbol": pair, "session": session, "direction": direction,
                    "entry_price": fill_price, "sl": sl_price, "tp": exchange_tp,
                    "qty": qty, "order_id": order_id,
                    "fee_r": actual_fee_r,
                    "pair_class": pair_class,
                    "risk_pct": risk_pct,
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "scale_order_id": None, "scale_status": "none",
                    # Guardian v3 tracking
                    "base_tp_price": base_tp_price,  # real 1.5R level for reference
                    "peak_price": fill_price,
                    "risk_per_unit": risk_per_unit,
                    "original_sl": signal.stop_loss,
                    # Analysis tracking
                    "_slip_r": slip_r,
                    "_fc_range_pct": fc.range_pct,
                    "_edge_score": _edge["score"],
                    "_dyn_mult": _dyn_mult,
                    "_dyn_flags": _dyn_flags,
                }
                self.state.record_entry(pair, session, entry_data)

                # Track this pair as entered for continuous scan
                if session not in self._5m_entered:
                    self._5m_entered[session] = set()
                self._5m_entered[session].add(pair)

                # ── SCALE-OUT: reduce-only limit at FC boundary ──
                # If price retraces to FC boundary, close 50% to cap losses.
                # On wins price goes straight to TP — this order never fills.
                # On losses price crosses FC boundary — we escape half early.
                # Sim on 9 live trades: +2.3R saved, expectancy +0.258→+0.514R.
                if SCALE_OUT:
                    try:
                        fc_boundary = fc.high if direction == "long" else fc.low
                        scale_out_price = exch.round_price(self.ex, pair, fc_boundary)
                        close_side = "sell" if direction == "long" else "buy"
                        scale_out_qty = exch.round_qty(self.ex, pair, qty * SCALE_OUT_PCT)

                        if scale_out_qty > 0:
                            log.info(f"  ↳ SCALE-OUT: conditional {close_side.upper()} {scale_out_qty} {pair} "
                                     f"trigger @ {scale_out_price} (close {SCALE_OUT_PCT*100:.0f}% on retrace)")

                            scale_order = exch.place_reduce_only_stop(
                                self.ex, pair, close_side, scale_out_qty,
                                scale_out_price, direction
                            )
                            scale_order_id = scale_order.get("id", "unknown")

                            entry_data["scale_order_id"] = scale_order_id
                            entry_data["scale_limit_price"] = scale_out_price
                            entry_data["scale_qty"] = scale_out_qty
                            entry_data["scale_status"] = "pending"
                            entry_data["scale_type"] = "out"
                            self.state._save()

                            trade_log.log_entry(
                                symbol=pair, session=session, direction=direction,
                                price=scale_out_price, qty=scale_out_qty,
                                sl=sl_price, tp=tp_price,
                                risk_per_unit=signal.risk_per_unit,
                                fee_r=signal.fee_r,
                                order_id=scale_order_id, equity=equity,
                                notes=f"SCALE-OUT conditional stop @ FC boundary",
                            )
                        else:
                            log.info(f"  ↳ SCALE-OUT: qty rounds to 0, skipped")
                    except Exception as e:
                        log.warning(f"  ↳ SCALE-OUT failed for {pair}: {e}")

                # ── SPLIT ENTRY: Place limit scale-in at FC boundary (legacy, disabled) ──
                elif SPLIT_ENTRY:
                    try:
                        scale_sig = compute_scale_signal(
                            fc=fc,
                            direction=direction,
                            equity=equity,
                            contract_size=info.get("contract_size") or 1,
                            price_precision=info.get("price_precision") or 4,
                            qty_precision=info.get("amount_precision") or 2,
                            min_qty=info.get("min_qty") or 0.001,
                            min_notional=info.get("min_notional") or 5.0,
                            risk_pct=scale_risk_pct,
                        )
                        if scale_sig is not None:
                            scale_limit = exch.round_price(self.ex, pair, scale_sig.limit_price)
                            scale_sl = exch.round_price(self.ex, pair, scale_sig.stop_loss)
                            # Use the BASE entry's TP for the scale-in order.
                            # On Bybit, when positions merge the limit order's
                            # SL/TP overwrites the base position's.  By using
                            # the same TP we eliminate the post-fill correction
                            # race and the "zero position" errors it can cause.
                            scale_tp = tp_price
                            scale_qty = exch.round_qty(self.ex, pair, scale_sig.position_size)

                            if scale_qty > 0:
                                log.info(f"  ↳ SCALE-IN: limit {side.upper()} {scale_qty} {pair} "
                                         f"@ {scale_limit} | SL={scale_sl} TP={scale_tp} "
                                         f"risk=${equity * scale_risk_pct:.2f}")

                                scale_order = exch.place_limit_order(
                                    self.ex, pair, side, scale_qty,
                                    scale_limit, scale_sl, scale_tp
                                )
                                scale_order_id = scale_order.get("id", "unknown")

                                # Update entry_data with scale info
                                entry_data["scale_order_id"] = scale_order_id
                                entry_data["scale_limit_price"] = scale_limit
                                entry_data["scale_qty"] = scale_qty
                                entry_data["scale_sl"] = scale_sl
                                entry_data["scale_tp"] = scale_tp
                                entry_data["scale_status"] = "pending"
                                self.state._save()

                                trade_log.log_entry(
                                    symbol=pair, session=session, direction=direction,
                                    price=scale_limit, qty=scale_qty,
                                    sl=scale_sl, tp=scale_tp,
                                    risk_per_unit=scale_sig.risk_per_unit,
                                    fee_r=scale_sig.fee_r,
                                    order_id=scale_order_id, equity=equity,
                                    notes=f"SCALE-IN limit @ FC boundary",
                                )
                            else:
                                log.info(f"  ↳ SCALE-IN: qty rounds to 0, skipped")
                        else:
                            log.info(f"  ↳ SCALE-IN: signal rejected (too small)")
                    except Exception as e:
                        log.warning(f"  ↳ SCALE-IN failed for {pair}: {e}")

                entries += 1

                # Brief pause between orders to respect rate limits
                time.sleep(0.5)

            except ccxt.InsufficientFunds:
                log.warning(f"  {pair}: INSUFFICIENT MARGIN — skipping "
                            f"(will try remaining cheaper pairs)")
                continue  # Try next pair — it may need less margin

            except ccxt.BadRequest as e:
                err_msg = str(e).lower()
                if "exceeds maximum limit" in err_msg:
                    log.warning(f"  {pair}: ORDER REJECTED (max qty exceeded after clamping) — {e}")
                else:
                    log.error(f"  {pair}: error processing breakout — {e}")
                    log.debug(traceback.format_exc())

            except Exception as e:
                log.error(f"  {pair}: error processing breakout — {e}")
                log.debug(traceback.format_exc())

        log.info(f"Session {session}: {entries} entries placed out of {len(pairs)} pairs")

        # Update equity after entries
        try:
            equity = exch.get_equity(self.ex)
            self.state.update_equity(equity)
        except:
            pass

    # (15m FCB removed — 5m only with dynamic scanner)

    # ═══════════════════════════════════════════════════════════
    #  POSITION MONITORING — C3 Fakeout + Health Logging
    # ═══════════════════════════════════════════════════════════

    def _trail_positions(self):
        """Post-entry position monitoring. Runs every 15s from _monitor_and_sleep.

        Two active systems:
          1. C3 FAKEOUT DETECTION — at 5 minutes post-entry, analyze C3 candle.
             If C3 reverses (body against us > 30%) AND we're negative → exit.
             100% precision: fired on 2/7 losers, 0/8 winners in live data.
          2. POSITION HEALTH — log current R every poll cycle for visibility.

        NOTE: Trailing SL is handled by Profit Guardian v3 daemon thread
        (polls every 2s, trails 0.3R behind peak once R>=1.0).
        No trailing code needed here.

        SL only moves forward (never backward) — exchange handles the exit.
        """
        if not self.state.pending_entries:
            return

        for entry in self.state.pending_entries:
            symbol = entry.get("symbol", "")
            direction = entry.get("direction", "")
            entry_price = entry.get("entry_price", 0)
            risk_per_unit = entry.get("risk_per_unit") or abs(entry_price - entry.get("sl", 0))

            if risk_per_unit <= 0:
                continue

            try:
                # ── Get current price ──
                ticker = exch.get_ticker(self.ex, symbol)
                current_price = float(ticker.get("last", 0))
                if not current_price:
                    continue

                # Calculate current R
                if direction == "long":
                    current_r = (current_price - entry_price) / risk_per_unit
                else:
                    current_r = (entry_price - current_price) / risk_per_unit

                # ── POSITION HEALTH LOG ──
                entry_time_str = entry.get("entry_time", "")
                try:
                    if entry_time_str:
                        et = datetime.fromisoformat(entry_time_str)
                        if et.tzinfo is None:
                            et = et.replace(tzinfo=timezone.utc)
                        elapsed_min = (datetime.now(timezone.utc) - et).total_seconds() / 60
                    else:
                        elapsed_min = 999
                except:
                    elapsed_min = 999

                # Log health every ~60 seconds (every 4th poll at 15s)
                poll_count = entry.get("_poll_count", 0) + 1
                entry["_poll_count"] = poll_count
                if poll_count % 4 == 1:  # first poll, then every 4th
                    peak_r_logged = entry.get("_max_r", current_r)
                    tag = "↗" if current_r > 0 else "↘"
                    log.info(
                        f"  {tag} {symbol}: R={current_r:+.2f} | "
                        f"peak={peak_r_logged:.2f}R | "
                        f"{elapsed_min:.0f}m since entry"
                    )

                # Track max R seen
                max_r = entry.get("_max_r", current_r)
                if current_r > max_r:
                    entry["_max_r"] = current_r
                    max_r = current_r

                # ═══════════════════════════════════════════════
                # C3 FAKEOUT DETECTION — 100% precision signal
                # ═══════════════════════════════════════════════
                if C3_EXIT and not entry.get("c3_checked", False):
                    # Check at ~5 minutes after entry (C3 should be closed)
                    if elapsed_min >= 5.5:
                        entry["c3_checked"] = True
                        try:
                            candles = exch.fetch_latest_candles(self.ex, symbol, n=2)
                            if candles and len(candles) >= 1:
                                c3 = candles[-1]  # latest closed candle = C3
                                c3_open = c3["open"]
                                c3_close = c3["close"]
                                c3_high = c3["high"]
                                c3_low = c3["low"]
                                c3_body = c3_close - c3_open
                                c3_range = c3_high - c3_low if c3_high > c3_low else 0.0001
                                c3_body_pct = abs(c3_body) / c3_range

                                # Is C3 a reversal candle?
                                is_reversal = False
                                if direction == "long" and c3_body < 0 and c3_body_pct >= C3_REVERSAL_BODY_PCT:
                                    is_reversal = True
                                elif direction == "short" and c3_body > 0 and c3_body_pct >= C3_REVERSAL_BODY_PCT:
                                    is_reversal = True

                                if is_reversal and current_r < C3_MAX_R_TO_EXIT:
                                    # FAKEOUT DETECTED — exit at market
                                    log.info(
                                        f"  ⚡ {symbol}: C3 FAKEOUT DETECTED — "
                                        f"reversal candle (body={c3_body_pct:.0%}) "
                                        f"while R={current_r:+.2f}. CLOSING AT MARKET."
                                    )
                                    log.audit(
                                        "C3_FAKEOUT_EXIT", symbol=symbol,
                                        direction=direction,
                                        current_r=f"{current_r:+.3f}",
                                        c3_body_pct=f"{c3_body_pct:.0%}",
                                    )
                                    tlog.log_c3_check(
                                        symbol=symbol, direction=direction,
                                        current_r=current_r, c3_body_pct=c3_body_pct,
                                        is_reversal=True, action="exit",
                                        session=entry.get("session", ""),
                                        c3_open=c3_open, c3_close=c3_close,
                                        c3_high=c3_high, c3_low=c3_low,
                                        c3_volume=c3.get("volume", 0),
                                        entry_price=entry_price,
                                        peak_r=max_r, elapsed_min=elapsed_min,
                                    )
                                    try:
                                        exch.close_position(self.ex, symbol)
                                        entry["c3_exited"] = True
                                        log.info(f"  ↳ {symbol}: C3 exit executed ✓")
                                    except Exception as ce:
                                        log.error(f"  ↳ {symbol}: C3 exit FAILED — {ce}")
                                elif is_reversal:
                                    log.info(
                                        f"  ⚡ {symbol}: C3 reversal detected but "
                                        f"R={current_r:+.2f} > {C3_MAX_R_TO_EXIT} — holding"
                                    )
                                    tlog.log_c3_check(
                                        symbol=symbol, direction=direction,
                                        current_r=current_r, c3_body_pct=c3_body_pct,
                                        is_reversal=True, action="hold_r_too_high",
                                        session=entry.get("session", ""),
                                        c3_open=c3_open, c3_close=c3_close,
                                        c3_high=c3_high, c3_low=c3_low,
                                        c3_volume=c3.get("volume", 0),
                                        entry_price=entry_price,
                                        peak_r=max_r, elapsed_min=elapsed_min,
                                    )
                                else:
                                    log.info(
                                        f"  ✓ {symbol}: C3 clean — "
                                        f"{'bullish' if c3_body > 0 else 'bearish'} "
                                        f"body={c3_body_pct:.0%}, R={current_r:+.2f}"
                                    )
                                    tlog.log_c3_check(
                                        symbol=symbol, direction=direction,
                                        current_r=current_r, c3_body_pct=c3_body_pct,
                                        is_reversal=False, action="clean",
                                        session=entry.get("session", ""),
                                        c3_open=c3_open, c3_close=c3_close,
                                        c3_high=c3_high, c3_low=c3_low,
                                        c3_volume=c3.get("volume", 0),
                                        entry_price=entry_price,
                                        peak_r=max_r, elapsed_min=elapsed_min,
                                    )
                        except Exception as e:
                            log.debug(f"  {symbol}: C3 check failed — {e}")

                # NOTE: Trailing is handled by Profit Guardian v3 daemon thread
                # (polls every 2s, trails 0.3R behind peak once R>=1.0).
                # No trailing code needed here — guardian operates independently.

                self.state._save()

            except Exception as e:
                log.debug(f"  {symbol}: position check failed — {e}")

    # ═══════════════════════════════════════════════════════════
    #  SKIP MONITOR — Log trades we didn't take
    # ═══════════════════════════════════════════════════════════

    def _log_skipped_trade(self, symbol, session, direction, entry_price,
                           fc, signal, slip_r, reason):
        """Log a skipped trade so we can review consequences later.

        Creates/appends to live/skipped_trades.csv with full context.
        Run the bot's skip-review tool later to see which skips were
        good decisions and which would have been winners.
        """
        write_header = not os.path.exists(SKIP_LOG)
        try:
            with open(SKIP_LOG, "a", newline="") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow([
                        "timestamp_utc", "symbol", "session", "direction",
                        "entry_price", "fc_high", "fc_low", "sl", "tp",
                        "risk_per_unit", "slip_r", "reason",
                    ])
                w.writerow([
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    symbol, session, direction, f"{entry_price:.6f}",
                    f"{fc.high:.6f}", f"{fc.low:.6f}",
                    f"{signal.stop_loss:.6f}", f"{signal.take_profit:.6f}",
                    f"{signal.risk_per_unit:.6f}", f"{slip_r:.4f}", reason,
                ])
            log.info(f"  ↳ Skipped trade logged to {SKIP_LOG}")
        except Exception as e:
            log.warning(f"  ↳ Failed to log skipped trade: {e}")

    # ═══════════════════════════════════════════════════════════
    #  POSITION RESOLUTION & REPORTING
    # ═══════════════════════════════════════════════════════════

    def _resolve_positions(self):
        """Check open positions — have any hit SL/TP and closed?
        Also checks scale-in limit orders: cancel if still pending after base exits."""
        if not self.state.pending_entries:
            return

        log.info(f"Checking {len(self.state.pending_entries)} open position(s)...")

        # Refresh equity before resolution
        try:
            equity = exch.get_equity(self.ex)
            self.state.update_equity(equity)
        except:
            pass

        original_count = len(self.state.pending_entries)
        still_pending = []
        for entry in self.state.pending_entries:
            symbol = entry.get("symbol", "")
            try:
                positions = exch.get_open_positions(self.ex, symbol)
                if positions:
                    # Still open — check if scale-in order filled
                    self._check_scale_fill(entry)
                    still_pending.append(entry)
                    continue

                # Position fully closed — determine outcome
                direction = entry.get("direction", "")
                entry_price = entry.get("entry_price", 0)
                sl_price = entry.get("sl", 0)
                tp_price = entry.get("tp", 0)
                qty = entry.get("qty", 0)
                # Use stored risk_per_unit (original SL distance), NOT
                # guardian-modified SL which may have moved to breakeven.
                risk_per_unit = entry.get("risk_per_unit") or abs(
                    entry_price - entry.get("original_sl", sl_price)
                )

                if risk_per_unit <= 0:
                    log.warning(f"  {symbol}: risk_per_unit=0, cannot resolve — removing from pending")
                    # Don't silently drop — record as a 0R trade so it's tracked
                    self.state.record_outcome(
                        symbol=symbol, session=entry.get("session", ""),
                        direction=direction, entry_price=entry_price,
                        close_price=entry_price, pnl_r=0, pnl_usd=0, is_win=False,
                    )
                    continue

                # Cancel any unfilled scale-in limit order
                self._cancel_scale_order(entry, "base position closed")

                # Try to get actual close price
                # Priority: guardian_close_price > trade history > ticker inference
                close_price = entry.get("guardian_close_price")

                if close_price is None:
                    close_price = self._get_close_price(symbol, direction)

                if close_price is None:
                    try:
                        ticker = exch.get_ticker(self.ex, symbol)
                        last = ticker.get("last", 0)
                        dist_to_tp = abs(last - tp_price)
                        dist_to_sl = abs(last - sl_price)
                        close_price = tp_price if dist_to_tp < dist_to_sl else sl_price
                        log.info(f"  {symbol}: inferred close at {'TP' if dist_to_tp < dist_to_sl else 'SL'}")
                    except:
                        # Don't silently orphan — assume SL hit (worst case)
                        close_price = sl_price
                        log.warning(f"  {symbol}: closed but cannot determine close price — assuming SL @ {close_price}")

                # Calculate PnL — different paths for scale-out vs scale-in vs none
                scale_type = entry.get("scale_type", "in")
                scale_filled = entry.get("scale_status") == "filled"
                scale_qty = entry.get("scale_qty", 0) if scale_filled else 0
                scale_fill_price = entry.get("scale_fill_price") or entry.get("scale_limit_price", 0)

                if scale_filled and scale_type == "out":
                    # ── SCALE-OUT: two legs ──
                    # Leg 1: scale-out portion closed at FC boundary (small loss)
                    # Leg 2: remaining position closed at close_price (TP or BE)
                    remaining_qty = qty - scale_qty

                    # Leg 1: already-closed half
                    if direction == "long":
                        scale_pnl_per_unit = scale_fill_price - entry_price  # negative (FC < entry)
                    else:
                        scale_pnl_per_unit = entry_price - scale_fill_price  # negative (FC > entry)
                    scale_pnl_usd = scale_pnl_per_unit * scale_qty
                    scale_fee = FEE_RATE * (entry_price + scale_fill_price) * scale_qty
                    scale_pnl_usd -= scale_fee
                    scale_pnl_r = scale_pnl_per_unit / risk_per_unit if risk_per_unit > 0 else 0

                    # Leg 2: remaining half hit TP or BE-SL
                    if direction == "long":
                        base_pnl_per_unit = close_price - entry_price
                    else:
                        base_pnl_per_unit = entry_price - close_price
                    base_pnl_usd = base_pnl_per_unit * remaining_qty
                    base_fee = FEE_RATE * (entry_price + close_price) * remaining_qty
                    base_pnl_usd -= base_fee
                    base_pnl_r = base_pnl_per_unit / risk_per_unit if risk_per_unit > 0 else 0

                    # Combine: weighted R (fees deducted from USD, not R, for accuracy)
                    scale_frac = scale_qty / qty if qty > 0 else 0.5
                    remain_frac = remaining_qty / qty if qty > 0 else 0.5
                    pnl_r = scale_pnl_r * scale_frac + base_pnl_r * remain_frac - entry.get("fee_r", 0)
                    net_pnl_usd = scale_pnl_usd + base_pnl_usd

                    log.info(f"  >> {symbol}: SCALE-OUT PnL: "
                             f"scaled half {scale_pnl_r:+.3f}R (${scale_pnl_usd:+.2f}), "
                             f"remaining half {base_pnl_r:+.3f}R (${base_pnl_usd:+.2f})")

                elif scale_filled and scale_type == "in":
                    # ── SCALE-IN (legacy): base + scale portions both close at close_price ──
                    if direction == "long":
                        pnl_per_unit = close_price - entry_price
                    else:
                        pnl_per_unit = entry_price - close_price

                    gross_pnl_usd = pnl_per_unit * qty
                    fee_usd = 2 * FEE_RATE * entry_price * qty
                    net_pnl_usd = gross_pnl_usd - fee_usd
                    pnl_r = pnl_per_unit / risk_per_unit - entry.get("fee_r", 0)

                    scale_entry = entry.get("scale_limit_price", 0)
                    scale_sl = entry.get("scale_sl", 0)
                    scale_risk = abs(scale_entry - scale_sl) if scale_sl else risk_per_unit

                    if direction == "long":
                        scale_pnl_per_unit = close_price - scale_entry
                    else:
                        scale_pnl_per_unit = scale_entry - close_price

                    scale_gross = scale_pnl_per_unit * scale_qty
                    scale_fee = 2 * FEE_RATE * scale_entry * scale_qty
                    scale_pnl_usd = scale_gross - scale_fee
                    scale_pnl_r = scale_pnl_per_unit / scale_risk if scale_risk > 0 else 0
                    net_pnl_usd += scale_pnl_usd
                    log.info(f"  >> {symbol}: SCALE-IN PnL: {scale_pnl_r:+.3f}R (${scale_pnl_usd:+.2f})")

                else:
                    # ── NO SCALE: simple single-position PnL ──
                    if direction == "long":
                        pnl_per_unit = close_price - entry_price
                    else:
                        pnl_per_unit = entry_price - close_price

                    gross_pnl_usd = pnl_per_unit * qty
                    fee_usd = 2 * FEE_RATE * entry_price * qty
                    net_pnl_usd = gross_pnl_usd - fee_usd
                    pnl_r = pnl_per_unit / risk_per_unit - entry.get("fee_r", 0)

                total_pnl_usd = net_pnl_usd
                is_win = total_pnl_usd > 0

                self.state.record_outcome(
                    symbol=symbol,
                    session=entry.get("session", ""),
                    direction=direction,
                    entry_price=entry_price,
                    close_price=close_price,
                    pnl_r=pnl_r,
                    pnl_usd=total_pnl_usd,
                    is_win=is_win,
                )

                # Update pair classification (promotion/demotion)
                self.state.record_pair_outcome(symbol, is_win)

                # ── Dynamic Engine: record outcome for next-trade adaptation ──
                if self.dynamic:
                    self.dynamic.record_outcome(symbol, pnl_r, is_win)

                outcome = "WIN" if is_win else "LOSS"
                scale_note = ""
                if entry.get("scale_status") == "filled":
                    s_type = entry.get("scale_type", "in")
                    scale_note = f" [scale-{s_type}: filled]"
                elif entry.get("scale_status") == "cancelled":
                    scale_note = " [scale: unfilled]"

                # Guardian close reason for trade log
                guardian_note = ""
                if entry.get("guardian_closed"):
                    reason = entry.get("guardian_reason", "guardian")
                    guardian_note = f" [GUARDIAN: {reason}]"
                    peak_r = entry.get("_max_r", 0)
                    log.info(f"  >> {symbol}: Guardian close | peak={peak_r:.2f}R → closed={pnl_r:+.3f}R")

                log.info(f"  >> {symbol}: {outcome} | "
                         f"entry={entry_price:.6f} -> close={close_price:.6f} | "
                         f"PnL={pnl_r:+.3f}R (${net_pnl_usd:+.2f}){scale_note}{guardian_note}")
                log.position_closed(symbol, direction, entry_price, close_price,
                                    pnl_r, total_pnl_usd, outcome + scale_note + guardian_note)

                trade_log.log_exit(
                    symbol=symbol,
                    session=entry.get("session", ""),
                    direction=direction,
                    price=close_price,
                    qty=qty,
                    order_id=entry.get("order_id", ""),
                    equity_after=self.state.equity,
                    notes=f"{outcome}{scale_note}{guardian_note}",
                )

                # ── STRUCTURED TRADE LOG (for analysis + dashboard) ──
                entry_time_str = entry.get("entry_time", "")
                try:
                    if entry_time_str:
                        et = datetime.fromisoformat(entry_time_str)
                        if et.tzinfo is None:
                            et = et.replace(tzinfo=timezone.utc)
                        dur_secs = (datetime.now(timezone.utc) - et).total_seconds()
                    else:
                        dur_secs = 0
                except Exception:
                    dur_secs = 0

                peak_r_val = entry.get("_max_r", 0)
                exit_r_val = pnl_r + entry.get("fee_r", 0)  # gross R (before fees)
                trail_active = bool(entry.get("_trail_active", False))
                gc = bool(entry.get("guardian_closed", False))
                exit_rsn = "c3_fakeout" if entry.get("c3_exited") else (
                    entry.get("guardian_reason", "guardian") if gc else outcome.lower()
                )

                tlog.log_exit(
                    symbol=symbol,
                    session=entry.get("session", ""),
                    direction=direction,
                    entry_price=entry_price,
                    close_price=close_price,
                    pnl_r=pnl_r,
                    pnl_usd=total_pnl_usd,
                    peak_r=peak_r_val,
                    exit_r=exit_r_val,
                    duration_secs=dur_secs,
                    equity_after=self.state.equity,
                    exit_reason=exit_rsn,
                    guardian_closed=gc,
                    trail_was_active=trail_active,
                    pair_class=entry.get("pair_class", ""),
                    fc_range_pct=entry.get("_fc_range_pct", 0),
                    slip_r=entry.get("_slip_r", 0),
                    # enriched lifecycle fields
                    qty=entry.get("qty", 0),
                    risk_per_unit=entry.get("risk_per_unit", 0),
                    fee_r=entry.get("fee_r", 0),
                    risk_pct=entry.get("risk_pct", 0),
                    original_sl=entry.get("original_sl", 0),
                    final_sl=entry.get("sl", 0),
                    peak_price=entry.get("_peak_price", entry.get("peak_price", 0)),
                    entry_time=entry_time_str,
                    c3_exited=bool(entry.get("c3_exited", False)),
                    c3_checked=bool(entry.get("c3_checked", False)),
                    guardian_tier=entry.get("_guardian_tier", -1),
                    guardian_polls=entry.get("_guardian_polls", 0),
                    total_trades=self.state.total_trades,
                    cumulative_r=self.state.total_pnl_r,
                    open_positions=len(still_pending),
                    day_of_week=datetime.now(timezone.utc).weekday(),
                )

            except Exception as e:
                log.error(f"  {symbol}: error resolving — {e}")
                log.debug(traceback.format_exc())
                still_pending.append(entry)

        self.state.pending_entries = still_pending
        self.state._save()

        resolved = original_count - len(still_pending)
        if resolved > 0:
            log.info(f"{resolved} position(s) closed (SL/TP hit). {len(still_pending)} still open.")

    def _check_scale_fill(self, entry: dict):
        """Check if a pending scale order (in or out) has been filled.
        
        For scale-IN: position size increases when filled.
        For scale-OUT: position size decreases when filled (reduce-only).
        
        The get_order_status function returns:
          - {"status": "open"} if order is still in open orders
          - {"status": "gone"} if disappeared (filled or cancelled)
          - {"status": "closed"/"filled"} if fetch_order worked
          - {} if all checks failed
        """
        if entry.get("scale_status") != "pending" or not entry.get("scale_order_id"):
            return

        symbol = entry.get("symbol", "")
        scale_id = entry["scale_order_id"]
        scale_type = entry.get("scale_type", "in")  # "in" or "out"

        try:
            order_info = exch.get_order_status(self.ex, scale_id, symbol)
            status = order_info.get("status", "").lower()

            if status == "open":
                return  # Still pending, nothing to do

            if status in ("closed", "filled"):
                self._handle_scale_filled(entry, order_info)
                return

            if status == "gone":
                # Order disappeared — determine if it filled or was cancelled.
                positions = exch.get_open_positions(self.ex, symbol)
                if positions:
                    pos = positions[0]
                    pos_qty = abs(float(pos.get("contracts", 0) or 0))
                    base_qty = entry.get("qty", 0)
                    scale_qty = entry.get("scale_qty", 0)

                    if scale_type == "out":
                        # Scale-out: position should be SMALLER than base
                        expected_reduced = base_qty - scale_qty
                        if pos_qty <= expected_reduced * 1.05:
                            log.info(f"  ↳ {symbol}: scale-out FILLED "
                                     f"(pos size {pos_qty} ≈ base-scale {expected_reduced})")
                            self._handle_scale_filled(entry, order_info)
                            return
                    else:
                        # Scale-in: position should be BIGGER than base
                        expected_merged = base_qty + scale_qty
                        if pos_qty >= expected_merged * 0.95:
                            log.info(f"  ↳ {symbol}: scale-in FILLED "
                                     f"(pos size {pos_qty} ≈ base+scale {expected_merged})")
                            self._handle_scale_filled(entry, order_info)
                            return

                # Position is unchanged or gone → scale was cancelled/expired
                entry["scale_status"] = "cancelled"
                log.info(f"  ↳ {symbol}: scale-{scale_type} gone from open orders (cancelled/expired)")
                self.state._save()
                return

            if status in ("canceled", "cancelled", "expired", "rejected"):
                entry["scale_status"] = "cancelled"
                log.info(f"  ↳ {symbol}: scale-{scale_type} {status}")
                self.state._save()
            # else: unknown status, do nothing

        except Exception as e:
            log.debug(f"  ↳ {symbol}: could not check scale order: {e}")

    def _handle_scale_filled(self, entry: dict, order_info: dict):
        """Handle a confirmed scale fill — either scale-in or scale-out.
        
        Scale-OUT: 50% of position was closed at FC boundary.
                   Move SL to breakeven on the remaining 50% so if SL hits
                   the net loss is only the small loss on the closed half.
        Scale-IN:  Position merged, correct TP if needed (legacy).
        """
        symbol = entry.get("symbol", "")
        scale_id = entry.get("scale_order_id", "")
        scale_type = entry.get("scale_type", "in")
        direction = entry.get("direction", "")

        entry["scale_status"] = "filled"
        fill_price = order_info.get("average") or order_info.get("price") or entry.get("scale_limit_price")
        if fill_price:
            entry["scale_fill_price"] = float(fill_price)

        if scale_type == "out":
            # ── SCALE-OUT FILLED: move SL to breakeven ──
            log.info(f"  ↳ {symbol}: SCALE-OUT FILLED @ {fill_price} — "
                     f"closed {SCALE_OUT_PCT*100:.0f}% of position")

            base_tp = entry.get("tp", 0)

            # Move SL to breakeven (entry price) on remaining position.
            # This means if price continues to SL, remaining half loses $0.
            # Net trade result = only the small loss on the closed half.
            be_price = exch.round_price(self.ex, symbol, entry.get("entry_price", 0))

            if be_price:
                log.info(f"  ↳ {symbol}: moving SL to breakeven @ {be_price} "
                         f"(was {entry.get('sl', '?')})")
                for attempt in range(3):
                    pos = exch.get_open_positions(self.ex, symbol)
                    if not pos:
                        log.info(f"  ↳ {symbol}: position already closed — skipping BE move")
                        break
                    ok = exch.set_trading_stop(
                        self.ex, symbol,
                        side=direction,
                        sl_price=be_price,
                        tp_price=base_tp if base_tp else None,
                    )
                    if ok:
                        entry["sl"] = be_price  # update stored SL
                        log.info(f"  ↳ {symbol}: SL moved to breakeven ✓")
                        break
                    log.warning(f"  ↳ {symbol}: BE move attempt {attempt+1}/3 failed")
                    time.sleep(1)
                else:
                    pos = exch.get_open_positions(self.ex, symbol)
                    if pos:
                        log.error(f"  ↳ {symbol}: FAILED to move SL to breakeven after scale-out!")
                    else:
                        log.info(f"  ↳ {symbol}: position closed during BE move — no action needed")

        else:
            # ── SCALE-IN FILLED: correct TP (legacy) ──
            log.info(f"  ↳ {symbol}: scale-in FILLED @ {fill_price}")
            log.scale_in_event(symbol, "FILLED",
                               order_id=scale_id, fill_price=str(fill_price))

            base_tp = entry.get("tp", 0)
            scale_tp = entry.get("scale_tp", 0)

            if base_tp and scale_tp and base_tp != scale_tp:
                if direction == "long":
                    correct_tp = max(base_tp, scale_tp)
                else:
                    correct_tp = min(base_tp, scale_tp)

                base_sl = entry.get("sl", 0)

                log.info(f"  ↳ {symbol}: correcting merged position TP to "
                         f"{correct_tp} (base_tp={base_tp}, scale_tp={scale_tp})")

                for attempt in range(3):
                    pos = exch.get_open_positions(self.ex, symbol)
                    if not pos:
                        log.info(f"  ↳ {symbol}: position already closed "
                                 f"(TP/SL hit during scale-in) — skipping TP correction")
                        break

                    ok = exch.set_trading_stop(
                        self.ex, symbol,
                        side=direction,
                        sl_price=base_sl if base_sl else None,
                        tp_price=correct_tp,
                    )
                    if ok:
                        break
                    log.warning(f"  ↳ {symbol}: TP correction attempt {attempt+1}/3 failed")
                    time.sleep(1)
                else:
                    pos = exch.get_open_positions(self.ex, symbol)
                    if pos:
                        log.critical(f"  ↳ {symbol}: FAILED to correct TP after scale-in fill! "
                                     f"Position has wrong TP={scale_tp}, should be {correct_tp}")
                    else:
                        log.info(f"  ↳ {symbol}: position closed during TP correction — no action needed")
            elif base_tp and scale_tp:
                log.info(f"  ↳ {symbol}: scale-in TP matches base TP ({base_tp}) — no correction needed")

        self.state._save()

    def _cancel_scale_order(self, entry: dict, reason: str = ""):
        """Cancel a pending scale-in or scale-out limit order."""
        if entry.get("scale_status") != "pending" or not entry.get("scale_order_id"):
            return

        symbol = entry.get("symbol", "")
        scale_id = entry["scale_order_id"]
        scale_label = "scale-" + entry.get("scale_type", "in")

        # Check if it filled first
        self._check_scale_fill(entry)
        if entry["scale_status"] == "filled":
            return  # Already filled, nothing to cancel

        success = exch.cancel_order(self.ex, scale_id, symbol)
        if success:
            # Re-check status — cancel_order returns True for "already filled"
            # errors too. Must verify the actual state.
            self._check_scale_fill(entry)
            if entry["scale_status"] != "filled":
                entry["scale_status"] = "cancelled"
                log.info(f"  ↳ {symbol}: {scale_label} cancelled ({reason})")
        else:
            # Cancel failed — re-check, it may have just filled
            self._check_scale_fill(entry)
        self.state._save()

    def _cancel_session_scale_orders(self, session: str):
        """Cancel all unfilled scale-in/out orders from a session.
        Called when session ends to clean up stale limit orders."""
        cancelled = 0
        for entry in self.state.pending_entries:
            if entry.get("session") == session and entry.get("scale_status") == "pending":
                self._cancel_scale_order(entry, f"session {session} ended")
                cancelled += 1
        if cancelled:
            log.info(f"Cancelled {cancelled} unfilled scale order(s) from {session}")

    def _get_close_price(self, symbol: str, direction: str):
        """Try to get the close price of a resolved position from trade history."""
        try:
            trades = self.ex.fetch_my_trades(
                symbol, limit=20, params={"category": "linear"}
            )
            # The close trade is in the opposite direction
            close_side = "sell" if direction == "long" else "buy"
            for t in reversed(trades):
                if t.get("side", "").lower() == close_side:
                    return float(t["price"])
        except Exception as e:
            log.debug(f"Could not fetch trades for {symbol}: {e}")
        return None

    def _print_session_report(self, session: str):
        """Print a detailed performance report after processing a session.

        Includes: per-trade breakdown with WHY each won/lost, class info,
        scale-in status, session totals, equity, and x1000 projection.
        """
        # Refresh equity
        try:
            equity = exch.get_equity(self.ex)
            self.state.update_equity(equity)
        except:
            equity = self.state.equity

        s = self.state.daily_summary()

        log.info("━" * 60)
        log.info(f"  SESSION {session.upper()} REPORT — {s['date']}")
        log.info("━" * 60)

        # Per-trade detail from recent trade history
        today_trades = [t for t in self.state.trade_history
                        if t.get("timestamp", "").startswith(s["date"])]
        if today_trades:
            for t in today_trades:
                sym = t.get("symbol", "?")
                d = t.get("direction", "?")
                outcome = t.get("outcome", "?")
                pnl_r = t.get("pnl_r", 0)
                pnl_usd = t.get("pnl_usd", 0)
                cls = self.state.get_pair_class(sym)

                # Determine WHY it won or lost
                if outcome == "WIN":
                    if pnl_r > 1.6:  # > 1.5R means guardian trail captured a runner
                        why = f"RUNNER — trailed to {pnl_r:+.2f}R (${pnl_usd:+.2f})"
                    else:
                        why = f"hit TP at {pnl_r:+.2f}R (${pnl_usd:+.2f})"
                else:
                    why = f"hit SL at midpoint (${pnl_usd:+.2f})"

                log.info(f"    {sym:<26} {d:>5} → {outcome:<4} "
                         f"({pnl_r:+.3f}R) [Class {cls}] — {why}")
        else:
            log.info("    (no resolved trades this session)")

        # Session summary line
        log.info(f"  ─── Summary ───")
        log.info(f"  Equity: ${equity:.2f}  "
                 f"(Day start: ${s['start_equity']:.2f})")
        log.info(f"  Today: {s['entries_today']} entries | "
                 f"{s['wins']}W / {s['losses']}L | "
                 f"PnL: ${s['pnl_usd']:+.2f} ({s['pnl_pct']:+.1f}%)")
        log.info(f"  Pending: {s['pending']} position(s) still open")

        # All-time with x10 projection
        total = s['total_trades']
        wr = s['all_time_wr']
        total_r = s['total_pnl_r']
        log.info(f"  All-time: {total} trades | "
                 f"{s['total_wins']}W / {s['total_losses']}L | "
                 f"WR={wr:.1f}% | {total_r:+.2f}R")

        # x10 projection
        if total > 0 and total_r > 0 and s['start_equity'] > 0:
            import math
            growth = equity / s['start_equity'] if s['start_equity'] > 0 else 1
            if growth > 1:
                log_growth_per_day = math.log(growth)
                days_to_10x = math.log(10) / log_growth_per_day if log_growth_per_day > 0 else 9999
                log.info(f"  x10 projection: ~{days_to_10x:.0f} days at today's pace")

        log.info("━" * 60)
        log.session_end(session, equity,
                        s['entries_today'], s['wins'], s['losses'])
        # Structured JSONL session close
        tlog.log_session_close(
            session=session, equity=equity,
            entries=s['entries_today'], wins=s['wins'], losses=s['losses'],
            pnl_r=s.get('total_pnl_r', 0), pnl_usd=s.get('pnl_usd', 0),
            pending_positions=s['pending'],
            total_trades=s['total_trades'],
            skips=0,  # TODO: track session skip count
        )

        # ── x10 Growth Tracker ──
        try:
            _growth_session_end(
                equity, session,
                s['entries_today'], s['wins'], s['losses'],
                s.get('total_pnl_r', 0),
            )
            alert = _growth_pace_alert(equity)
            if alert:
                log.warning(f"  GROWTH ALERT: {alert}")
        except Exception as e:
            log.warning(f"Growth tracker error: {e}")

        # ── Learning Agent: automated session review ──
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            sess_exits = _get_session_exits(session, today)
            if sess_exits:
                _journal_review(session, sess_exits, equity)
        except Exception as exc:
            log.warning(f"[JOURNAL] Session review failed: {exc}")

    def _print_daily_report(self):
        """Print full daily report after NY session.

        Includes performance metrics, win/loss analysis, x1000 projection,
        pair class standings, and key insights.
        """
        # Refresh equity
        try:
            equity = exch.get_equity(self.ex)
            self.state.update_equity(equity)
        except:
            equity = self.state.equity

        s = self.state.daily_summary()
        log.info("=" * 60)
        log.info(f"  DAILY REPORT — {s['date']}")
        log.info("=" * 60)
        log.info(f"  Start Equity:   ${s['start_equity']:.2f}")
        log.info(f"  Current Equity: ${equity:.2f}")
        pnl_usd = equity - s['start_equity'] if s['start_equity'] > 0 else s['pnl_usd']
        pnl_pct = (equity / s['start_equity'] - 1) * 100 if s['start_equity'] > 0 else 0
        log.info(f"  Day PnL:        ${pnl_usd:+.2f} ({pnl_pct:+.2f}%)")
        log.info(f"  Entries:        {s['entries_today']}")
        log.info(f"  Resolved:       {s['resolved_today']} "
                 f"({s['wins']}W / {s['losses']}L)")
        wr_today = s['win_rate']
        log.info(f"  Win Rate Today: {wr_today:.1f}%")
        log.info(f"  Pending:        {s['pending']} still open")

        # Day's trade log with reasons
        today_trades = [t for t in self.state.trade_history
                        if t.get("timestamp", "").startswith(s["date"])]
        if today_trades:
            log.info(f"  ─── Trade Log ───")
            for t in today_trades:
                sym = t.get("symbol", "?")
                d = t.get("direction", "?")
                outcome = t.get("outcome", "?")
                pr = t.get("pnl_r", 0)
                pu = t.get("pnl_usd", 0)
                cls = self.state.get_pair_class(sym)
                if outcome == "WIN":
                    why = f"RUNNER {pr:+.2f}R" if pr > 1.6 else "TP hit"
                else:
                    why = "SL hit"
                log.info(f"    {sym:<26} {d:>5} {outcome:<4} "
                         f"{pr:+.3f}R (${pu:+.2f}) [{cls}] {why}")

        log.info("-" * 40)
        log.info(f"  ALL-TIME TOTALS")
        log.info(f"  Total Trades:   {s['total_trades']}")
        log.info(f"  Total W/L:      {s['total_wins']}W / {s['total_losses']}L")
        log.info(f"  All-time WR:    {s['all_time_wr']:.1f}%")
        log.info(f"  Total PnL:      {s['total_pnl_r']:+.2f}R")

        # x1000 projection based on all-time performance
        total = s['total_trades']
        if total >= 5 and s['total_pnl_r'] > 0:
            import math
            # Estimate avg R per trade and trades per day
            avg_r = s['total_pnl_r'] / total
            # Approximate log growth: risk * R * leverage per trade
            # Using effective growth
            log.info(f"  Avg R/trade:    {avg_r:+.3f}R")

        # Pair class standings
        class_summary = self.state.pair_class_summary()
        log.info(f"  {class_summary}")

        log.info("=" * 60)

    def _check_equity_floor(self):
        """At NY close, check if equity dropped below floor. If floor=0, skip."""
        try:
            equity = exch.get_equity(self.ex)
            self.state.update_equity(equity)
        except:
            equity = self.state.equity

        if EQUITY_FLOOR <= 0:
            log.info(f"Equity floor disabled — equity ${equity:.2f} (no floor)")
            return

        if equity < EQUITY_FLOOR:
            log.critical("=" * 60)
            log.critical(f"  EQUITY FLOOR BREACHED: ${equity:.2f} < ${EQUITY_FLOOR:.2f}")
            log.critical(f"  Bot will stop trading.")
            log.critical(f"  Fund account above ${EQUITY_FLOOR:.0f} and reset "
                         f"'equity_floor_hit' in state.json to resume.")
            log.critical("=" * 60)
            self.state.equity_floor_hit = True
            self.state._save()
            self._running = False
        else:
            log.info(f"Equity floor check: ${equity:.2f} >= ${EQUITY_FLOOR:.2f} — OK")

    def _monitor_and_sleep(self, session: str):
        """Monitor pending positions for the remainder of the session, then sleep.

        Polls every 15s.  Each iteration:
          - Check if any scale-in limit orders have filled
          - Resolve any base positions that closed (SL/TP hit)
          - Cancel remaining scale orders when session ends

        Profit Guardian v3 handles trailing in its own daemon thread (every 2s).

        Continues monitoring while ANY pending positions exist for this session,
        not just while scale orders are pending.
        """
        end_h = SESSIONS[session][1]

        session_entries = [
            e for e in self.state.pending_entries
            if e.get("session") == session
        ]

        if not session_entries:
            log.info("No open positions this session — sleeping until next session.")
            # Still log going-to-sleep with any positions from prior sessions
            if self.state.pending_entries:
                pending_syms = [e.get("symbol", "?") for e in self.state.pending_entries]
                log.info(f"Prior session position(s) still live "
                         f"(exchange SL/TP managing): {', '.join(pending_syms)}")
            # Show performance tracker before sleeping
            try:
                eq = exch.get_equity(self.ex)
            except Exception:
                eq = self.state.equity
            _startup_report(eq)
            self._sleep_until_next_session()
            return

        _write_activity("MONITORING", f"{len(session_entries)} open position(s)",
                        session=session, positions=len(session_entries))
        log.info(f"Monitoring {len(session_entries)} open position(s) for session {session}...")

        while self._running:
            now = datetime.now(timezone.utc)
            current_h = now.hour

            # Check if session has ended
            session_over = False
            if end_h == 24:
                session_over = (current_h < SESSIONS[session][0])
            else:
                session_over = (current_h >= end_h)

            if session_over:
                # Session ended — cancel remaining unfilled scale-in orders
                self._cancel_session_scale_orders(session)
                # Resolve any positions that may have closed
                self._resolve_positions()
                break

            # NOTE: Profit Guardian v3 handles trailing SL in its
            # own daemon thread (every 2s). But C3 fakeout detection
            # runs here since it uses candle data, not price polling.
            if C3_EXIT:
                self._trail_positions()

            # Check scale-in fills for all pending entries with pending scales
            pending_scales = [
                e for e in self.state.pending_entries
                if e.get("scale_status") == "pending" and e.get("session") == session
            ]

            for entry in pending_scales:
                self._check_scale_fill(entry)

            # Resolve any base positions that have closed
            self._resolve_positions()

            # Check if ALL session positions have closed
            remaining = [
                e for e in self.state.pending_entries
                if e.get("session") == session
            ]
            if not remaining:
                log.info(f"All {session} positions closed (SL/TP hit).")
                break

            # Heartbeat for monitoring
            log.heartbeat(self.state.equity, len(self.state.pending_entries), session)
            # Structured JSONL heartbeat — equity curve reconstruction
            _pos_details = []
            for _pe in self.state.pending_entries:
                _sym = _pe.get("symbol", "")
                _mr = _pe.get("_max_r", 0)
                _pos_details.append({"s": _sym, "r": round(_mr, 3)})
            tlog.log_heartbeat(
                equity=self.state.equity,
                pending=len(self.state.pending_entries),
                session=session,
                total_trades=self.state.total_trades,
                total_pnl_r=self.state.total_pnl_r,
                wins_today=self.state.wins_today,
                losses_today=self.state.losses_today,
                position_details=_pos_details if _pos_details else None,
            )

            # Poll every 15s for scale-in checks & resolution.
            # Profit Guardian v2 handles real-time monitoring at 2s.
            time.sleep(15)

        # Now sleep until next session
        # Log open positions before sleeping
        if self.state.pending_entries:
            open_syms = [e.get("symbol", "?") for e in self.state.pending_entries]
            log.info(f"Going to sleep with {len(open_syms)} LIVE position(s) "
                     f"(exchange SL/TP managing): {', '.join(open_syms)}")
        # Show performance tracker before sleeping
        try:
            eq = exch.get_equity(self.ex)
        except Exception:
            eq = self.state.equity
        _startup_report(eq)
        self._sleep_until_next_session()

    def _sleep_until_next_session(self):
        """Sleep until 30 seconds before the next session opens.
        
        Shows a live countdown so the user knows the bot is alive.
        Updates every 60 seconds with time remaining.
        """
        name, when = next_session_start()
        now = datetime.now(timezone.utc)
        wait = (when - now).total_seconds() - 30  # wake 30s early

        if wait <= 0:
            # Next session is imminent or started
            time.sleep(5)
            return

        _write_activity("SLEEPING", f"Until {name} at {when.strftime('%H:%M')} UTC",
                        next_session=name,
                        next_session_time=when.isoformat(),
                        positions=len(self.state.pending_entries))
        log.info(f"Sleeping until {name} session at {when.strftime('%H:%M')} UTC "
                 f"({wait/60:.0f} min)...")

        # Live countdown — update every 60s so user sees bot is alive
        import sys as _sys
        while wait > 0 and self._running:
            mins_left = int(wait // 60)
            secs_left = int(wait % 60)
            hrs = mins_left // 60
            mins = mins_left % 60

            if hrs > 0:
                countdown = f"{hrs}h {mins:02d}m"
            else:
                countdown = f"{mins}m {secs_left:02d}s"

            now_str = datetime.now(timezone.utc).strftime('%H:%M:%S')
            _sys.stdout.write(
                f"\r  ⏳ {now_str} UTC │ {name} session in {countdown} "
                f"│ opens {when.strftime('%H:%M')} UTC   "
            )
            _sys.stdout.flush()

            # Sleep 60s (or less if close to wake time)
            chunk = min(60, wait)
            time.sleep(chunk)
            wait -= chunk

            # Re-sync with real clock every 5 minutes (safety against drift)
            if int(wait) % 300 < 60:
                name, when = next_session_start()
                now = datetime.now(timezone.utc)
                wait = (when - now).total_seconds() - 30
                if wait <= 0:
                    break

        # Clear the countdown line
        _sys.stdout.write("\r" + " " * 80 + "\r")
        _sys.stdout.flush()

        # ── WAKE-UP LOGGING ──
        now = datetime.now(timezone.utc)
        sess = current_session()
        log.info(f"━━━ WOKE UP at {now.strftime('%H:%M:%S')} UTC — "
                 f"target session: {name} | current session: {sess or 'between'} ━━━")
        # Quick equity check
        try:
            equity = exch.get_equity(self.ex)
            self.state.update_equity(equity)
            log.info(f"Wake-up equity: ${equity:.2f} | "
                     f"Pending: {len(self.state.pending_entries)} position(s)")
        except Exception as e:
            log.warning(f"Wake-up equity check failed: {e}")

    def status(self) -> str:
        """Return a human-readable status string."""
        sess = current_session() or "none"
        return (
            f"Session: {sess} | "
            f"Equity: ${self.state.equity:.2f} | "
            f"Total trades: {self.state.total_trades} | "
            f"Today: {sum(self.state.daily_counts.values())} trades"
        )
