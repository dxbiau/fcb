"""
v13pro/logger.py -- Logging for v13pro bot.

Console (ANSI colored) + daily file rotation.
Self-contained: no imports from obr/.
"""

import os
import sys
import logging
import traceback
from datetime import datetime, timezone
from v13pro import config as cfg

os.makedirs(cfg.LOG_DIR, exist_ok=True)

if sys.platform == "win32":
    os.system("")

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class C:
    RESET   = "\033[0m";  BOLD = "\033[1m";  DIM = "\033[2m"
    RED     = "\033[31m";  GREEN = "\033[32m"; YELLOW = "\033[33m"
    BLUE    = "\033[34m";  MAGENTA = "\033[35m"; CYAN = "\033[36m"
    WHITE   = "\033[37m"
    BRED    = "\033[91m";  BGREEN = "\033[92m"; BYELLOW = "\033[93m"
    BBLUE   = "\033[94m";  BMAGENTA = "\033[95m"; BCYAN = "\033[96m"
    BWHITE  = "\033[97m";  BG_RED = "\033[41m"


class _ColorFmt(logging.Formatter):
    LEVEL_MAP = {
        logging.DEBUG:    (C.DIM + C.CYAN,    "    "),
        logging.INFO:     (C.BWHITE,          "    "),
        logging.WARNING:  (C.BYELLOW,         " ⚠️  "),
        logging.ERROR:    (C.BRED,            " ❌ "),
        logging.CRITICAL: (C.BOLD + C.BG_RED + C.WHITE, " 💀 "),
    }

    def format(self, record):
        style, icon = self.LEVEL_MAP.get(record.levelno, (C.WHITE, "    "))
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        msg = record.getMessage()
        return f"{C.DIM}{C.CYAN}{ts}{C.RESET}{icon}{C.DIM}│{C.RESET} {msg}"


_logger = logging.getLogger("v13pro")
_logger.setLevel(logging.DEBUG)
_logger.propagate = False

_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.INFO)
_ch.setFormatter(_ColorFmt())
_logger.addHandler(_ch)


def _get_file_handler():
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = os.path.join(cfg.LOG_DIR, f"v13pro_{today}.log")
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    return fh

_fh = _get_file_handler()
_logger.addHandler(_fh)
_last_log_date = datetime.now(timezone.utc).strftime("%Y%m%d")


def _rotate_if_needed():
    global _fh, _last_log_date
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    if today != _last_log_date:
        _logger.removeHandler(_fh)
        _fh.close()
        _fh = _get_file_handler()
        _logger.addHandler(_fh)
        _last_log_date = today


def debug(msg):   _rotate_if_needed(); _logger.debug(msg)
def info(msg):    _rotate_if_needed(); _logger.info(msg)
def warning(msg): _rotate_if_needed(); _logger.warning(msg)
def error(msg):   _rotate_if_needed(); _logger.error(msg)
def critical(msg):_rotate_if_needed(); _logger.critical(msg)

def log_exception(context, exc):
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    error(f"{context}: {exc}")
    debug("".join(tb))

def header(title, icon=""):
    info(f"\n{'═'*56}")
    info(f"  {icon} {C.BOLD}{C.BWHITE}{title}{C.RESET}")
    info(f"{'═'*56}")

def divider():
    info(f"{C.DIM}{'─'*56}{C.RESET}")

def banner_box(lines, color=C.BGREEN):
    w = 56
    info(f"\n{color}{'═'*w}{C.RESET}")
    for line in lines:
        info(f"  {line}")
    info(f"{color}{'═'*w}{C.RESET}\n")

def position_opened(symbol, direction, entry, sl, tp, qty, dollar_risk):
    short = symbol.split('/')[0]
    dc = C.BGREEN if direction == "long" else C.BRED
    info(f"  {'📈' if direction=='long' else '📉'} {dc}{direction.upper()}{C.RESET} "
         f"{C.BOLD}{short}{C.RESET} "
         f"@ {entry:.6g}  SL={sl:.6g}  TP={tp:.6g}  "
         f"qty={qty:.4g}  risk=${dollar_risk:.2f}")

