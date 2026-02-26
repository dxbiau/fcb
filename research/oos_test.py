"""
research/oos_test.py — OUT-OF-SAMPLE Validation

Tests the FCB-TP1R strategy on 35 Binance futures pairs that were NEVER
used in any optimization, parameter tuning, or pair selection.

This is the acid test: if the strategy works here, the edge is REAL.
If it fails, we curve-fitted to the Bybit data.

Uses exact live params:
  - FCB entry: FC capture → C2 breakout → Entry at C2 close (no retest)
  - Also tests WITH retest for comparison
  - Fixed TP at 1.0R, 1.5R
  - Risk: 2%, 3%
  - Max 2 concurrent, 1 per pair per session
  - 0.04R fee per trade
  - Start: $50

Data source: binance_futures_*_5m.csv (35 pairs never used in optimization)
"""

from __future__ import annotations
import csv, glob, math, os, sys, time, statistics, random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT))

from research.session_sim import (
    build_time_index, get_candle_at, get_candle_after,
    Candle, OpenTrade, SESSIONS, SESSION_ORDER, monte_carlo,
)


# ═══════════════════════════════════════════════
#  DATA LOADING — Binance format
# ═══════════════════════════════════════════════

def load_binance_pairs() -> Dict[str, List[Candle]]:
    """Load all binance_futures_*_5m.csv files."""
    pattern = str(DATA_DIR / "binance_futures_*_5m.csv")
    pair_data = {}

    for fpath in sorted(glob.glob(pattern)):
        base = os.path.basename(fpath).replace(".csv", "")
        # binance_futures_AAVE_USDT_5m -> AAVE
        parts = base.replace("binance_futures_", "").replace("_5m", "")
        # parts = "AAVE_USDT" -> symbol = "AAVE"
        symbol = parts.replace("_USDT", "")
        pair = f"{symbol}/USDT:USDT"

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
                o, h, l, c, v = (
                    float(row["open"]), float(row["high"]),
                    float(row["low"]), float(row["close"]),
                    float(row["volume"]),
                )
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


def load_bybit_pairs_excluding(exclude_set: set) -> Dict[str, List[Candle]]:
    """Load bybit 5m pairs, EXCLUDING those in exclude_set."""
    from research.session_sim import load_all_pairs
    all_pairs = load_all_pairs()
    return {p: c for p, c in all_pairs.items() if p not in exclude_set}


# ═══════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════

@dataclass
class OOSConfig:
    name: str = ""
    tp_r: float = 1.0
    be_trigger_r: float = 0.0
    require_retest: bool = False
    risk_pct: float = 0.02
    max_concurrent: int = 2
    fc_counter: bool = True
    min_c2_body: float = 0.50
    vol_ratio_long: float = 1.0
    vol_ratio_short: float = 0.25
    min_range_pct: float = 0.003
    fee_per_trade_r: float = 0.04
    breakout_window: int = 12  # 60min / 5min
    max_per_session: int = 1
    max_per_day: int = 6
    start_equity: float = 50.0


# ═══════════════════════════════════════════════
#  TRADE MANAGEMENT
# ═══════════════════════════════════════════════

def _manage_trade(candle: Candle, trade: OpenTrade, cfg: OOSConfig):
    """Fixed TP + optional BE."""
    h, l = candle.h, candle.l
    risk = trade.risk_per_unit

    if trade.direction == "long":
        # SL check
        if l <= trade.stop_loss:
            reason = "be" if (cfg.be_trigger_r > 0 and trade.trail_active) else "sl"
            trade.close(trade.stop_loss, candle.dt, reason)
            return

        current_r = (h - trade.entry_price) / risk if risk > 0 else 0

        # BE move
        if cfg.be_trigger_r > 0 and not trade.trail_active:
            if current_r >= cfg.be_trigger_r:
                trade.trail_active = True
                trade.stop_loss = trade.entry_price

        # TP check
        tp_price = trade.entry_price + cfg.tp_r * risk
        if h >= tp_price:
            trade.close(tp_price, candle.dt, "tp")
            return

    else:  # SHORT
        if h >= trade.stop_loss:
            reason = "be" if (cfg.be_trigger_r > 0 and trade.trail_active) else "sl"
            trade.close(trade.stop_loss, candle.dt, reason)
            return

        current_r = (trade.entry_price - l) / risk if risk > 0 else 0

        if cfg.be_trigger_r > 0 and not trade.trail_active:
            if current_r >= cfg.be_trigger_r:
                trade.trail_active = True
                trade.stop_loss = trade.entry_price

        tp_price = trade.entry_price - cfg.tp_r * risk
        if l <= tp_price:
            trade.close(tp_price, candle.dt, "tp")
            return


