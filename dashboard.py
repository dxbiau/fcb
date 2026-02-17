"""
dashboard.py — Lightweight FCB Performance Dashboard

A standalone HTTP server that reads from the structured trade log (trades.jsonl)
and serves a single-page performance tracker. Does NOT import or interact with
the bot process at all — it only reads files.

Endpoints:
  GET /          → HTML dashboard page
  GET /api/stats → JSON performance data (auto-refreshes every 60s)
  GET /api/events → Raw JSONL events (for download/paste)
  GET /api/health → Server health check

Runs on port 8080 by default. Configurable via DASHBOARD_PORT env var.
Memory footprint: ~15MB. No external dependencies beyond Python stdlib.
"""

import json
import os
import sys
import csv
import http.server
import socketserver
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.resolve()))

PORT = int(os.environ.get("DASHBOARD_PORT", 8080))
TRADE_JSONL = os.path.join("live", "logs", "trades.jsonl")
TRADE_CSV = os.path.join("live", "trades.csv")
STATE_FILE = os.path.join("live", "state.json")
CONTROL_FILE = os.path.join("live", "logs", "bot_control.json")
ACTIVITY_FILE = os.path.join("live", "logs", "bot_activity.json")


def _read_state() -> dict:
    """Read current bot state (non-blocking, read-only)."""
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _read_events() -> list:
    """Read all JSONL events."""
    if not os.path.exists(TRADE_JSONL):
        return []
    events = []
    try:
        with open(TRADE_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        pass
    return events


def _match_trades(events: list) -> list:
    """Match ENTRY+EXIT events into completed trades."""
    entries = {}
    trades = []
    for ev in events:
        etype = ev.get("e")
        if etype == "ENTRY":
            entries[ev.get("sym", "")] = ev
        elif etype == "EXIT":
            sym = ev.get("sym", "")
            entry_ev = entries.pop(sym, None)
            trades.append({
                "symbol": sym,
                "session": ev.get("ses", ""),
                "direction": ev.get("dir", ""),
                "entry_price": ev.get("entry", 0),
                "close_price": ev.get("close", 0),
                "entry_time": entry_ev.get("ts", "") if entry_ev else "",
                "exit_time": ev.get("ts", ""),
                "fc_range_pct": entry_ev.get("fc_rng", 0) if entry_ev else ev.get("fc_rng", 0),
                "qty": entry_ev.get("qty", 0) if entry_ev else 0,
                "risk_pct": entry_ev.get("risk%", 0) if entry_ev else 0,
                "risk_usd": entry_ev.get("risk$", 0) if entry_ev else 0,
                "pair_class": entry_ev.get("cls", "") if entry_ev else ev.get("cls", ""),
                "equity_before": entry_ev.get("eq", 0) if entry_ev else 0,
                "equity_after": ev.get("eq", 0),
                "slip_r": entry_ev.get("slip_r", 0) if entry_ev else ev.get("slip_r", 0),
                "c2_body_ratio": entry_ev.get("c2_br", 0) if entry_ev else 0,
                "pnl_r": ev.get("pnl_r", 0),
                "pnl_usd": ev.get("pnl$", 0),
                "peak_r": ev.get("peak_r", 0),
                "exit_r": ev.get("exit_r", 0),
                "r_left_on_table": ev.get("left_r", 0),
                "duration_secs": ev.get("dur_s", 0),
                "exit_reason": ev.get("rsn", ""),
                "guardian_closed": ev.get("gc", False),
                "trail_was_active": ev.get("trail", False),
            })
    return trades


def _compute_stats(trades: list, state: dict) -> dict:
    """Compute comprehensive performance stats."""
    if not trades:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0, "expectancy_r": 0,
            "total_pnl_r": 0, "total_pnl_usd": 0,
            "profit_factor": 0, "avg_win_r": 0, "avg_loss_r": 0,
            "avg_peak_r": 0, "avg_r_left": 0,
            "best_r": 0, "worst_r": 0, "avg_duration_min": 0,
            "by_session": {}, "by_pair": {},
            "expected": _expected(), "equity_curve": [],
            "state": _safe_state(state), "trades": [],
            "pending": [],
        }

    total = len(trades)
    wins = [t for t in trades if t["pnl_r"] > 0]
    losses = [t for t in trades if t["pnl_r"] <= 0]
    n_w, n_l = len(wins), len(losses)

    total_pnl_r = sum(t["pnl_r"] for t in trades)
    total_pnl_usd = sum(t["pnl_usd"] for t in trades)
    gw = sum(t["pnl_r"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl_r"] for t in losses)) if losses else 0

    stats = {
        "total_trades": total,
        "wins": n_w,
        "losses": n_l,
        "win_rate": round(n_w / total * 100, 1),
        "expectancy_r": round(total_pnl_r / total, 4),
        "total_pnl_r": round(total_pnl_r, 3),
        "total_pnl_usd": round(total_pnl_usd, 2),
        "profit_factor": round(gw / gl, 2) if gl > 0 else 999,
        "avg_win_r": round(gw / n_w, 3) if n_w else 0,
        "avg_loss_r": round(gl / n_l, 3) if n_l else 0,
        "avg_peak_r": round(sum(t["peak_r"] for t in trades) / total, 3),
        "avg_r_left": round(sum(t["r_left_on_table"] for t in trades) / total, 3),
        "best_r": round(max(t["pnl_r"] for t in trades), 3),
        "worst_r": round(min(t["pnl_r"] for t in trades), 3),
        "avg_duration_min": round(sum(t["duration_secs"] for t in trades) / total / 60, 1),
    }

    # Per-session
    by_session = {}
    for ses in sorted(set(t["session"] for t in trades)):
        st = [t for t in trades if t["session"] == ses]
        sw = [t for t in st if t["pnl_r"] > 0]
        sp = sum(t["pnl_r"] for t in st)
        by_session[ses] = {
            "trades": len(st), "wins": len(sw),
            "wr": round(len(sw)/len(st)*100, 1) if st else 0,
            "pnl_r": round(sp, 3),
            "exp_r": round(sp/len(st), 4) if st else 0,
        }
    stats["by_session"] = by_session

    # Per-pair (sorted by pnl)
    by_pair = {}
    for sym in set(t["symbol"] for t in trades):
        st = [t for t in trades if t["symbol"] == sym]
        sw = [t for t in st if t["pnl_r"] > 0]
        sp = sum(t["pnl_r"] for t in st)
        by_pair[sym] = {
            "trades": len(st), "wins": len(sw),
            "wr": round(len(sw)/len(st)*100, 1) if st else 0,
            "pnl_r": round(sp, 3),
        }
    stats["by_pair"] = dict(sorted(by_pair.items(), key=lambda x: x[1]["pnl_r"], reverse=True))

    # Trail analysis
    trailed = [t for t in trades if t["trail_was_active"]]
    if trailed:
        stats["trail"] = {
            "count": len(trailed),
            "avg_peak_r": round(sum(t["peak_r"] for t in trailed)/len(trailed), 3),
            "avg_exit_r": round(sum(t["exit_r"] for t in trailed)/len(trailed), 3),
            "avg_left_r": round(sum(t["r_left_on_table"] for t in trailed)/len(trailed), 3),
            "pnl_r": round(sum(t["pnl_r"] for t in trailed), 3),
        }

    # Expected (backtest baseline)
    stats["expected"] = _expected()

    # Equity curve
    curve = []
    for t in trades:
        curve.append({
            "ts": t["exit_time"],
            "eq": t["equity_after"],
            "r": t["pnl_r"],
        })
    stats["equity_curve"] = curve

    # Recent trades (last 50)
    stats["trades"] = trades[-50:]

    # State info
    stats["state"] = _safe_state(state)

    # Pending positions
    stats["pending"] = state.get("pending_entries", [])

    # Bot control status
    stats["bot_status"] = _read_bot_status()

    # Bot activity (what the bot is doing right now)
    stats["activity"] = _read_activity()

    # Pair list from config
    stats["pair_list"] = _read_pair_list()

    # Full CSV trade log (last 100 rows)
    stats["csv_log"] = _read_trade_csv()

    return stats


def _expected() -> dict:
    """Backtest baseline from config (hardcoded to avoid importing config)."""
    return {
        "wr": 52.2,
        "exp_r": 0.237,
        "pf": 1.47,
        "avg_win_r": 1.418,
        "avg_loss_r": 1.053,
    }


def _safe_state(state: dict) -> dict:
    """Extract safe subset of state for dashboard display."""
    return {
        "equity": state.get("equity", 0),
        "total_trades": state.get("total_trades", 0),
        "total_pnl_r": state.get("total_pnl_r", 0),
        "wins_today": state.get("wins_today", 0),
        "losses_today": state.get("losses_today", 0),
        "pending": len(state.get("pending_entries", [])),
        "last_updated": state.get("last_updated", ""),
    }


def _read_bot_status() -> str:
    """Read bot status from the shared control file."""
    if not os.path.exists(CONTROL_FILE):
        return "running"  # Default: assume running
    try:
        with open(CONTROL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("status", "running")
    except Exception:
        return "unknown"


def _write_bot_command(command: str) -> dict:
    """Write a start/stop command to the control file for watchdog to read."""
    try:
        data = {}
        if os.path.exists(CONTROL_FILE):
            with open(CONTROL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data["command"] = command
        data["command_time"] = datetime.now(timezone.utc).isoformat()
        os.makedirs(os.path.dirname(CONTROL_FILE), exist_ok=True)
        with open(CONTROL_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return {"ok": True, "command": command}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _read_activity() -> dict:
    """Read bot activity status (what the bot is currently doing)."""
    if not os.path.exists(ACTIVITY_FILE):
        return {"phase": "UNKNOWN", "detail": "No activity data yet", "ts": ""}
    try:
        with open(ACTIVITY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Calculate staleness — if no update in 5 min, bot may be dead
        ts = data.get("ts", "")
        if ts:
            try:
                last = datetime.fromisoformat(ts)
                age_secs = (datetime.now(timezone.utc) - last).total_seconds()
                data["age_secs"] = round(age_secs)
                data["stale"] = age_secs > 300  # 5 minutes
            except Exception:
                data["age_secs"] = -1
                data["stale"] = True
        return data
    except Exception:
        return {"phase": "ERROR", "detail": "Could not read activity", "ts": ""}


def _read_pair_list() -> dict:
    """Read the configured pair list from live/config.py.
    
    Returns structured data: {session: [{pair, class}, ...]}
    """
    try:
        from live.config import PAIRS
        result = {}
        for session, pair_list in PAIRS.items():
            result[session] = [{"pair": p, "cls": c} for p, c in pair_list]
        return result
    except Exception:
        return {}


def _read_trade_csv(limit: int = 100) -> list:
    """Read the last N rows from trades.csv for the full trade log."""
    if not os.path.exists(TRADE_CSV):
        return []
    try:
        rows = []
        with open(TRADE_CSV, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        # Return last `limit` rows (most recent)
        return rows[-limit:]
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════
#  HTML DASHBOARD (single-page, no dependencies)
# ═══════════════════════════════════════════════════════════

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FCB Command Centre</title>
<style>
/* ─── Reset & Base ─── */
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0a0e1a;--surface:#111827;--surface2:#1a2235;--border:#1e293b;
  --text:#e2e8f0;--muted:#64748b;--accent:#3b82f6;--accent2:#6366f1;
  --green:#10b981;--green-dim:#064e3b;--red:#ef4444;--red-dim:#450a0a;
  --yellow:#f59e0b;--yellow-dim:#451a03;--cyan:#06b6d4;
  --glow:0 0 20px rgba(59,130,246,0.15);
  --radius:12px;--radius-sm:8px;
}
body{font-family:'Inter','Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}

/* ─── Header ─── */
.header{
  background:linear-gradient(135deg,#0f172a 0%,#1e1b4b 50%,#0f172a 100%);
  border-bottom:1px solid var(--border);
  padding:16px 24px;position:relative;overflow:hidden;
}
.header::before{
  content:'';position:absolute;top:0;left:0;right:0;bottom:0;
  background:radial-gradient(ellipse at 30% 50%,rgba(99,102,241,0.08) 0%,transparent 70%);
  pointer-events:none;
}
.header-inner{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;position:relative;z-index:1}
.header-left{display:flex;align-items:center;gap:14px}
.logo{
  width:36px;height:36px;border-radius:8px;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  display:flex;align-items:center;justify-content:center;
  font-weight:900;font-size:13px;color:#fff;letter-spacing:-0.5px;
}
.header-title{font-size:1.3em;font-weight:800;
  background:linear-gradient(135deg,#fff 0%,#a5b4fc 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text;letter-spacing:-0.5px;
}
.header-subtitle{color:var(--muted);font-size:0.75em;margin-top:1px}
.header-right{display:flex;align-items:center;gap:14px;flex-wrap:wrap}

/* ─── Next Session Hero Timer ─── */
.next-session-hero{
  text-align:center;padding:14px 24px;
  background:linear-gradient(180deg,rgba(99,102,241,0.06),transparent);
  border-bottom:1px solid var(--border);
}
.next-session-label{font-size:0.7em;text-transform:uppercase;letter-spacing:1.5px;color:var(--muted);font-weight:600;margin-bottom:4px}
.next-session-name{font-size:0.9em;font-weight:700;color:var(--accent);margin-bottom:2px}
.next-session-timer{
  font-size:2.4em;font-weight:900;font-variant-numeric:tabular-nums;
  background:linear-gradient(135deg,var(--accent),var(--cyan));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text;letter-spacing:1px;
}
.next-session-timer.active-now{
  background:linear-gradient(135deg,var(--green),var(--cyan));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text;
}

/* ─── Status Indicator ─── */
.status-badge{
  display:flex;align-items:center;gap:6px;
  padding:6px 12px;border-radius:20px;font-size:0.8em;font-weight:600;
  border:1px solid;transition:all 0.3s ease;
}
.status-badge.running{background:var(--green-dim);border-color:rgba(16,185,129,0.3);color:var(--green)}
.status-badge.stopped{background:var(--red-dim);border-color:rgba(239,68,68,0.3);color:var(--red)}
.pulse-dot{width:8px;height:8px;border-radius:50%;position:relative}
.pulse-dot.live{background:var(--green)}
.pulse-dot.live::after{
  content:'';position:absolute;top:-3px;left:-3px;width:14px;height:14px;
  border-radius:50%;background:var(--green);opacity:0;
  animation:pulse 2s infinite;
}
.pulse-dot.dead{background:var(--red)}
@keyframes pulse{0%{opacity:0.5;transform:scale(1)}100%{opacity:0;transform:scale(2.2)}}

/* ─── Toggle Switch ─── */
.toggle-container{display:flex;align-items:center;gap:8px}
.toggle-label{font-size:0.8em;font-weight:600;color:var(--muted)}
.toggle{position:relative;width:48px;height:24px;cursor:pointer}
.toggle input{opacity:0;width:0;height:0}
.toggle .slider{
  position:absolute;top:0;left:0;right:0;bottom:0;
  background:#374151;border-radius:24px;transition:all 0.3s cubic-bezier(0.4,0,0.2,1);
}
.toggle .slider::before{
  content:'';position:absolute;height:18px;width:18px;left:3px;bottom:3px;
  background:#fff;border-radius:50%;transition:all 0.3s cubic-bezier(0.4,0,0.2,1);
  box-shadow:0 2px 4px rgba(0,0,0,0.3);
}
.toggle input:checked+.slider{background:var(--green)}
.toggle input:checked+.slider::before{transform:translateX(24px)}

/* ─── Session Countdown Chips ─── */
.countdown-bar{display:flex;gap:6px;flex-wrap:wrap}
.session-chip{
  display:flex;align-items:center;gap:6px;
  padding:5px 10px;border-radius:6px;font-size:0.75em;
  background:var(--surface);border:1px solid var(--border);
  transition:all 0.3s ease;
}
.session-chip.active{border-color:var(--accent);background:rgba(59,130,246,0.1);box-shadow:0 0 10px rgba(59,130,246,0.1)}
.session-chip .name{font-weight:700;color:var(--muted)}
.session-chip.active .name{color:var(--accent)}
.session-chip .timer{font-variant-numeric:tabular-nums;font-weight:600;color:var(--text);min-width:50px}
.session-chip.active .timer{color:var(--accent)}

/* ─── Main Layout ─── */
.main-layout{display:grid;grid-template-columns:1fr 280px;gap:16px;max-width:1600px;margin:0 auto;padding:16px 20px}
@media(max-width:1100px){.main-layout{grid-template-columns:1fr}}

/* ─── Activity Status Bar ─── */
.activity-bar{
  display:flex;align-items:center;gap:12px;padding:10px 14px;
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  margin-bottom:12px;flex-wrap:wrap;
}
.activity-phase{
  display:flex;align-items:center;gap:6px;
  padding:3px 10px;border-radius:5px;font-weight:700;font-size:0.8em;
  text-transform:uppercase;letter-spacing:0.5px;
}
.activity-phase.CAPTURING,.activity-phase.SCANNING{background:rgba(59,130,246,0.15);color:var(--accent)}
.activity-phase.MONITORING{background:rgba(245,158,11,0.15);color:var(--yellow)}
.activity-phase.SLEEPING,.activity-phase.IDLE,.activity-phase.WAITING{background:rgba(100,116,139,0.15);color:var(--muted)}
.activity-phase.STARTING,.activity-phase.READY{background:rgba(16,185,129,0.15);color:var(--green)}
.activity-phase.UNKNOWN,.activity-phase.ERROR{background:var(--red-dim);color:var(--red)}
.activity-phase.OFFLINE{background:rgba(100,116,139,0.1);color:var(--muted)}
.activity-detail{color:var(--text);font-size:0.8em}
.activity-meta{margin-left:auto;display:flex;align-items:center;gap:12px;font-size:0.72em;color:var(--muted)}
.activity-meta .stale{color:var(--red)}

/* ─── Metrics Grid ─── */
.metrics-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:10px;margin-bottom:14px}
.metric-card{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:12px 14px;position:relative;overflow:hidden;transition:all 0.25s ease;
}
.metric-card:hover{border-color:rgba(99,102,241,0.3);transform:translateY(-1px);box-shadow:var(--glow)}
.metric-card .label{font-size:0.65em;text-transform:uppercase;letter-spacing:0.5px;color:var(--muted);font-weight:600;margin-bottom:4px}
.metric-card .value{font-size:1.4em;font-weight:800;letter-spacing:-0.5px}
.metric-card .sub{font-size:0.72em;color:var(--muted);margin-top:3px}
.metric-card::after{
  content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,transparent,var(--accent),transparent);opacity:0;transition:opacity 0.3s;
}
.metric-card:hover::after{opacity:1}

/* ─── Section Panels ─── */
.panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:12px;overflow:hidden}
.panel-header{
  padding:10px 14px;border-bottom:1px solid var(--border);
  display:flex;justify-content:space-between;align-items:center;
  background:linear-gradient(180deg,var(--surface2),var(--surface));
}
.panel-header h2{font-size:0.82em;font-weight:700;color:var(--text);letter-spacing:-0.2px}
.panel-header .badge{font-size:0.65em;padding:2px 7px;border-radius:5px;font-weight:600;background:rgba(99,102,241,0.15);color:var(--accent2)}
.panel-header .badge.red{background:var(--red-dim);color:var(--red)}
.panel-header .badge.green{background:var(--green-dim);color:var(--green)}
.panel-header .badge.yellow{background:var(--yellow-dim);color:var(--yellow)}
.panel-body{padding:12px 14px}

/* ─── Tables ─── */
table{width:100%;border-collapse:collapse;font-size:0.78em}
th{text-align:left;color:var(--muted);padding:6px 8px;font-weight:600;font-size:0.8em;text-transform:uppercase;letter-spacing:0.3px;border-bottom:1px solid var(--border)}
td{padding:6px 8px;border-bottom:1px solid rgba(30,41,59,0.5);transition:background 0.15s}
tr:hover td{background:rgba(99,102,241,0.04)}

/* ─── Colors ─── */
.green{color:var(--green)}.red{color:var(--red)}.yellow{color:var(--yellow)}.blue{color:var(--accent)}.cyan{color:var(--cyan)}.muted{color:var(--muted)}

/* ─── Tags ─── */
.tag{display:inline-block;padding:2px 7px;border-radius:5px;font-size:0.72em;font-weight:600}
.tag.win{background:var(--green-dim);color:var(--green)}
.tag.loss{background:var(--red-dim);color:var(--red)}
.tag.long{background:rgba(6,182,212,0.15);color:var(--cyan)}
.tag.short{background:rgba(245,158,11,0.15);color:var(--yellow)}
.tag.bot{background:rgba(59,130,246,0.15);color:var(--accent)}
.tag.ext{background:rgba(100,116,139,0.15);color:var(--muted)}
.tag.classA{background:var(--green-dim);color:var(--green)}
.tag.classB{background:var(--yellow-dim);color:var(--yellow)}
.tag.entry{background:rgba(6,182,212,0.12);color:var(--cyan)}
.tag.exit{background:rgba(245,158,11,0.12);color:var(--yellow)}

/* ─── Bar Chart ─── */
.bar-container{display:flex;align-items:center;gap:5px}
.bar{height:5px;border-radius:3px;min-width:2px;transition:width 0.5s ease}
.bar.pos{background:var(--green)}.bar.neg{background:var(--red)}

/* ─── Equity Chart ─── */
.eq-chart{position:relative;height:140px}
.eq-chart canvas{width:100%;height:100%}

/* ─── Grid Layouts ─── */
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:800px){.grid-2{grid-template-columns:1fr}}

/* ─── Right Sidebar ─── */
.sidebar{display:flex;flex-direction:column;gap:12px}
.sidebar .panel{margin-bottom:0}
.pair-list{max-height:320px;overflow-y:auto}
.pair-list::-webkit-scrollbar{width:4px}
.pair-list::-webkit-scrollbar-track{background:transparent}
.pair-list::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.pair-item{
  display:flex;justify-content:space-between;align-items:center;
  padding:4px 0;border-bottom:1px solid rgba(30,41,59,0.3);font-size:0.82em;
}
.pair-item:last-child{border-bottom:none}
.pair-item .pair-name{font-weight:600;color:var(--text)}
.session-group-label{
  font-size:0.7em;text-transform:uppercase;letter-spacing:0.8px;
  color:var(--muted);font-weight:700;padding:8px 0 4px;margin-top:4px;
  border-bottom:1px solid var(--border);
}
.session-group-label:first-child{margin-top:0}

/* ─── CSV Log Table ─── */
.csv-log-wrap{max-height:400px;overflow:auto}
.csv-log-wrap::-webkit-scrollbar{width:4px;height:4px}
.csv-log-wrap::-webkit-scrollbar-track{background:transparent}
.csv-log-wrap::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.csv-log-wrap table{min-width:900px}

/* ─── Footer ─── */
.footer{
  text-align:center;padding:12px;font-size:0.68em;color:var(--muted);
  border-top:1px solid var(--border);margin-top:4px;
}
.footer a{color:var(--accent);text-decoration:none}

/* ─── Animations ─── */
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.animate-in{animation:fadeIn 0.35s ease forwards}

/* ─── Refresh indicator ─── */
.refresh-ring{width:14px;height:14px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;display:none}
.refresh-ring.active{display:inline-block;animation:spin 0.8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* ─── Position source indicator ─── */
.src-indicator{display:inline-flex;align-items:center;gap:4px;font-size:0.7em;font-weight:600}
.src-indicator.bot-src{color:var(--accent)}
.src-indicator.ext-src{color:var(--muted)}
</style>
</head>
<body>

<!-- ─── HEADER ─── -->
<div class="header">
  <div class="header-inner">
    <div class="header-left">
      <div class="logo">FCB</div>
      <div>
        <div class="header-title">FCB Command Centre</div>
        <div class="header-subtitle">Guardian v3 Trail &middot; Bybit Mainnet &middot; 10x Leverage</div>
      </div>
    </div>
    <div class="header-right">
      <div class="countdown-bar" id="countdown-bar">
        <div class="session-chip" id="ses-asia"><span class="name">ASIA</span><span class="timer" id="timer-asia">--:--:--</span></div>
        <div class="session-chip" id="ses-london"><span class="name">LDN</span><span class="timer" id="timer-london">--:--:--</span></div>
        <div class="session-chip" id="ses-ny"><span class="name">NY</span><span class="timer" id="timer-ny">--:--:--</span></div>
      </div>
      <div class="status-badge running" id="status-badge">
        <div class="pulse-dot live" id="pulse-dot"></div>
        <span id="status-text">RUNNING</span>
      </div>
      <div class="toggle-container">
        <span class="toggle-label">Bot</span>
        <label class="toggle">
          <input type="checkbox" id="bot-toggle" checked>
          <span class="slider"></span>
        </label>
      </div>
      <div class="refresh-ring" id="refresh-ring"></div>
      <span style="font-size:0.68em;color:var(--muted)" id="lastUpdate">--:--:--</span>
    </div>
  </div>
</div>

<!-- ─── NEXT SESSION HERO TIMER ─── -->
<div class="next-session-hero">
  <div class="next-session-label">Next Trading Session</div>
  <div class="next-session-name" id="next-ses-name">--</div>
  <div class="next-session-timer" id="next-ses-timer">--:--:--</div>
</div>

<!-- ─── MAIN LAYOUT ─── -->
<div class="main-layout">

  <!-- LEFT COLUMN (main content) -->
  <div class="main-col">

    <!-- Activity Status Bar -->
    <div class="activity-bar animate-in" id="activity-bar">
      <div class="activity-phase OFFLINE" id="act-phase">OFFLINE</div>
      <div class="activity-detail" id="act-detail">Waiting for bot data...</div>
      <div class="activity-meta">
        <span id="act-session"></span>
        <span id="act-positions"></span>
        <span id="act-age"></span>
        <span id="act-uptime" style="font-size:0.9em"></span>
      </div>
    </div>

    <!-- Metrics -->
    <div class="metrics-grid" id="metrics">
      <div class="metric-card animate-in"><div class="label">Total Trades</div><div class="value" id="m-trades">-</div></div>
      <div class="metric-card animate-in"><div class="label">Win Rate</div><div class="value" id="m-wr">-</div><div class="sub" id="m-wl">-</div></div>
      <div class="metric-card animate-in"><div class="label">Expectancy</div><div class="value" id="m-exp">-</div></div>
      <div class="metric-card animate-in"><div class="label">Profit Factor</div><div class="value" id="m-pf">-</div></div>
      <div class="metric-card animate-in"><div class="label">Total P&amp;L (R)</div><div class="value" id="m-pnlr">-</div></div>
      <div class="metric-card animate-in"><div class="label">Total P&amp;L ($)</div><div class="value" id="m-pnl">-</div></div>
      <div class="metric-card animate-in"><div class="label">Equity</div><div class="value blue" id="m-eq">-</div></div>
      <div class="metric-card animate-in"><div class="label">Open Positions</div><div class="value yellow" id="m-pend">-</div></div>
    </div>

    <!-- Open Positions (always visible) -->
    <div class="panel animate-in" id="positions-panel">
      <div class="panel-header">
        <h2>Open Positions</h2>
        <span class="badge" id="open-count">0 OPEN</span>
      </div>
      <div class="panel-body">
        <table>
          <tr><th>Pair</th><th>Dir</th><th>Entry</th><th>SL</th><th>Session</th><th>Source</th><th>Opened</th></tr>
          <tbody id="positions-table">
            <tr><td colspan="7" class="muted" style="text-align:center;padding:16px">No open positions</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Full Trade Log (CSV) -->
    <div class="panel animate-in">
      <div class="panel-header"><h2>Trade Log</h2><span class="badge" id="csv-log-count">-</span></div>
      <div class="panel-body csv-log-wrap">
        <table>
          <tr><th>Time (UTC)</th><th>Pair</th><th>Session</th><th>Dir</th><th>Action</th><th>Price</th><th>Qty</th><th>SL</th><th>TP</th><th>Risk/U</th><th>Fee (R)</th><th>Eq Before</th><th>Eq After</th><th>Notes</th></tr>
          <tbody id="csv-log-table">
            <tr><td colspan="14" class="muted" style="text-align:center;padding:16px">No trade log data</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Equity Curve -->
    <div class="panel animate-in">
      <div class="panel-header"><h2>Equity Curve</h2><span class="badge" id="eq-range">-</span></div>
      <div class="panel-body">
        <div class="eq-chart"><canvas id="eqChart" height="140"></canvas></div>
      </div>
    </div>

    <!-- Performance vs Backtest -->
    <div class="panel animate-in">
      <div class="panel-header"><h2>Performance vs Backtest Baseline</h2><span class="badge">12,355 TRADES</span></div>
      <div class="panel-body">
        <table>
          <tr><th>Metric</th><th>Live</th><th>Expected</th><th>Delta</th></tr>
          <tr><td>Win Rate</td><td id="cmp-wr-a">-</td><td id="cmp-wr-e">-</td><td id="cmp-wr-d">-</td></tr>
          <tr><td>Expectancy (R)</td><td id="cmp-exp-a">-</td><td id="cmp-exp-e">-</td><td id="cmp-exp-d">-</td></tr>
          <tr><td>Profit Factor</td><td id="cmp-pf-a">-</td><td id="cmp-pf-e">-</td><td id="cmp-pf-d">-</td></tr>
          <tr><td>Avg Win (R)</td><td id="cmp-aw-a">-</td><td id="cmp-aw-e">-</td><td id="cmp-aw-d">-</td></tr>
          <tr><td>Avg Loss (R)</td><td id="cmp-al-a">-</td><td id="cmp-al-e">-</td><td id="cmp-al-d">-</td></tr>
        </table>
      </div>
    </div>

    <!-- Session + Trail -->
    <div class="grid-2">
      <div class="panel animate-in">
        <div class="panel-header"><h2>By Session</h2></div>
        <div class="panel-body">
          <table>
            <tr><th>Session</th><th>Trades</th><th>WR%</th><th>P&amp;L (R)</th><th>Exp (R)</th></tr>
            <tbody id="ses-table"></tbody>
          </table>
        </div>
      </div>
      <div class="panel animate-in">
        <div class="panel-header"><h2>Trail Performance</h2><span class="badge">GUARDIAN v3</span></div>
        <div class="panel-body">
          <table>
            <tr><th>Metric</th><th>Value</th></tr>
            <tbody id="trail-table"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- By Pair -->
    <div class="panel animate-in">
      <div class="panel-header"><h2>By Pair</h2><span class="badge" id="pair-count">-</span></div>
      <div class="panel-body">
        <table>
          <tr><th>Pair</th><th>Trades</th><th>WR%</th><th>P&amp;L (R)</th><th></th></tr>
          <tbody id="pair-table"></tbody>
        </table>
      </div>
    </div>

    <!-- Recent Trades (JSONL matched) -->
    <div class="panel animate-in">
      <div class="panel-header"><h2>Recent Trades</h2><span class="badge" id="trade-count">LAST 50</span></div>
      <div class="panel-body" style="overflow-x:auto">
        <table>
          <tr><th>Time</th><th>Pair</th><th>Dir</th><th>Session</th><th>P&amp;L (R)</th><th>Peak</th><th>R Left</th><th>Duration</th><th>Exit</th></tr>
          <tbody id="trade-table"></tbody>
        </table>
      </div>
    </div>

  </div><!-- /main-col -->

  <!-- RIGHT SIDEBAR -->
  <div class="sidebar">

    <!-- Configured Pairs by Class -->
    <div class="panel animate-in">
      <div class="panel-header"><h2>Listed Pairs</h2><span class="badge" id="total-pairs-badge">-</span></div>
      <div class="panel-body pair-list" id="pair-list-panel">
        <div class="muted" style="text-align:center;padding:12px;font-size:0.82em">Loading pairs...</div>
      </div>
    </div>

    <!-- Session Schedule -->
    <div class="panel animate-in">
      <div class="panel-header"><h2>Sessions (UTC)</h2></div>
      <div class="panel-body" style="font-size:0.82em">
        <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid rgba(30,41,59,0.3)">
          <span style="font-weight:600">Asia</span><span class="muted">00:00 &ndash; 08:00</span>
        </div>
        <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid rgba(30,41,59,0.3)">
          <span style="font-weight:600">London</span><span class="muted">08:00 &ndash; 16:00</span>
        </div>
        <div style="display:flex;justify-content:space-between;padding:4px 0">
          <span style="font-weight:600">New York</span><span class="muted">16:00 &ndash; 00:00</span>
        </div>
      </div>
    </div>

    <!-- Quick Links -->
    <div class="panel animate-in">
      <div class="panel-header"><h2>Quick Links</h2></div>
      <div class="panel-body" style="font-size:0.82em;display:flex;flex-direction:column;gap:6px">
        <a href="/api/stats" target="_blank" style="color:var(--accent);text-decoration:none">Stats API (JSON)</a>
        <a href="/api/events" target="_blank" style="color:var(--accent);text-decoration:none">Raw Events (JSONL)</a>
        <a href="/api/health" target="_blank" style="color:var(--accent);text-decoration:none">Health Check</a>
      </div>
    </div>

  </div><!-- /sidebar -->

</div><!-- /main-layout -->

<div class="footer">
  FCB Command Centre v3 &middot; Guardian v3 Trail &middot; Auto-refresh 30s
</div>

<script>
/* ─── Helpers ─── */
const $=id=>document.getElementById(id);
function fmt(v,d=2){return v!=null&&v!==''?Number(v).toFixed(d):'-'}
function pnlClass(v){return v>0?'green':v<0?'red':'muted'}
function delta(a,e){let d=a-e;return '<span class="'+pnlClass(d)+'">'+(d>=0?'+':'')+fmt(d,3)+'</span>'}

/* ─── Session Definitions ─── */
const SESSIONS=[
  {id:'asia',name:'Asia',h:0},
  {id:'london',name:'London',h:8},
  {id:'ny',name:'New York',h:16},
];

/* ─── Session Countdowns + Next Session Hero ─── */
function updateCountdowns(){
  const now=new Date();
  const utcH=now.getUTCHours(),utcM=now.getUTCMinutes(),utcS=now.getUTCSeconds();
  const nowTotal=utcH*3600+utcM*60+utcS;

  let nextName='',nextSecs=Infinity,currentSession='';

  SESSIONS.forEach((s,i)=>{
    const startSec=s.h*3600;
    const nextI=(i+1)%SESSIONS.length;
    const endSec=nextI===0?24*3600:SESSIONS[nextI].h*3600;

    // Time until this session starts
    let diff=startSec-nowTotal;
    if(diff<=0)diff+=86400;
    if(diff===86400)diff=86400; // exactly on the mark

    const el=$('timer-'+s.id);
    const chip=$('ses-'+s.id);
    const hh=Math.floor(diff/3600);
    const mm=Math.floor((diff%3600)/60);
    const ss=diff%60;
    el.textContent=(hh<10?'0':'')+hh+':'+(mm<10?'0':'')+mm+':'+(ss<10?'0':'')+ss;
    chip.classList.remove('active');

    // Check if currently in this session
    if(nowTotal>=startSec&&nowTotal<endSec){
      currentSession=s.name;
      chip.classList.add('active');
    }

    // Find next session
    if(diff<nextSecs&&diff>0){
      nextSecs=diff;
      nextName=s.name;
    }
  });

  // Hero timer
  const heroName=$('next-ses-name');
  const heroTimer=$('next-ses-timer');
  if(currentSession){
    // Currently in a session — show time until NEXT session
    heroName.textContent=currentSession+' (ACTIVE)';
    heroTimer.classList.add('active-now');
    if(nextSecs<Infinity){
      const hh=Math.floor(nextSecs/3600);
      const mm=Math.floor((nextSecs%3600)/60);
      const ss=nextSecs%60;
      heroTimer.textContent='Next: '+(hh<10?'0':'')+hh+':'+(mm<10?'0':'')+mm+':'+(ss<10?'0':'')+ss;
    }
  }else{
    heroName.textContent=nextName||'--';
    heroTimer.classList.remove('active-now');
    if(nextSecs<Infinity){
      const hh=Math.floor(nextSecs/3600);
      const mm=Math.floor((nextSecs%3600)/60);
      const ss=nextSecs%60;
      heroTimer.textContent=(hh<10?'0':'')+hh+':'+(mm<10?'0':'')+mm+':'+(ss<10?'0':'')+ss;
    }else{
      heroTimer.textContent='--:--:--';
    }
  }
}
setInterval(updateCountdowns,1000);
updateCountdowns();

/* ─── Bot Toggle ─── */
let botRunning=true;
const toggle=$('bot-toggle');
toggle.addEventListener('change',async function(){
  const action=this.checked?'start':'stop';
  try{
    const r=await fetch('/api/bot/'+action,{method:'POST'});
    const d=await r.json();
    updateBotStatus(action==='start');
  }catch(e){
    console.error('Toggle failed:',e);
    this.checked=!this.checked;
  }
});
function updateBotStatus(running){
  botRunning=running;
  const badge=$('status-badge');
  const dot=$('pulse-dot');
  const text=$('status-text');
  if(running){
    badge.className='status-badge running';
    dot.className='pulse-dot live';
    text.textContent='RUNNING';
  }else{
    badge.className='status-badge stopped';
    dot.className='pulse-dot dead';
    text.textContent='STOPPED';
  }
  toggle.checked=running;
}

/* ─── Equity Chart ─── */
function drawEquityCurve(curve){
  const canvas=$('eqChart');
  const ctx=canvas.getContext('2d');
  const dpr=window.devicePixelRatio||1;
  const rect=canvas.parentElement.getBoundingClientRect();
  canvas.width=rect.width*dpr;canvas.height=140*dpr;
  canvas.style.width=rect.width+'px';canvas.style.height='140px';
  ctx.scale(dpr,dpr);
  const W=rect.width,H=140;
  if(!curve||curve.length<2){
    ctx.fillStyle='#64748b';ctx.font='12px Inter,sans-serif';
    ctx.textAlign='center';ctx.fillText('Waiting for trade data...',W/2,H/2);
    return;
  }
  const eqs=curve.map(c=>c.eq);
  const mn=Math.min(...eqs),mx=Math.max(...eqs);
  const pad=10,range=mx-mn||1;
  ctx.strokeStyle='rgba(30,41,59,0.6)';ctx.lineWidth=0.5;
  for(let i=0;i<5;i++){const y=pad+(i/4)*(H-2*pad);ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(W-pad,y);ctx.stroke()}
  const grad=ctx.createLinearGradient(0,0,0,H);
  grad.addColorStop(0,'rgba(59,130,246,0.15)');grad.addColorStop(1,'rgba(59,130,246,0)');
  ctx.fillStyle=grad;ctx.beginPath();
  curve.forEach((c,i)=>{const x=pad+(i/(curve.length-1))*(W-2*pad);const y=H-pad-((c.eq-mn)/range)*(H-2*pad);i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)});
  const lastX=pad+((curve.length-1)/(curve.length-1))*(W-2*pad);
  ctx.lineTo(lastX,H-pad);ctx.lineTo(pad,H-pad);ctx.closePath();ctx.fill();
  ctx.strokeStyle='#3b82f6';ctx.lineWidth=2;ctx.beginPath();
  curve.forEach((c,i)=>{const x=pad+(i/(curve.length-1))*(W-2*pad);const y=H-pad-((c.eq-mn)/range)*(H-2*pad);i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)});
  ctx.stroke();
  const ly=H-pad-((eqs[eqs.length-1]-mn)/range)*(H-2*pad);
  ctx.fillStyle='#3b82f6';ctx.beginPath();ctx.arc(lastX,ly,4,0,Math.PI*2);ctx.fill();
  ctx.fillStyle='#fff';ctx.beginPath();ctx.arc(lastX,ly,2,0,Math.PI*2);ctx.fill();
  ctx.fillStyle='#64748b';ctx.font='10px Inter,sans-serif';ctx.textAlign='left';
  ctx.fillText('$'+fmt(mn,0),pad,H-2);
  ctx.textAlign='right';ctx.fillText('$'+fmt(mx,0),W-pad,12);
  $('eq-range').textContent='$'+fmt(mn,0)+' \\u2192 $'+fmt(mx,0);
}

/* ─── Render Open Positions ─── */
function renderPositions(pending){
  const tb=$('positions-table');
  const badge=$('open-count');
  if(!pending||pending.length===0){
    badge.textContent='0 OPEN';
    badge.className='badge';
    tb.innerHTML='<tr><td colspan="7" class="muted" style="text-align:center;padding:16px">No open positions</td></tr>';
    return;
  }
  badge.textContent=pending.length+' OPEN';
  badge.className='badge yellow';
  tb.innerHTML=pending.map(p=>{
    const sym=(p.symbol||'').replace('/USDT:USDT','');
    const isBot=p.session||p.entry_time; // bot-managed if it has session/entry_time data
    const srcTag=isBot?'<span class="tag bot">BOT</span>':'<span class="tag ext">EXTERNAL</span>';
    return '<tr>'+
      '<td><b>'+sym+'</b></td>'+
      '<td><span class="tag '+(p.direction==='long'?'long':'short')+'">'+(p.direction||'?')+'</span></td>'+
      '<td>'+fmt(p.entry_price,4)+'</td>'+
      '<td>'+fmt(p.sl,4)+'</td>'+
      '<td>'+(p.session||'-')+'</td>'+
      '<td>'+srcTag+'</td>'+
      '<td style="font-size:0.72em">'+(p.entry_time?p.entry_time.slice(11,19):'-')+'</td>'+
    '</tr>';
  }).join('');
}

/* ─── Render CSV Trade Log ─── */
function renderCsvLog(rows){
  const tb=$('csv-log-table');
  const badge=$('csv-log-count');
  if(!rows||rows.length===0){
    badge.textContent='NO DATA';
    tb.innerHTML='<tr><td colspan="14" class="muted" style="text-align:center;padding:16px">No trade log data</td></tr>';
    return;
  }
  badge.textContent=rows.length+' ROWS';
  // Reverse for most recent first
  const rev=rows.slice().reverse();
  tb.innerHTML=rev.map(r=>{
    const isEntry=(r.action||'').toUpperCase()==='ENTRY';
    const actionTag=isEntry?'<span class="tag entry">ENTRY</span>':'<span class="tag exit">EXIT</span>';
    const dirTag=r.direction==='long'?'<span class="tag long">L</span>':'<span class="tag short">S</span>';
    const sym=(r.symbol||'').replace('/USDT:USDT','');
    const ts=(r.timestamp_utc||'').replace('T',' ').slice(0,19);
    const eqBefore=r.equity_before?'$'+fmt(r.equity_before,2):'';
    const eqAfter=r.equity_after?'$'+fmt(r.equity_after,2):'';
    return '<tr>'+
      '<td style="font-size:0.72em;white-space:nowrap">'+ts+'</td>'+
      '<td><b>'+sym+'</b></td>'+
      '<td>'+(r.session||'')+'</td>'+
      '<td>'+dirTag+'</td>'+
      '<td>'+actionTag+'</td>'+
      '<td>'+fmt(r.price,4)+'</td>'+
      '<td>'+fmt(r.qty,4)+'</td>'+
      '<td>'+fmt(r.sl,4)+'</td>'+
      '<td>'+fmt(r.tp,4)+'</td>'+
      '<td>'+fmt(r.risk_per_unit,6)+'</td>'+
      '<td class="'+(Number(r.fee_r)<0?'red':'muted')+'">'+fmt(r.fee_r,4)+'</td>'+
      '<td>'+eqBefore+'</td>'+
      '<td>'+eqAfter+'</td>'+
      '<td style="font-size:0.72em;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+(r.notes||'')+'">'+(r.notes||'')+'</td>'+
    '</tr>';
  }).join('');
}

/* ─── Render Pair List (sidebar) ─── */
function renderPairList(pairList){
  const panel=$('pair-list-panel');
  const badge=$('total-pairs-badge');
  if(!pairList||Object.keys(pairList).length===0){
    panel.innerHTML='<div class="muted" style="text-align:center;padding:12px;font-size:0.82em">No pair data</div>';
    badge.textContent='-';
    return;
  }
  let totalPairs=0;
  let html='';
  const sessionOrder=['asia','london','ny'];
  const sessionLabels={asia:'Asia (00-08 UTC)',london:'London (08-16 UTC)',ny:'New York (16-00 UTC)'};
  sessionOrder.forEach(ses=>{
    const pairs=pairList[ses];
    if(!pairs||pairs.length===0)return;
    totalPairs+=pairs.length;
    html+='<div class="session-group-label">'+sessionLabels[ses]+' &mdash; '+pairs.length+' pairs</div>';
    // Sort: Class A first, then B
    const sorted=pairs.slice().sort((a,b)=>{if(a.cls===b.cls)return a.pair.localeCompare(b.pair);return a.cls==='A'?-1:1});
    sorted.forEach(p=>{
      const sym=p.pair.replace('/USDT:USDT','');
      const clsTag=p.cls==='A'?'<span class="tag classA">A</span>':'<span class="tag classB">B</span>';
      html+='<div class="pair-item"><span class="pair-name">'+sym+'</span>'+clsTag+'</div>';
    });
  });
  panel.innerHTML=html;
  badge.textContent=totalPairs+' PAIRS';
}

/* ─── Activity Bar ─── */
function updateActivity(act){
  if(!act)return;
  const phaseEl=$('act-phase');
  const detailEl=$('act-detail');
  const phase=act.phase||'OFFLINE';
  phaseEl.textContent=phase;
  phaseEl.className='activity-phase '+phase;
  detailEl.textContent=act.detail||'Waiting for bot data...';
  const sesEl=$('act-session');
  if(act.session){sesEl.textContent='Session: '+act.session.toUpperCase()}
  else if(act.next_session){sesEl.textContent='Next: '+act.next_session}
  else{sesEl.textContent=''}
  const posEl=$('act-positions');
  if(act.positions>0){posEl.textContent=act.positions+' pos';posEl.style.color='var(--yellow)'}
  else{posEl.textContent='';posEl.style.color='var(--muted)'}
  const ageEl=$('act-age');
  if(act.age_secs!=null&&act.age_secs>=0){
    if(act.stale){ageEl.innerHTML='<span class="stale">'+Math.floor(act.age_secs/60)+'m ago \\u2014 STALE</span>'}
    else if(act.age_secs<60){ageEl.textContent=act.age_secs+'s ago'}
    else{ageEl.textContent=Math.floor(act.age_secs/60)+'m ago'}
  }else{ageEl.textContent=''}
  const upEl=$('act-uptime');
  if(act.uptime_since){
    try{const start=new Date(act.uptime_since);const now=new Date();const diff=Math.floor((now-start)/1000);
    const h=Math.floor(diff/3600);const m=Math.floor((diff%3600)/60);
    upEl.textContent='Up '+(h>0?h+'h ':'')+m+'m'}catch(e){upEl.textContent=''}
  }else{upEl.textContent=''}
}

/* ─── Main Update ─── */
function update(data){
  $('lastUpdate').textContent=new Date().toLocaleTimeString();
  if(data.bot_status){updateBotStatus(data.bot_status==='running')}
  updateActivity(data.activity);

  // Metrics
  $('m-trades').textContent=data.total_trades||0;
  const wrEl=$('m-wr');
  wrEl.textContent=fmt(data.win_rate,1)+'%';
  wrEl.className='value '+(data.win_rate>=50?'green':'red');
  $('m-wl').textContent=(data.wins||0)+'W / '+(data.losses||0)+'L';
  const expEl=$('m-exp');
  expEl.textContent=fmt(data.expectancy_r,3)+'R';
  expEl.className='value '+pnlClass(data.expectancy_r);
  const pfEl=$('m-pf');
  pfEl.textContent=fmt(data.profit_factor,2);
  pfEl.className='value '+(data.profit_factor>=1?'green':'red');
  const pnlrEl=$('m-pnlr');
  pnlrEl.textContent=(data.total_pnl_r>=0?'+':'')+fmt(data.total_pnl_r,2)+'R';
  pnlrEl.className='value '+pnlClass(data.total_pnl_r);
  const pnlEl=$('m-pnl');
  pnlEl.textContent='$'+(data.total_pnl_usd>=0?'+':'')+fmt(data.total_pnl_usd,2);
  pnlEl.className='value '+pnlClass(data.total_pnl_usd);
  $('m-eq').textContent='$'+fmt(data.state?.equity||0,2);
  $('m-pend').textContent=data.state?.pending||0;

  // Expected vs Actual
  const e=data.expected||{};
  $('cmp-wr-a').innerHTML='<b>'+fmt(data.win_rate,1)+'%</b>';
  $('cmp-wr-e').textContent=fmt(e.wr,1)+'%';
  $('cmp-wr-d').innerHTML=delta(data.win_rate,e.wr);
  $('cmp-exp-a').innerHTML='<b>'+fmt(data.expectancy_r,3)+'R</b>';
  $('cmp-exp-e').textContent=fmt(e.exp_r,3)+'R';
  $('cmp-exp-d').innerHTML=delta(data.expectancy_r,e.exp_r);
  $('cmp-pf-a').innerHTML='<b>'+fmt(data.profit_factor,2)+'</b>';
  $('cmp-pf-e').textContent=fmt(e.pf,2);
  $('cmp-pf-d').innerHTML=delta(data.profit_factor,e.pf);
  $('cmp-aw-a').innerHTML='<b>'+fmt(data.avg_win_r,3)+'R</b>';
  $('cmp-aw-e').textContent=fmt(e.avg_win_r,3)+'R';
  $('cmp-aw-d').innerHTML=delta(data.avg_win_r,e.avg_win_r);
  $('cmp-al-a').innerHTML='<b>'+fmt(data.avg_loss_r,3)+'R</b>';
  $('cmp-al-e').textContent=fmt(e.avg_loss_r,3)+'R';
  $('cmp-al-d').innerHTML=delta(-data.avg_loss_r,-e.avg_loss_r);

  // Sessions
  const sesTb=$('ses-table');
  sesTb.innerHTML=Object.entries(data.by_session||{}).map(([s,v])=>'<tr>'+
    '<td><b>'+s+'</b></td><td>'+v.trades+'</td>'+
    '<td class="'+(v.wr>=50?'green':'red')+'">'+fmt(v.wr,1)+'%</td>'+
    '<td class="'+pnlClass(v.pnl_r)+'">'+fmt(v.pnl_r,2)+'R</td>'+
    '<td class="'+pnlClass(v.exp_r)+'">'+fmt(v.exp_r,3)+'R</td>'+
  '</tr>').join('');

  // Trail
  const trTb=$('trail-table');
  const tr=data.trail;
  if(tr){
    trTb.innerHTML=[
      ['Trailed Trades',tr.count],
      ['Avg Peak R',fmt(tr.avg_peak_r,3)+'R'],
      ['Avg Exit R',fmt(tr.avg_exit_r,3)+'R'],
      ['R Left on Table',fmt(tr.avg_left_r,3)+'R'],
      ['Trail P&L',fmt(tr.pnl_r,2)+'R'],
    ].map(([k,v])=>'<tr><td>'+k+'</td><td class="'+(String(v).includes('-')?'red':'green')+'"><b>'+v+'</b></td></tr>').join('');
  }else{
    trTb.innerHTML='<tr><td colspan="2" class="muted">No trailed trades yet</td></tr>';
  }

  // Pairs performance
  const pairTb=$('pair-table');
  const pairs=Object.entries(data.by_pair||{});
  $('pair-count').textContent=pairs.length+' PAIRS';
  const maxAbs=Math.max(...pairs.map(([,v])=>Math.abs(v.pnl_r)),1);
  pairTb.innerHTML=pairs.map(([sym,v])=>{
    const w=Math.abs(v.pnl_r)/maxAbs*100;
    const cls=v.pnl_r>=0?'pos':'neg';
    return '<tr>'+
      '<td><b>'+sym.replace('/USDT:USDT','')+'</b></td><td>'+v.trades+'</td>'+
      '<td class="'+(v.wr>=50?'green':'red')+'">'+fmt(v.wr,1)+'%</td>'+
      '<td class="'+pnlClass(v.pnl_r)+'"><b>'+fmt(v.pnl_r,2)+'R</b></td>'+
      '<td><div class="bar-container"><div class="bar '+cls+'" style="width:'+w+'%"></div></div></td>'+
    '</tr>';
  }).join('');

  // Recent trades (JSONL matched)
  const tradeTb=$('trade-table');
  const trades=(data.trades||[]).slice().reverse();
  $('trade-count').textContent='LAST '+trades.length;
  tradeTb.innerHTML=trades.map(t=>{
    const dur=t.duration_secs>0?(t.duration_secs/60).toFixed(0)+'m':'?';
    return '<tr>'+
      '<td style="font-size:0.72em">'+(t.exit_time?.slice(11,19)||'')+'</td>'+
      '<td><b>'+(t.symbol?.replace('/USDT:USDT','')||'')+'</b></td>'+
      '<td><span class="tag '+(t.direction==='long'?'long':'short')+'">'+(t.direction||'')+'</span></td>'+
      '<td>'+(t.session||'')+'</td>'+
      '<td class="'+pnlClass(t.pnl_r)+'"><b>'+fmt(t.pnl_r,3)+'R</b></td>'+
      '<td>'+fmt(t.peak_r,2)+'R</td>'+
      '<td class="yellow">'+fmt(t.r_left_on_table,2)+'R</td>'+
      '<td>'+dur+'</td>'+
      '<td><span class="tag '+(t.pnl_r>0?'win':'loss')+'">'+(t.exit_reason||'')+'</span></td>'+
    '</tr>';
  }).join('');

  // Equity curve
  drawEquityCurve(data.equity_curve);

  // Open positions
  renderPositions(data.pending);

  // CSV trade log
  renderCsvLog(data.csv_log);

  // Pair list sidebar
  renderPairList(data.pair_list);
}

/* ─── Refresh Logic ─── */
let refreshTimer;
async function refresh(){
  const ring=$('refresh-ring');
  ring.classList.add('active');
  try{
    const r=await fetch('/api/stats');
    const d=await r.json();
    update(d);
  }catch(e){
    console.error('Refresh failed:',e);
  }finally{
    ring.classList.remove('active');
  }
}
refresh();
refreshTimer=setInterval(refresh,30000);
</script>
</body>
</html>"""


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Simple HTTP handler for the dashboard."""

    def log_message(self, format, *args):
        """Suppress default request logging to keep output clean."""
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._send_html(DASHBOARD_HTML)

        elif path == "/api/stats":
            events = _read_events()
            trades = _match_trades(events)
            state = _read_state()
            stats = _compute_stats(trades, state)
            self._send_json(stats)

        elif path == "/api/events":
            # Raw JSONL for copy-paste analysis
            if os.path.exists(TRADE_JSONL):
                with open(TRADE_JSONL, "r", encoding="utf-8") as f:
                    self._send_text(f.read())
            else:
                self._send_text("")

        elif path == "/api/health":
            self._send_json({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})

        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/bot/start":
            result = _write_bot_command("start")
            self._send_json(result)

        elif path == "/api/bot/stop":
            result = _write_bot_command("stop")
            self._send_json(result)

        else:
            self.send_error(404)


def main():
    print(f"FCB Dashboard starting on port {PORT}...")
    print(f"  Data: {TRADE_JSONL}")
    print(f"  State: {STATE_FILE}")
    print(f"  URL: http://0.0.0.0:{PORT}/")

    with socketserver.TCPServer(("0.0.0.0", PORT), DashboardHandler) as httpd:
        httpd.allow_reuse_address = True
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