def position_closed(symbol, direction, entry, exit_price, pnl_r, pnl_usd, reason):
    short = symbol.split('/')[0]
    rc = C.BGREEN if pnl_r > 0 else C.BRED
    info(f"  {'✅' if pnl_r > 0 else '❌'} {C.BOLD}{short}{C.RESET} "
         f"closed ({reason}) "
         f"{rc}{pnl_r:+.2f}R{C.RESET} "
         f"({rc}${pnl_usd:+.2f}{C.RESET})")

def heartbeat(*, equity=0, daily_growth=0, positions=0, scans=0,
              signals=0, trades=0, ws_buffers=0, uptime_h=0,
              open_count=0, session=""):
    ts = datetime.now(timezone.utc).strftime("%H:%M")
    pos = positions or open_count
    gc = C.BGREEN if daily_growth >= 0 else C.BRED
    info(f"💓 {ts} | ${equity:.2f} | {gc}{daily_growth:+.1f}%{C.RESET} | "
         f"{pos} pos | {trades} trades | {signals} sig | "
         f"WS:{ws_buffers} | {uptime_h:.1f}h")


def dashboard(*, equity=0, peak_equity=0, target_equity=5000,
              daily_growth=0, dd_pct=0, uptime_h=0,
              positions=0, max_positions=5, scans=0,
              signals=0, trades=0, wins=0, losses=0,
              pnl_r=0, pnl_usd=0, wr_pct=0,
              skill_min=40, skill_eval=0, skill_pass=0, skill_rej=0,
              learner_outcomes=0, aftermath_done=0, aftermath_pending=0,
              watchdog_healthy=True, ws_buffers=0,
              risk_pct=0.03, leverage=10, maker_tp=False, maker_entry=False,
              max_conc=5, phase_label="", session="",
              hunter_signals=0, combo_count=0, pair_count=0,
              dna_records=0, dna_clusters=0,
              open_positions=None, total_wins=0, total_losses=0,
              sentiment=None, orderflow=None, shadow=None, regime=None,
              lifecycle=None, cross_sectional=None, calibrator=None,
              burst=None, burst_optim=None, edge_radar=None,
              micro_tf=None, alignment=None, session_lc=None):
    """Rich colored dashboard display — preflight-style layout."""

    W = 58
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ── Colors for values ──
    gc = C.BGREEN if daily_growth >= 0 else C.BRED
    ec = C.BGREEN if equity >= 500 else C.BYELLOW
    dc = C.BRED if dd_pct > 20 else (C.BYELLOW if dd_pct > 10 else C.BGREEN)
    wrc = C.BGREEN if wr_pct >= 50 else (C.BYELLOW if wr_pct >= 35 else C.BRED)
    pnlc = C.BGREEN if pnl_r >= 0 else C.BRED
    wdc = C.BGREEN if watchdog_healthy else C.BRED

    on = f"{C.BGREEN}ON{C.RESET}"
    off = f"{C.DIM}OFF{C.RESET}"

    progress = min(100, max(0, (equity / target_equity * 100))) if target_equity > 0 else 0
    bar_len = 20
    filled = int(bar_len * progress / 100)
    bar = f"{C.BGREEN}{'█' * filled}{C.DIM}{'░' * (bar_len - filled)}{C.RESET}"

    # Health icon
    if not watchdog_healthy:
        health = f"{C.BRED}🔴 UNHEALTHY{C.RESET}"
    elif positions > 0:
        health = f"{C.BGREEN}🟢 TRADING{C.RESET}"
    else:
        health = f"{C.BCYAN}🔵 SCANNING{C.RESET}"

    _p = lambda msg: info(msg)

    _p(f"\n{C.BCYAN}{'═' * W}{C.RESET}")
    _p(f"  🤖 {C.BOLD}{C.BWHITE}v13pro LIVE DASHBOARD{C.RESET}    {health}")
    _p(f"  {C.DIM}{ts}{C.RESET}   {C.DIM}Session: {session}{C.RESET}")
    _p(f"{C.BCYAN}{'═' * W}{C.RESET}")

    # ── EQUITY BLOCK ──
    _p(f"  {C.BOLD}💰 EQUITY{C.RESET}")
    _p(f"     Balance   {ec}{C.BOLD}${equity:,.2f}{C.RESET}"
       f"               Peak  ${peak_equity:,.2f}")
    _p(f"     Growth    {gc}{daily_growth:+.1f}%{C.RESET}"
       f"                DD    {dc}{dd_pct:.1f}%{C.RESET}")
    _p(f"     Target    ${target_equity:,.0f}"
       f"       {bar} {progress:.0f}%")
    if phase_label:
        _p(f"     {C.DIM}{phase_label}{C.RESET}")
    _p(f"  {C.DIM}{'─' * (W - 4)}{C.RESET}")

    # ── TRADING BLOCK ──
    _p(f"  {C.BOLD}📊 TRADING{C.RESET}")
    _p(f"     Positions {C.BWHITE}{positions}{C.RESET} / {max_positions}"
       f"              Scans   {scans}")
    _p(f"     Signals   {signals}"
       f"                Trades  {trades}")
    if trades > 0 or total_wins > 0 or total_losses > 0:
        _p(f"     Wins      {C.BGREEN}{wins}{C.RESET}"
           f"                Losses  {C.BRED}{losses}{C.RESET}"
           f"          {C.DIM}(today){C.RESET}")
        total_all = total_wins + total_losses
        total_wr = (total_wins / total_all * 100) if total_all > 0 else 0
        twc = C.BGREEN if total_wr >= 50 else (C.BYELLOW if total_wr >= 35 else C.BRED)
        _p(f"     Total W   {C.BGREEN}{total_wins}{C.RESET}"
           f"                Total L {C.BRED}{total_losses}{C.RESET}"
           f"          {C.DIM}(all-time){C.RESET}")
        _p(f"     Win Rate  {wrc}{wr_pct:.1f}%{C.RESET} {C.DIM}today{C.RESET}"
           f"          {twc}{total_wr:.1f}%{C.RESET} {C.DIM}all-time{C.RESET}")
        _p(f"     PnL       {pnlc}{pnl_r:+.2f}R{C.RESET}"
           f" ({pnlc}${pnl_usd:+.2f}{C.RESET})")
    _p(f"  {C.DIM}{'─' * (W - 4)}{C.RESET}")

    # ── INTELLIGENCE BLOCK ──
    _p(f"  {C.BOLD}🧠 INTELLIGENCE{C.RESET}")

    # Session countdown
    from v13pro import config as _cfg
    from datetime import datetime as _dt, timezone as _tz
    _now = _dt.now(_tz.utc)
    _hour_f = _now.hour + _now.minute / 60  # fractional hour
    _sess_order = ["asia", "london", "ny"]
    _sess_icons = {"asia": "🌏", "london": "🌍", "ny": "🌎"}
    _cur_sess = session
    # Find next session
    _cur_idx = _sess_order.index(_cur_sess) if _cur_sess in _sess_order else 0
    _next_idx = (_cur_idx + 1) % len(_sess_order)
    _next_sess = _sess_order[_next_idx]
    _next_icon = _sess_icons.get(_next_sess, "🔄")
    _cur_icon = _sess_icons.get(_cur_sess, "🔄")
    _, _cur_end = _cfg.SESSIONS.get(_cur_sess, (0, 8))
    # Hours until next session starts (= current session end)
    _remain_h = _cur_end - _hour_f
    if _remain_h < 0:
        _remain_h += 24
    _rem_hrs = int(_remain_h)
    _rem_mins = int((_remain_h - _rem_hrs) * 60)
    _p(f"     {_cur_icon} Session  {C.BWHITE}{_cur_sess.upper()}{C.RESET}"
       f"    ➜ {_next_icon} {C.BCYAN}{_next_sess.upper()}{C.RESET}"
       f" in {C.BYELLOW}{_rem_hrs}h {_rem_mins}m{C.RESET}")

    _p(f"     🎯 Skill     min={skill_min}  eval={skill_eval}"
       f"  pass={skill_pass}  rej={skill_rej}")
    _p(f"     📚 Learner   {learner_outcomes} outcomes tracked")
    if aftermath_done > 0 or aftermath_pending > 0:
        _p(f"     🔍 Aftermath {aftermath_done} done, {aftermath_pending} pending")
    _p(f"     {'💚' if watchdog_healthy else '🔴'} Watchdog  "
       f"{wdc}{'HEALTHY' if watchdog_healthy else 'UNHEALTHY'}{C.RESET}"
       f"     📡 WS  {ws_buffers} buffers")
    if hunter_signals > 0:
        _p(f"     🔭 Hunter    {hunter_signals} non-portfolio signals")
    if dna_records > 0 or dna_clusters > 0:
        dc = C.BGREEN if dna_clusters > 0 else C.DIM
        _p(f"     🧬 DNA       {dna_records} records  "
           f"{dc}{dna_clusters} proven clusters{C.RESET}")
    # Sentiment gauge
    if sentiment and sentiment.get("bias") and sentiment["bias"] != "unknown":
        s_bias = sentiment["bias"]
        s_arrows = sentiment.get("arrows", "")
        s_score = sentiment.get("score", 0)
        s_conf = sentiment.get("confidence", 0)
        if s_bias == "bull":
            s_icon = "🟢"
            s_color = C.BGREEN
        elif s_bias == "bear":
            s_icon = "🔴"
            s_color = C.BRED
        else:
            s_icon = "⚪"
            s_color = C.BYELLOW
        _p(f"     {s_icon} Sentiment "
           f"{s_color}{C.BOLD}{s_bias.upper()}{C.RESET}"
           f"  {C.DIM}score={s_score:+.3f}  conf={s_conf:.0%}{C.RESET}"
           f"  {s_arrows}")
    _p(f"  {C.DIM}{'─' * (W - 4)}{C.RESET}")

    # ── ORDER FLOW INTEL BLOCK ──
    if orderflow and orderflow.get("snapshots", 0) > 0:
        _p(f"  {C.BOLD}📈 ORDER FLOW INTEL{C.RESET}")
        of_snaps = orderflow.get('snapshots', 0)
        of_errs = orderflow.get('errors', 0)
        _p(f"     Snapshots  {of_snaps}")
        if of_errs > 0:
            _p(f"     Errors     {C.BRED}{of_errs}{C.RESET}")
        _p(f"  {C.DIM}{'─' * (W - 4)}{C.RESET}")

    # ── SHADOW TRADER BLOCK ──
    if shadow and shadow.get("signals_seen", 0) > 0:
        _p(f"  {C.BOLD}👻 SHADOW TRADER{C.RESET}  {C.DIM}(passive sim){C.RESET}")
        sh_seen = shadow.get('signals_seen', 0)
        sh_pend = shadow.get('pending', 0)
        sh_done = shadow.get('completed', 0)
        sh_wins = shadow.get('wins', 0)
        sh_losses = shadow.get('losses', 0)
        sh_wr = shadow.get('wr_pct', 0)
        sh_errs = shadow.get('errors', 0)
        _p(f"     Signals    {C.BWHITE}{sh_seen}{C.RESET}"
           f"   pending {C.BYELLOW}{sh_pend}{C.RESET}"
           f"   done {C.BGREEN}{sh_done}{C.RESET}")
        if sh_done > 0:
            wr_c = C.BGREEN if sh_wr >= 50 else C.BRED
            _p(f"     W/L        {C.BGREEN}{sh_wins}W{C.RESET}"
               f" / {C.BRED}{sh_losses}L{C.RESET}"
               f"   WR {wr_c}{sh_wr:.1f}%{C.RESET}")
        if sh_errs > 0:
            _p(f"     Errors     {C.BRED}{sh_errs}{C.RESET}")
        _p(f"  {C.DIM}{'─' * (W - 4)}{C.RESET}")

    # ── REGIME DETECTOR BLOCK ──
    if regime and regime.get("regime"):
        rg = regime.get("regime", "NORMAL")
        rg_mult = regime.get("global_mult", 1.0)
        rg_wr = regime.get("rolling_wr", 0)
        rg_n = regime.get("window_n", 0)
        rg_conf = regime.get("confidence", 0)
        # Color by regime
        if rg == "HOT":
            rg_c = C.BGREEN
            rg_icon = "🔥"
        elif rg == "WARM":
            rg_c = C.BGREEN
            rg_icon = "☀️"
        elif rg == "NORMAL":
            rg_c = C.BWHITE
            rg_icon = "⚡"
        elif rg == "COOL":
            rg_c = C.BYELLOW
            rg_icon = "🌤️"
        else:  # COLD
            rg_c = C.BRED
            rg_icon = "❄️"
        _p(f"  {C.BOLD}{rg_icon} REGIME{C.RESET}")
        _p(f"     State      {rg_c}{C.BOLD}{rg}{C.RESET}"
           f"  mult={rg_mult:.2f}x"
           f"  WR={rg_wr:.1f}%"
           f"  n={rg_n}")
        sess_mults = regime.get("session_mults", {})
        if sess_mults:
            parts = []
            for s, m in sess_mults.items():
                mc = C.BGREEN if m >= 1.0 else (C.BYELLOW if m >= 0.8 else C.BRED)
                parts.append(f"{s}={mc}{m:.2f}x{C.RESET}")
            _p(f"     Sessions   {' '.join(parts)}")
        _p(f"  {C.DIM}{'─' * (W - 4)}{C.RESET}")

    # ── BURST ENGINE BLOCK ──
    if burst and burst.get("bcs") is not None:
        b_bcs = burst.get("bcs", 0.5)
        b_state = burst.get("state", "NORMAL")
        b_valid = burst.get("shadow_valid", False)
        b_risk = burst.get("risk_mult", 1.0)
        b_lev = burst.get("lev_mult", 1.0)
        b_tp = burst.get("tp_mult", 1.0)
        b_dd = burst.get("dd_pct", 0)

        # State coloring
        if b_state == "BURST":
            st_c = C.BGREEN
            st_icon = "🚀"
        elif b_state == "DECAY":
            st_c = C.BRED
            st_icon = "📉"
        else:
            st_c = C.BWHITE
            st_icon = "⚡"
        valid_str = f"{C.BGREEN}✓{C.RESET}" if b_valid else f"{C.BRED}✗{C.RESET}"

        _p(f"  {C.BOLD}{st_icon} BURST ENGINE{C.RESET}")
        _p(f"     State      {st_c}{C.BOLD}{b_state}{C.RESET}"
           f"  BCS={b_bcs:.3f}"
           f"  shadow={valid_str}")
        # Show multipliers
        r_c = C.BGREEN if b_risk > 1.0 else (C.BRED if b_risk < 1.0 else C.BWHITE)
        l_c = C.BGREEN if b_lev > 1.0 else (C.BRED if b_lev < 1.0 else C.BWHITE)
        t_c = C.BGREEN if b_tp > 1.0 else (C.BRED if b_tp < 1.0 else C.BWHITE)
        _p(f"     Risk {r_c}{b_risk:.2f}x{C.RESET}"
           f"  Lev {l_c}{b_lev:.2f}x{C.RESET}"
           f"  TP {t_c}{b_tp:.2f}x{C.RESET}"
           f"  DD={b_dd:.1f}%")
        # Top combos
        top_c = burst.get("top_combos", [])
        if top_c:
            tops = " ".join(f"{c[0]}({c[1]:.2f})" for c in top_c[:3])
            _p(f"     Top ECS    {C.DIM}{tops}{C.RESET}")
        # Burst optimizer status (Phase 2A)
        if burst_optim and burst_optim.get("enabled"):
            bo_runs = burst_optim.get("runs", 0)
            bo_score = burst_optim.get("last_score", 0)
            bo_iters = burst_optim.get("iterations", 0)
            bo_ago = burst_optim.get("last_run_ago", -1)
            ago_str = f"{bo_ago // 60}m ago" if bo_ago >= 0 else "pending"
            _p(f"     {C.DIM}Optimizer  runs={bo_runs} score={bo_score:.3f}"
               f" iters={bo_iters} ({ago_str}){C.RESET}")
        _p(f"  {C.DIM}{'─' * (W - 4)}{C.RESET}")

    # ── EDGE RADAR BLOCK ──
    if edge_radar and edge_radar.get("total_outcomes", 0) > 0:
        er_mkt = edge_radar.get("market_heat", "?")
        er_peak = edge_radar.get("avg_peak_r", 0)
        er_runners = edge_radar.get("runner_pct", 0)
        er_hot = edge_radar.get("hot_seat", False)
        er_sigs = edge_radar.get("hot_seat_signals", 0)
        er_hot_c = edge_radar.get("hot_combos", [])
        er_cold_c = edge_radar.get("cold_combos", [])

        # Market heat color
        if er_mkt == "HOT":
            mkt_c, mkt_icon = C.BGREEN, "🔥"
        elif er_mkt == "WARM":
            mkt_c, mkt_icon = C.BYELLOW, "☀️"
        else:
            mkt_c, mkt_icon = C.BRED, "❄️"

        hot_str = f"{C.BGREEN}🔥 HOT SEAT ({er_sigs}/3){C.RESET}" if er_hot else f"{C.DIM}no{C.RESET}"

        _p(f"  {C.BOLD}{mkt_icon} EDGE RADAR{C.RESET}")
        _p(f"     Market     {mkt_c}{C.BOLD}{er_mkt}{C.RESET}"
           f"  peak={er_peak:.2f}R"
           f"  runners={er_runners:.0%}")
        _p(f"     Hot Seat   {hot_str}")
        if er_hot_c:
            _p(f"     {C.BGREEN}HOT{C.RESET}: {', '.join(er_hot_c[:5])}")
        if er_cold_c:
            _p(f"     {C.BRED}COLD{C.RESET}: {', '.join(er_cold_c[:5])}")
        _p(f"  {C.DIM}{'─' * (W - 4)}{C.RESET}")

    # ── MICRO-TF INTELLIGENCE BLOCK ──
    if micro_tf and micro_tf.get("total_recorded", 0) > 0:
        mt_baro = micro_tf.get("barometer", {})
        mt_label = mt_baro.get("label", "?")
        mt_wr = mt_baro.get("wr", 0)
        mt_n = mt_baro.get("n", 0)
        mt_hot = micro_tf.get("hot_strategies", [])
        mt_cold = micro_tf.get("cold_strategies", [])
        mt_warm = micro_tf.get("warm_strategies", [])
        mt_total = micro_tf.get("total_recorded", 0)
        mt_wins = micro_tf.get("total_wins", 0)
        mt_losses = micro_tf.get("total_losses", 0)

        if mt_label == "HOT":
            baro_c, baro_icon = C.BGREEN, "🔥"
        elif mt_label == "COLD":
            baro_c, baro_icon = C.BRED, "❄️"
        elif mt_label == "BUILDING":
            baro_c, baro_icon = C.DIM, "🔨"
        else:
            baro_c, baro_icon = C.BYELLOW, "📊"

        _p(f"  {C.BOLD}{baro_icon} MICRO-TF INTELLIGENCE (3m/5m){C.RESET}")
        _p(f"     Barometer  {baro_c}{C.BOLD}{mt_label}{C.RESET}"
           f"  WR={mt_wr*100:.0f}%  N={mt_n}")
        _p(f"     Outcomes   {mt_total} ({mt_wins}W / {mt_losses}L)")
        if mt_hot:
            _p(f"     {C.BGREEN}HOT{C.RESET}: {', '.join(mt_hot[:5])}")
        if mt_warm:
            _p(f"     {C.BYELLOW}WARM{C.RESET}: {', '.join(mt_warm[:5])}")
        if mt_cold:
            _p(f"     {C.BRED}COLD{C.RESET}: {', '.join(mt_cold[:5])}")
        _p(f"  {C.DIM}{'─' * (W - 4)}{C.RESET}")

    # ── MOMENTUM ALIGNMENT BLOCK ──
    if alignment:
        a_state = alignment.get("state", "?")
        a_score = alignment.get("score", 0)
        a_dir = alignment.get("direction", "?")
        a_sust = alignment.get("sustained", False)
        a_mult = alignment.get("risk_mult", 1.0)
        a_micro = alignment.get("micro_synergy", "N/A")
        a_dd = alignment.get("dd_floor")
        a_conv = alignment.get("use_config_conviction", False)
        a_promo = alignment.get("promoted_combos", [])

        if a_state == "ALIGNED":
            ac, a_icon = (C.BGREEN, "🎯") if a_sust else (C.BYELLOW, "⏳")
        elif a_state == "CONFLICTED":
            ac, a_icon = C.BRED, "⚠️"
        else:
            ac, a_icon = C.BYELLOW, "📐"

        _p(f"  {C.BOLD}{a_icon} MOMENTUM ALIGNMENT{C.RESET}")
        _p(f"     State      {ac}{C.BOLD}{a_state}{C.RESET}"
           f"{'  SUSTAINED' if a_sust else ''}"
           f"  score={a_score:.2f}  dir={a_dir}")
        _p(f"     Risk       x{a_mult:.2f}  micro={a_micro}"
           f"{'  DD_FLOOR=' + str(a_dd) if a_dd else ''}")
        coins = alignment.get("coins", {})
        if coins:
            parts = []
            for cn, cd in coins.items():
                s_icon = "↑" if cd.get("structure") == "hh_hl" else ("↓" if cd.get("structure") == "ll_lh" else "→")
                parts.append(f"{cn}{s_icon}")
            _p(f"     Coins      {' '.join(parts)}"
               f"{'  CONFIG_CONV' if a_conv else ''}")
        if a_promo:
            _p(f"     {C.BGREEN}PROMOTED{C.RESET}: {', '.join(f'{s}/{t}' for s,t in a_promo)}")
        _p(f"  {C.DIM}{'─' * (W - 4)}{C.RESET}")

    # ── SESSION LIFECYCLE BLOCK ──
    if session_lc:
        sl_session = session_lc.get("session", "?")
        sl_phase = session_lc.get("phase", "?")
        sl_trades = session_lc.get("trades", 0)
        sl_wins = session_lc.get("wins", 0)
        sl_losses = session_lc.get("losses", 0)
        sl_pnl = session_lc.get("pnl_r", 0)
        sl_peak = session_lc.get("peak_pnl_r", 0)
        sl_giveback = session_lc.get("giveback_r", 0)
        sl_mult = session_lc.get("risk_mult", 1.0)
        sl_tp = session_lc.get("tp_mult", 1.0)
        sl_momentum = session_lc.get("momentum", False)
        sl_hot = session_lc.get("hot", False)
        sl_fatigued = session_lc.get("fatigued", False)
        sl_stopped = session_lc.get("stopped", False)

        if sl_phase == "EARLY":
            slc = C.BGREEN
        elif sl_phase == "PEAK":
            slc = C.BCYAN
        else:
            slc = C.BYELLOW

        flags = ""
        if sl_momentum: flags += f" {C.BGREEN}MOMENTUM{C.RESET}"
        if sl_hot: flags += f" {C.BGREEN}HOT!{C.RESET}"
        if sl_fatigued: flags += f" {C.BRED}FATIGUED{C.RESET}"
        if sl_stopped: flags += f" {C.BRED}STOPPED{C.RESET}"

        sl_pnlc = C.BGREEN if sl_pnl >= 0 else C.BRED

        _p(f"  {C.BOLD}⏱️  SESSION LIFECYCLE{C.RESET}")
        _p(f"     {sl_session.upper()} {slc}{C.BOLD}{sl_phase}{C.RESET}"
           f"  {sl_wins}W/{sl_losses}L"
           f"  pnl={sl_pnlc}{sl_pnl:+.2f}R{C.RESET}"
           f"  peak={sl_peak:+.2f}R{flags}")
        if sl_giveback > 0.1:
            _p(f"     Giveback   {C.BYELLOW}{sl_giveback:.2f}R{C.RESET}"
               f"  risk×{sl_mult:.2f}  tp×{sl_tp:.2f}")
        else:
            _p(f"     Risk       x{sl_mult:.2f}  TP×{sl_tp:.2f}")
        _p(f"  {C.DIM}{'─' * (W - 4)}{C.RESET}")

    # ── LIFECYCLE / CALIBRATOR / CROSS-SECTIONAL ──
    _intel_parts = []
    if lifecycle and lifecycle.get("pairs_scored", 0) > 0:
        _intel_parts.append(f"LC:{lifecycle['pairs_scored']}pairs")
        exp = lifecycle.get("expanding", [])
        deg = lifecycle.get("degrading", [])
        if exp:
            _intel_parts.append(f"↗{','.join(exp[:3])}")
        if deg:
            _intel_parts.append(f"↘{','.join(deg[:3])}")
    if calibrator and calibrator.get("enabled"):
        h = calibrator.get("health", 1.0)
        hc = C.BGREEN if h >= 0.7 else (C.BYELLOW if h >= 0.4 else C.BRED)
        et = calibrator.get("edge_trend", 0)
        etc = C.BGREEN if et > 0 else C.BRED
        cr = calibrator.get("risk_mult", 1.0)
        _intel_parts.append(f"Health:{hc}{h:.2f}{C.RESET}")
        _intel_parts.append(f"Edge:{etc}{et:+.3f}{C.RESET}")
        if cr < 1.0:
            _intel_parts.append(f"Cal:{C.BYELLOW}{cr:.2f}x{C.RESET}")
    if cross_sectional and cross_sectional.get("enabled"):
        cs_mult = cross_sectional.get("risk_mult", 1.0)
        if cs_mult < 1.0:
            _intel_parts.append(f"XS:{C.BYELLOW}{cs_mult:.2f}x{C.RESET}")
        if cross_sectional.get("loss_cluster_active"):
            _intel_parts.append(f"{C.BRED}LOSS_CLUSTER{C.RESET}")
    if _intel_parts:
        _p(f"  {C.BOLD}🔬 ADAPTIVE INTELLIGENCE{C.RESET}")
        _p(f"     {' | '.join(_intel_parts)}")
        _p(f"  {C.DIM}{'─' * (W - 4)}{C.RESET}")

    # ── CONFIG BLOCK ──
    _p(f"  {C.BOLD}⚙️  CONFIG{C.RESET}")
    _p(f"     Risk       {C.BYELLOW}{risk_pct*100:.1f}%{C.RESET}"
       f"   Lev  {C.BYELLOW}{leverage}x{C.RESET}"
       f"   MaxPos {C.BYELLOW}{max_conc}{C.RESET}")
    _p(f"     Maker TP   {on if maker_tp else off}"
       f"              Maker Entry  {on if maker_entry else off}")
    _p(f"     Combos     {combo_count}"
       f"                Pairs  {pair_count}")
    _p(f"     Uptime     {uptime_h:.1f}h")

    _p(f"  {C.DIM}{'─' * (W - 4)}{C.RESET}")

    # ── OPEN POSITIONS BLOCK ──
    if open_positions:
        _p(f"  {C.BOLD}📍 OPEN POSITIONS{C.RESET}  ({len(open_positions)})")
        for p in open_positions:
            side_c = C.BGREEN if p['side'] == 'long' else C.BRED
            side_icon = '🟢' if p['side'] == 'long' else '🔴'
            trail_tag = f" {C.BCYAN}TRAIL{C.RESET}" if p.get('trail') else ""
            strat_tag = f" [{p['strategy']}]" if p.get('strategy') else ""
            peak_c = C.BGREEN if p['peak_r'] > 0 else C.DIM
            # Funding rate tag
            fund = p.get('funding')
            fund_tag = ""
            if fund is not None:
                if abs(fund) >= 0.10:
                    fund_tag = f" {C.BRED}FR={fund:+.3f}%{C.RESET}"
                elif abs(fund) >= 0.03:
                    fund_tag = f" {C.BYELLOW}FR={fund:+.3f}%{C.RESET}"
                else:
                    fund_tag = f" {C.DIM}FR={fund:+.3f}%{C.RESET}"
            _p(f"     {side_icon} {side_c}{p['side'].upper():5s}{C.RESET} "
               f"{C.BWHITE}{p['symbol']:<10s}{C.RESET}"
               f" entry={p['entry']:<10.6g}"
               f" SL={C.BRED}{p['sl']:<10.6g}{C.RESET}"
               f" TP={C.BGREEN}{p['tp']:<10.6g}{C.RESET}")
            _p(f"           "
               f"target {C.BGREEN}+{p['tp_r']:.1f}R{C.RESET}"
               f" (${p['tp_usd']:.2f})"
               f"  peak {peak_c}{p['peak_r']:+.2f}R{C.RESET}"
               f"{trail_tag}{fund_tag}{strat_tag}")
    else:
        _p(f"  {C.BOLD}📍 OPEN POSITIONS{C.RESET}  {C.DIM}none{C.RESET}")

    _p(f"{C.BCYAN}{'═' * W}{C.RESET}\n")