# ═══════════════════════════════════════════════
#  SIMULATION ENGINE
# ═══════════════════════════════════════════════

def simulate_oos(pair_data: Dict[str, List[Candle]], cfg: OOSConfig) -> dict:
    """
    Session-faithful simulation.

    IMPORTANT: No pair priority ordering here — pairs are shuffled randomly
    each session to avoid any ordering bias. This simulates real-world conditions
    where you don't know which pair will fire first.
    """
    time_idx = build_time_index(pair_data)

    all_dts = []
    for candles in pair_data.values():
        if candles:
            all_dts.extend([candles[0].dt, candles[-1].dt])
    if not all_dts:
        return _empty(cfg)

    start_date = min(all_dts).replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = max(all_dts).replace(hour=0, minute=0, second=0, microsecond=0)

    pair_list = list(pair_data.keys())

    # State
    equity = cfg.start_equity
    peak_eq = cfg.start_equity
    max_dd = 0.0
    open_positions: Dict[str, OpenTrade] = {}
    closed_trades: List[OpenTrade] = []

    session_entries: Dict[str, Set[str]] = {}
    daily_counts: Dict[str, int] = {}

    session_stats = {s: {"trades": 0, "wins": 0, "total_r": 0.0} for s in SESSION_ORDER}
    exit_reasons: Dict[str, int] = {}

    current_day = start_date
    while current_day <= end_date:
        session_entries.clear()
        daily_counts.clear()

        for sess_name in SESSION_ORDER:
            sh, eh = SESSIONS[sess_name]
            session_entries[sess_name] = set()

            sess_start = current_day.replace(hour=sh, minute=0, second=0, microsecond=0)

            # Capture FCs
            pair_fcs: Dict[str, Candle] = {}
            for pair in pair_list:
                fc = get_candle_at(pair, sess_start, pair_data, time_idx)
                if fc is None:
                    continue
                mid = (fc.h + fc.l) / 2
                if mid <= 0:
                    continue
                if (fc.h - fc.l) / mid < cfg.min_range_pct:
                    continue
                pair_fcs[pair] = fc

            # Shuffle pair order each session (no priority bias!)
            session_pairs = list(pair_fcs.keys())
            random.shuffle(session_pairs)

            n_candles = (eh - sh) * 12
            for c_idx in range(n_candles):
                ct = sess_start + timedelta(minutes=c_idx * 5)

                # Manage open trades
                for pair in list(open_positions.keys()):
                    trade = open_positions[pair]
                    candle = get_candle_at(pair, ct, pair_data, time_idx)
                    if candle is None:
                        continue

                    _manage_trade(candle, trade, cfg)

                    if not trade.is_open:
                        if cfg.fee_per_trade_r > 0:
                            trade.r_multiple -= cfg.fee_per_trade_r
                        pnl = trade.dollar_risk * trade.r_multiple
                        equity += pnl
                        equity = max(equity, 0.01)
                        if equity > peak_eq:
                            peak_eq = equity
                        dd = (peak_eq - equity) / peak_eq if peak_eq > 0 else 0
                        max_dd = max(max_dd, dd)
                        closed_trades.append(trade)
                        del open_positions[pair]

                        session_stats[sess_name]["trades"] += 1
                        session_stats[sess_name]["total_r"] += trade.r_multiple
                        if trade.r_multiple > 0:
                            session_stats[sess_name]["wins"] += 1
                        er = trade.exit_reason or "unknown"
                        exit_reasons[er] = exit_reasons.get(er, 0) + 1

                # Entries
                if c_idx < 1 or c_idx >= cfg.breakout_window:
                    continue

                for pair in session_pairs:
                    if pair not in pair_fcs:
                        continue
                    fc = pair_fcs[pair]

                    if pair in session_entries[sess_name]:
                        continue
                    if daily_counts.get(pair, 0) >= cfg.max_per_day:
                        continue
                    if pair in open_positions:
                        continue
                    if len(open_positions) >= cfg.max_concurrent:
                        break

                    c2 = get_candle_at(pair, ct, pair_data, time_idx)
                    if c2 is None:
                        continue

                    bo_dir = None
                    if c2.c > fc.h:
                        bo_dir = "long"
                    elif c2.c < fc.l:
                        bo_dir = "short"
                    if bo_dir is None:
                        continue

                    # Micro-filters
                    if cfg.min_c2_body > 0 and c2.body_ratio < cfg.min_c2_body:
                        continue
                    vr = c2.v / fc.v if fc.v > 0 else 1.0
                    if bo_dir == "long" and cfg.vol_ratio_long > 0 and vr < cfg.vol_ratio_long:
                        continue
                    if bo_dir == "short" and cfg.vol_ratio_short > 0 and vr < cfg.vol_ratio_short:
                        continue

                    # FC counter filter
                    if cfg.fc_counter:
                        if bo_dir == "long" and fc.candle_dir > 0:
                            continue
                        if bo_dir == "short" and fc.candle_dir < 0:
                            continue

                    # Retest gate
                    if cfg.require_retest:
                        c3 = get_candle_after(pair, ct, pair_data, time_idx, offset=1)
                        if c3 is None:
                            continue
                        if bo_dir == "long":
                            if not (c3.l <= fc.h and c3.c > fc.h):
                                continue
                        else:
                            if not (c3.h >= fc.l and c3.c < fc.l):
                                continue
                        entry_price = c3.c
                        entry_time = c3.dt
                    else:
                        entry_price = c2.c
                        entry_time = c2.dt

                    direction = bo_dir
                    sl = (fc.h + fc.l) / 2
                    risk_per_unit = abs(entry_price - sl)

                    if risk_per_unit <= 0:
                        continue
                    if risk_per_unit / entry_price < 0.0005:
                        continue
                    if len(open_positions) >= cfg.max_concurrent:
                        break
                    if equity < 2.0:
                        break

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
                    session_entries[sess_name].add(pair)
                    daily_counts[pair] = daily_counts.get(pair, 0) + 1

        current_day += timedelta(days=1)

    # Close remaining
    for pair, trade in list(open_positions.items()):
        if pair in pair_data and pair_data[pair]:
            last = pair_data[pair][-1]
            trade.close(last.c, last.dt, "data_end")
            if cfg.fee_per_trade_r > 0:
                trade.r_multiple -= cfg.fee_per_trade_r
            pnl = trade.dollar_risk * trade.r_multiple
            equity += pnl
            equity = max(equity, 0.01)
            closed_trades.append(trade)

    return _compute(closed_trades, equity, peak_eq, max_dd,
                    session_stats, exit_reasons, cfg)


def _compute(closed_trades, final_eq, peak_eq, max_dd,
             session_stats, exit_reasons, cfg) -> dict:
    r_vals = [t.r_multiple for t in closed_trades if t.r_multiple is not None]
    winners = [r for r in r_vals if r > 0]
    losers = [r for r in r_vals if r <= 0]

    x10 = None
    eq = cfg.start_equity
    for i, t in enumerate(closed_trades):
        if t.r_multiple is None:
            continue
        eq += t.dollar_risk * t.r_multiple
        eq = max(eq, 0.01)
        if x10 is None and eq >= cfg.start_equity * 10:
            x10 = i + 1

    max_consec = cur = 0
    for r in r_vals:
        if r <= 0:
            cur += 1
            max_consec = max(max_consec, cur)
        else:
            cur = 0

    monthly: Dict[str, List[float]] = {}
    for t in closed_trades:
        if t.r_multiple is None:
            continue
        mk = t.entry_time.strftime("%Y-%m")
        monthly.setdefault(mk, []).append(t.r_multiple)

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
        "final_eq": final_eq,
        "x10": x10,
        "max_consec_loss": max_consec,
        "r_vals": r_vals,
        "monthly": monthly,
        "session_stats": session_stats,
        "pair_stats": pair_stats,
        "exit_reasons": exit_reasons,
        "closed_trades": closed_trades,
        "config": cfg,
    }


def _empty(cfg):
    return {
        "trades": 0, "wr": 0, "avg_r": 0, "total_r": 0, "max_dd": 0,
        "final_eq": cfg.start_equity, "x10": None, "r_vals": [],
        "monthly": {}, "config": cfg, "pair_stats": {},
        "exit_reasons": {}, "session_stats": {}, "closed_trades": [],
        "max_consec_loss": 0,
    }


# ═══════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════

def main():
    t0 = time.time()
    random.seed(42)

    print("=" * 95)
    print("  OUT-OF-SAMPLE VALIDATION")
    print("  Testing on 35 Binance pairs NEVER used in optimization")
    print("  This is the acid test: if it works here, the edge is REAL")
    print("=" * 95)

    # Load Binance (out-of-sample) data
    print("\n  Loading Binance out-of-sample pairs...")
    oos_data = load_binance_pairs()
    total_c = sum(len(c) for c in oos_data.values())
    print(f"  {len(oos_data)} pairs, {total_c:,} candles")

    # Show pair list
    print(f"  Pairs: {', '.join(sorted(p.replace('/USDT:USDT','') for p in oos_data))}")

    # Date range
    all_dts = []
    for c in oos_data.values():
        if c:
            all_dts.extend([c[0].dt, c[-1].dt])
    print(f"  Date range: {min(all_dts).date()} to {max(all_dts).date()}")

    # ── Define strategies to test ──
    configs = []

    # The main candidates
    for tp in [1.0, 1.5]:
        for retest in [False, True]:
            for risk in [0.02, 0.03, 0.04]:
                rt_label = "RT" if retest else "NR"
                name = f"TP{tp}R-{rt_label}-{risk:.0%}"
                configs.append(OOSConfig(
                    name=name,
                    tp_r=tp,
                    require_retest=retest,
                    risk_pct=risk,
                ))

    # Also test no-filter versions (no fc_counter, no vol filter)
    for tp in [1.0]:
        configs.append(OOSConfig(
            name=f"TP1R-NOFILTER-3%",
            tp_r=tp,
            require_retest=False,
            risk_pct=0.03,
            fc_counter=False,
            vol_ratio_long=0.0,
            vol_ratio_short=0.0,
            min_c2_body=0.0,
        ))

    total = len(configs)
    print(f"\n  Running {total} configurations...")

    results = []
    for i, cfg in enumerate(configs):
        sys.stdout.write(f"\r  {i+1:>2d}/{total}: {cfg.name:<25s}")
        sys.stdout.flush()
        res = simulate_oos(oos_data, cfg)
        results.append(res)

    print(f"\r  Done: {total} configurations.                        \n")

    # ════════════════════════════════════════════
    #  SECTION 1: FULL TABLE
    # ════════════════════════════════════════════
    print("=" * 100)
    print("  SECTION 1: ALL OOS RESULTS (sorted by WR, then DD)")
    print("=" * 100)

    results.sort(key=lambda r: (-r["wr"], r["max_dd"]))

    print(f"\n  {'Strategy':>25s}  {'#':>5s}  {'WR':>5s}  {'AvgR':>7s}  "
          f"{'TotR':>7s}  {'MaxDD':>6s}  {'ConsL':>5s}  "
          f"{'x10':>5s}  {'Final$':>9s}  {'Exits':>12s}")
    div = (f"  {'-'*25}  {'-'*5}  {'-'*5}  {'-'*7}  "
           f"{'-'*7}  {'-'*6}  {'-'*5}  {'-'*5}  {'-'*9}  {'-'*12}")
    print(div)

    for res in results:
        cfg = res["config"]
        x10s = f"{res['x10']}t" if res.get("x10") else "-"
        tp_count = res["exit_reasons"].get("tp", 0)
        sl_count = res["exit_reasons"].get("sl", 0)
        be_count = res["exit_reasons"].get("be", 0)
        exits_str = f"tp={tp_count} sl={sl_count}"
        if be_count:
            exits_str += f" be={be_count}"
        print(f"  {cfg.name:>25s}  {res['trades']:>5d}  {res['wr']:>4.1%}  "
              f"{res['avg_r']:>+.4f}  {res['total_r']:>+6.1f}  "
              f"{res['max_dd']:>5.1%}  {res.get('max_consec_loss',0):>5d}  "
              f"{x10s:>5s}  ${res['final_eq']:>8,.0f}  {exits_str}")

    # ════════════════════════════════════════════
    #  SECTION 2: DETAILED - TOP 3
    # ════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  SECTION 2: TOP 3 DETAILED")
    print("=" * 100)

    # Sort by: positive total_r, then highest WR, then lowest DD
    viable = [r for r in results if r["total_r"] > 0 and r["trades"] >= 30]
    viable.sort(key=lambda r: (-r["wr"], r["max_dd"]))
    best3 = viable[:3] if viable else sorted(results, key=lambda r: -r["wr"])[:3]

    for idx, res in enumerate(best3):
        cfg = res["config"]
        mult = res["final_eq"] / cfg.start_equity
        print(f"\n  #{idx+1}: {cfg.name}")
        print(f"  {'─' * 50}")
        print(f"    Trades: {res['trades']:>4d}  |  WR: {res['wr']:.1%}  |  "
              f"Avg R: {res['avg_r']:+.4f}  |  Total R: {res['total_r']:+.1f}")
        print(f"    Avg Win: {res.get('avg_win',0):+.3f}  |  "
              f"Avg Loss: {res.get('avg_loss',0):+.3f}  |  "
              f"Payoff: {abs(res.get('avg_win',0)/res.get('avg_loss',-1)) if res.get('avg_loss', 0) < 0 else 0:.2f}")
        print(f"    DD: {res['max_dd']:.1%}  |  Consec: {res['max_consec_loss']}  |  "
              f"${cfg.start_equity:.0f} -> ${res['final_eq']:,.2f} (x{mult:.1f})")

        # Monthly
        if res.get("monthly"):
            print(f"    Monthly:")
            eq = cfg.start_equity
            for mk in sorted(res["monthly"].keys()):
                rv = res["monthly"][mk]
                w = sum(1 for r in rv if r > 0)
                wr = w / len(rv) if rv else 0
                tot = sum(rv)
                s_eq = eq
                for r in rv:
                    eq += eq * cfg.risk_pct * r
                    eq = max(eq, 0.01)
                ret = (eq - s_eq) / s_eq if s_eq > 0 else 0
                is_neg = " !!!" if tot < 0 else ""
                print(f"      {mk}: {len(rv):>3d}t  WR={wr:>4.0%}  "
                      f"R={tot:>+6.1f}  "
                      f"${s_eq:>7.0f} -> ${eq:>7.0f}  ({ret:>+5.0%}){is_neg}")

        # Top/bottom pairs
        if res.get("pair_stats"):
            ps = sorted(res["pair_stats"].items(),
                        key=lambda x: x[1]["total_r"], reverse=True)
            prof = sum(1 for _, s in ps if s["total_r"] > 0)
            losing = sum(1 for _, s in ps if s["total_r"] <= 0)
            print(f"    Pairs: {prof} profitable / {losing} losing")
            print(f"      TOP 5:")
            for p, s in ps[:5]:
                wr = s["wins"] / s["trades"] if s["trades"] > 0 else 0
                print(f"        {p:>10s}  {s['trades']:>2d}t  "
                      f"WR={wr:.0%}  R={s['total_r']:+.1f}")
            print(f"      BOTTOM 5:")
            for p, s in ps[-5:]:
                wr = s["wins"] / s["trades"] if s["trades"] > 0 else 0
                print(f"        {p:>10s}  {s['trades']:>2d}t  "
                      f"WR={wr:.0%}  R={s['total_r']:+.1f}")

    # ════════════════════════════════════════════
    #  SECTION 3: RETEST vs NO-RETEST HEAD-TO-HEAD
    # ════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  SECTION 3: RETEST vs NO-RETEST (same TP, same risk)")
    print("  Does waiting for retest improve quality?")
    print("=" * 100)

    print(f"\n  {'Config':>20s}  {'#':>5s}  {'WR':>5s}  {'AvgR':>7s}  "
          f"{'TotR':>6s}  {'DD':>6s}  {'Final$':>9s}")
    print(f"  {'-'*20}  {'-'*5}  {'-'*5}  {'-'*7}  "
          f"{'-'*6}  {'-'*6}  {'-'*9}")

    # Group by TP + risk
    for res in sorted(results, key=lambda r: (r["config"].tp_r,
                                               r["config"].risk_pct,
                                               r["config"].require_retest)):
        cfg = res["config"]
        if "NOFILTER" in cfg.name:
            continue
        print(f"  {cfg.name:>20s}  {res['trades']:>5d}  {res['wr']:>4.1%}  "
              f"{res['avg_r']:>+.4f}  {res['total_r']:>+5.1f}  "
              f"{res['max_dd']:>5.1%}  ${res['final_eq']:>8,.0f}")

    # ════════════════════════════════════════════
    #  SECTION 4: MONTE CARLO
    # ════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  SECTION 4: MONTE CARLO (2000 trials)")
    print("=" * 100)

    for res in best3:
        cfg = res["config"]
        r_vals = res["r_vals"]
        closed = res.get("closed_trades", [])
        d_risks = [t.dollar_risk for t in closed if t.r_multiple is not None]
        if not r_vals or not d_risks:
            continue

        print(f"\n  {cfg.name}  ({len(r_vals)} trades, WR={res['wr']:.1%})")
        mc = monte_carlo(r_vals, d_risks, 2000, cfg.start_equity)
        print(f"    Median DD:   {mc['median_dd']:>6.1%}")
        print(f"    95th pctl:   {mc['p95_dd']:>6.1%}  <- PLAN FOR THIS")
        print(f"    Bust (<$5):  {mc['bust_pct']:>6.1%}")
        print(f"    x10 chance:  {mc['x10_pct']:>6.1%}")
        if mc.get("x10_median"):
            print(f"    x10 median:  trade #{mc['x10_median']}")
        print(f"    Median eq:   ${mc['median_final']:>,.0f}")
        print(f"    10th pctl:   ${mc['p10_final']:>,.0f}")

    # ════════════════════════════════════════════
    #  SECTION 5: COMPARISON vs IN-SAMPLE (Bybit)
    # ════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  SECTION 5: OOS vs IN-SAMPLE COMPARISON")
    print("  If OOS is close to in-sample -> edge is real")
    print("  If OOS is much worse -> we curve-fitted")
    print("=" * 100)

    # Run the same best config on the original Bybit data for comparison
    print("\n  Running in-sample comparison on Bybit top-20 pairs...")
    from research.session_sim import load_all_pairs

    TOP_20 = {
        "MYX/USDT:USDT", "SEI/USDT:USDT", "HEMI/USDT:USDT", "WLFI/USDT:USDT",
        "KITE/USDT:USDT", "DYDX/USDT:USDT", "ENA/USDT:USDT", "INIT/USDT:USDT",
        "PENGU/USDT:USDT", "US/USDT:USDT", "FHE/USDT:USDT", "ARB/USDT:USDT",
        "RENDER/USDT:USDT", "SOMI/USDT:USDT", "COAI/USDT:USDT", "FIL/USDT:USDT",
        "WIF/USDT:USDT", "MON/USDT:USDT", "UNI/USDT:USDT", "ZKP/USDT:USDT",
    }

    bybit_all = load_all_pairs()
    bybit_top20 = {p: c for p, c in bybit_all.items() if p in TOP_20}
    print(f"  Loaded {len(bybit_top20)} Bybit top-20 pairs")

    # Use the best OOS config for in-sample comparison
    if best3:
        best_cfg = best3[0]["config"]
        compare_cfg = OOSConfig(
            name="IS-" + best_cfg.name,
            tp_r=best_cfg.tp_r,
            require_retest=best_cfg.require_retest,
            risk_pct=best_cfg.risk_pct,
            fc_counter=best_cfg.fc_counter,
            vol_ratio_long=best_cfg.vol_ratio_long,
            vol_ratio_short=best_cfg.vol_ratio_short,
            min_c2_body=best_cfg.min_c2_body,
        )
        is_res = simulate_oos(bybit_top20, compare_cfg)

        oos = best3[0]
        print(f"\n  {'Metric':>15s}  {'OOS (Binance)':>15s}  {'In-Sample (Bybit)':>17s}  {'Delta':>10s}")
        print(f"  {'-'*15}  {'-'*15}  {'-'*17}  {'-'*10}")

        metrics = [
            ("Pairs", f"{len(oos_data)}", f"{len(bybit_top20)}", "-"),
            ("Trades", f"{oos['trades']}", f"{is_res['trades']}", "-"),
            ("Win Rate", f"{oos['wr']:.1%}", f"{is_res['wr']:.1%}",
             f"{oos['wr'] - is_res['wr']:+.1%}"),
            ("Avg R", f"{oos['avg_r']:+.4f}", f"{is_res['avg_r']:+.4f}",
             f"{oos['avg_r'] - is_res['avg_r']:+.4f}"),
            ("Total R", f"{oos['total_r']:+.1f}", f"{is_res['total_r']:+.1f}",
             f"{oos['total_r'] - is_res['total_r']:+.1f}"),
            ("Max DD", f"{oos['max_dd']:.1%}", f"{is_res['max_dd']:.1%}",
             f"{oos['max_dd'] - is_res['max_dd']:+.1%}"),
            ("Consec Loss", f"{oos['max_consec_loss']}", f"{is_res['max_consec_loss']}", "-"),
            ("Final $", f"${oos['final_eq']:,.0f}", f"${is_res['final_eq']:,.0f}", "-"),
        ]

        for name, oos_v, is_v, delta in metrics:
            print(f"  {name:>15s}  {oos_v:>15s}  {is_v:>17s}  {delta:>10s}")

    # ════════════════════════════════════════════
    #  SECTION 6: LIVE BEHAVIOUR REALITY CHECKS
    # ════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  SECTION 6: LIVE BEHAVIOUR REALITY CHECKS")
    print("  Verifying backtest mirrors real market conditions")
    print("=" * 100)

    if best3:
        res = best3[0]
        r_vals = res["r_vals"]
        closed = res.get("closed_trades", [])

        # Check 1: Win/loss distribution
        wins = [r for r in r_vals if r > 0]
        losses = [r for r in r_vals if r <= 0]
        print(f"\n  1. WIN/LOSS DISTRIBUTION ({res['config'].name}):")
        print(f"     Winners: {len(wins)} ({len(wins)/len(r_vals)*100:.0f}%)")
        print(f"       Min win:  {min(wins):+.3f}R" if wins else "")
        print(f"       Max win:  {max(wins):+.3f}R" if wins else "")
        print(f"       Avg win:  {statistics.mean(wins):+.3f}R" if wins else "")
        print(f"     Losers:  {len(losses)} ({len(losses)/len(r_vals)*100:.0f}%)")
        print(f"       Min loss: {min(losses):+.3f}R" if losses else "")
        print(f"       Max loss: {max(losses):+.3f}R" if losses else "")
        print(f"       Avg loss: {statistics.mean(losses):+.3f}R" if losses else "")

        # Check 2: Trade frequency
        if closed:
            trades_per_day = {}
            for t in closed:
                day = t.entry_time.strftime("%Y-%m-%d")
                trades_per_day[day] = trades_per_day.get(day, 0) + 1
            if trades_per_day:
                avg_tpd = statistics.mean(trades_per_day.values())
                max_tpd = max(trades_per_day.values())
                zero_days = sum(1 for v in trades_per_day.values() if v == 0)
                print(f"\n  2. TRADE FREQUENCY:")
                print(f"     Avg trades/day: {avg_tpd:.1f}")
                print(f"     Max trades/day: {max_tpd}")
                print(f"     Days with trades: {len(trades_per_day)}")

        # Check 3: Session distribution
        if res.get("session_stats"):
            print(f"\n  3. SESSION DISTRIBUTION:")
            for sn in SESSION_ORDER:
                ss = res["session_stats"][sn]
                if ss["trades"] == 0:
                    continue
                wr = ss["wins"] / ss["trades"] if ss["trades"] else 0
                avg = ss["total_r"] / ss["trades"] if ss["trades"] else 0
                print(f"     {sn.upper():>7s}: {ss['trades']:>3d}t  "
                      f"WR={wr:.1%}  AvgR={avg:+.4f}  TotR={ss['total_r']:+.1f}")

        # Check 4: Slippage sensitivity
        print(f"\n  4. SLIPPAGE SENSITIVITY:")
        print(f"     Current fee: 0.04R (realistic for Bybit limit fills)")
        print(f"     If fills are market orders, real fee could be 0.08-0.12R")
        base_wr = res["wr"]
        base_tr = res["total_r"]
        for extra_slip in [0.02, 0.04, 0.06, 0.08]:
            adj_r = [r - extra_slip for r in r_vals]
            adj_wr = sum(1 for r in adj_r if r > 0) / len(adj_r) if adj_r else 0
            adj_tr = sum(adj_r)
            print(f"     +{extra_slip:.2f}R slip: WR={adj_wr:.1%} "
                  f"(was {base_wr:.1%})  TotR={adj_tr:+.1f} "
                  f"(was {base_tr:+.1f})")

        # Check 5: Direction split
        longs = [t for t in closed if t.direction == "long"]
        shorts = [t for t in closed if t.direction == "short"]
        print(f"\n  5. DIRECTION SPLIT:")
        if longs:
            l_wr = sum(1 for t in longs if t.r_multiple and t.r_multiple > 0) / len(longs)
            print(f"     Longs:  {len(longs)} trades, WR={l_wr:.1%}")
        if shorts:
            s_wr = sum(1 for t in shorts if t.r_multiple and t.r_multiple > 0) / len(shorts)
            print(f"     Shorts: {len(shorts)} trades, WR={s_wr:.1%}")

    # ════════════════════════════════════════════
    #  VERDICT
    # ════════════════════════════════════════════
    print(f"\n{'=' * 100}")
    print("  VERDICT: IS THE EDGE REAL?")
    print("=" * 100)

    if best3:
        res = best3[0]
        oos_wr = res["wr"]
        oos_dd = res["max_dd"]
        oos_tr = res["total_r"]

        if oos_wr >= 0.50 and oos_tr > 20:
            print(f"\n  EDGE CONFIRMED on out-of-sample data!")
            print(f"  OOS Win Rate: {oos_wr:.1%} (target: >50%)")
            print(f"  OOS Total R:  {oos_tr:+.1f}")
            print(f"  OOS Max DD:   {oos_dd:.1%}")
        elif oos_wr >= 0.45 and oos_tr > 0:
            print(f"\n  EDGE LIKELY REAL but weaker on OOS data")
            print(f"  OOS Win Rate: {oos_wr:.1%} (target: >50%)")
            print(f"  OOS Total R:  {oos_tr:+.1f}")
            print(f"  OOS Max DD:   {oos_dd:.1%}")
        elif oos_tr > 0:
            print(f"\n  MARGINAL EDGE — proceed with caution")
            print(f"  OOS Win Rate: {oos_wr:.1%}")
            print(f"  OOS Total R:  {oos_tr:+.1f}")
        else:
            print(f"\n  NO EDGE on out-of-sample data.")
            print(f"  The in-sample results were likely curve-fitted.")
            print(f"  OOS Win Rate: {oos_wr:.1%}")
            print(f"  OOS Total R:  {oos_tr:+.1f}")

    elapsed = time.time() - t0
    print(f"\n  Runtime: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print()


if __name__ == "__main__":
    main()
