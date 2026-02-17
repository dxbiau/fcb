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
import http.server
import socketserver
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.resolve()))

PORT = int(os.environ.get("DASHBOARD_PORT", 8080))
TRADE_JSONL = os.path.join("live", "logs", "trades.jsonl")
STATE_FILE = os.path.join("live", "state.json")


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


# ═══════════════════════════════════════════════════════════
#  HTML DASHBOARD (single-page, no dependencies)
# ═══════════════════════════════════════════════════════════

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FCB Performance Tracker</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;padding:16px}
h1{color:#58a6ff;margin-bottom:4px;font-size:1.4em}
.subtitle{color:#8b949e;margin-bottom:16px;font-size:0.85em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px}
.card h3{color:#8b949e;font-size:0.75em;text-transform:uppercase;margin-bottom:4px}
.card .val{font-size:1.5em;font-weight:700}
.green{color:#3fb950}.red{color:#f85149}.yellow{color:#d29922}.blue{color:#58a6ff}
.section{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin-bottom:16px}
.section h2{color:#58a6ff;font-size:1em;margin-bottom:10px;border-bottom:1px solid #30363d;padding-bottom:6px}
table{width:100%;border-collapse:collapse;font-size:0.85em}
th{text-align:left;color:#8b949e;padding:6px 8px;border-bottom:1px solid #30363d;font-weight:600}
td{padding:6px 8px;border-bottom:1px solid #21262d}
tr:hover td{background:#1c2128}
.tag{display:inline-block;padding:2px 6px;border-radius:4px;font-size:0.75em;font-weight:600}
.tag.win{background:#1a3a2a;color:#3fb950}.tag.loss{background:#3a1a1a;color:#f85149}
.exp-row{background:#1c2128}
.exp-row td{color:#d29922;font-style:italic}
.bar-container{display:flex;align-items:center;gap:8px}
.bar{height:8px;border-radius:4px;min-width:2px}
.bar.pos{background:#3fb950}.bar.neg{background:#f85149}
.pending-badge{background:#d29922;color:#0d1117;padding:2px 8px;border-radius:12px;font-weight:700;font-size:0.8em}
.status{text-align:center;padding:4px;font-size:0.75em;color:#8b949e;margin-top:8px}
.eq-chart{width:100%;height:120px;position:relative;margin-top:8px}
.eq-chart canvas{width:100%;height:100%}
#lastUpdate{color:#8b949e;font-size:0.75em}
.compare{display:grid;grid-template-columns:1fr 1fr;gap:2px}
.compare .label{color:#8b949e;font-size:0.8em}
.compare .actual{font-weight:700}
.compare .expected{color:#d29922;font-size:0.85em}
</style>
</head>
<body>
<h1>FCB Performance Tracker</h1>
<p class="subtitle">Guardian v3 Trail | Live on Bybit | Auto-refreshes 60s | <span id="lastUpdate">loading...</span></p>

<div class="grid" id="metrics">
  <div class="card"><h3>Total Trades</h3><div class="val" id="m-trades">-</div></div>
  <div class="card"><h3>Win Rate</h3><div class="val" id="m-wr">-</div></div>
  <div class="card"><h3>Expectancy</h3><div class="val" id="m-exp">-</div></div>
  <div class="card"><h3>Profit Factor</h3><div class="val" id="m-pf">-</div></div>
  <div class="card"><h3>Total P&L (R)</h3><div class="val" id="m-pnlr">-</div></div>
  <div class="card"><h3>Total P&L ($)</h3><div class="val" id="m-pnl">-</div></div>
  <div class="card"><h3>Equity</h3><div class="val blue" id="m-eq">-</div></div>
  <div class="card"><h3>Pending</h3><div class="val yellow" id="m-pend">-</div></div>
</div>

<div class="section">
  <h2>Expected vs Actual</h2>
  <table>
    <tr><th>Metric</th><th>Actual</th><th>Expected (Backtest)</th><th>Delta</th></tr>
    <tr><td>Win Rate</td><td id="cmp-wr-a">-</td><td id="cmp-wr-e">-</td><td id="cmp-wr-d">-</td></tr>
    <tr><td>Expectancy (R)</td><td id="cmp-exp-a">-</td><td id="cmp-exp-e">-</td><td id="cmp-exp-d">-</td></tr>
    <tr><td>Profit Factor</td><td id="cmp-pf-a">-</td><td id="cmp-pf-e">-</td><td id="cmp-pf-d">-</td></tr>
    <tr><td>Avg Win (R)</td><td id="cmp-aw-a">-</td><td id="cmp-aw-e">-</td><td id="cmp-aw-d">-</td></tr>
    <tr><td>Avg Loss (R)</td><td id="cmp-al-a">-</td><td id="cmp-al-e">-</td><td id="cmp-al-d">-</td></tr>
  </table>
</div>

<div class="section">
  <h2>Equity Curve</h2>
  <canvas id="eqChart" height="140"></canvas>
</div>

<div class="grid" style="grid-template-columns:1fr 1fr">
  <div class="section">
    <h2>By Session</h2>
    <table>
      <tr><th>Session</th><th>Trades</th><th>WR%</th><th>P&L (R)</th><th>Exp (R)</th></tr>
      <tbody id="ses-table"></tbody>
    </table>
  </div>
  <div class="section">
    <h2>Trail Performance</h2>
    <table>
      <tr><th>Metric</th><th>Value</th></tr>
      <tbody id="trail-table"></tbody>
    </table>
  </div>
</div>

<div class="section">
  <h2>By Pair (sorted by P&L)</h2>
  <table>
    <tr><th>Pair</th><th>Trades</th><th>WR%</th><th>P&L (R)</th><th>Bar</th></tr>
    <tbody id="pair-table"></tbody>
  </table>
</div>

<div class="section">
  <h2>Recent Trades (last 50)</h2>
  <table>
    <tr><th>Time</th><th>Pair</th><th>Dir</th><th>Session</th><th>P&L (R)</th><th>Peak</th><th>Left</th><th>Duration</th><th>Reason</th></tr>
    <tbody id="trade-table"></tbody>
  </table>
</div>

<div class="section" id="pending-section" style="display:none">
  <h2>Open Positions</h2>
  <table>
    <tr><th>Pair</th><th>Dir</th><th>Entry</th><th>SL</th><th>Session</th><th>Opened</th></tr>
    <tbody id="pending-table"></tbody>
  </table>
</div>

<div class="status">Data source: live/logs/trades.jsonl | Dashboard does not interact with bot</div>

<script>
function fmt(v,d=2){return v!=null?Number(v).toFixed(d):'-'}
function pnlClass(v){return v>0?'green':v<0?'red':''}
function delta(a,e){let d=a-e;return `<span class="${pnlClass(d)}">${d>=0?'+':''}${fmt(d,3)}</span>`}

function drawEquityCurve(curve){
  const canvas=document.getElementById('eqChart');
  const ctx=canvas.getContext('2d');
  canvas.width=canvas.offsetWidth*2;canvas.height=280;
  ctx.scale(2,2);
  const W=canvas.offsetWidth,H=140;
  if(!curve||curve.length<2){ctx.fillStyle='#8b949e';ctx.fillText('Not enough data',W/2-40,H/2);return}
  const eqs=curve.map(c=>c.eq);
  const mn=Math.min(...eqs),mx=Math.max(...eqs);
  const pad=10,range=mx-mn||1;
  ctx.strokeStyle='#58a6ff';ctx.lineWidth=1.5;ctx.beginPath();
  curve.forEach((c,i)=>{
    const x=pad+(i/(curve.length-1))*(W-2*pad);
    const y=H-pad-((c.eq-mn)/range)*(H-2*pad);
    i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);
  });
  ctx.stroke();
  // Start/end labels
  ctx.fillStyle='#8b949e';ctx.font='11px sans-serif';
  ctx.fillText('$'+fmt(eqs[0],0),pad,H-2);
  ctx.fillText('$'+fmt(eqs[eqs.length-1],0),W-60,H-2);
}

function renderPending(pending){
  const sec=document.getElementById('pending-section');
  const tb=document.getElementById('pending-table');
  if(!pending||pending.length===0){sec.style.display='none';return}
  sec.style.display='block';
  tb.innerHTML=pending.map(p=>`<tr>
    <td>${p.symbol||''}</td>
    <td>${p.direction||''}</td>
    <td>${fmt(p.entry_price,6)}</td>
    <td>${fmt(p.sl,6)}</td>
    <td>${p.session||''}</td>
    <td>${p.entry_time||''}</td>
  </tr>`).join('');
}

function update(data){
  document.getElementById('lastUpdate').textContent=new Date().toLocaleTimeString();
  // Metrics
  document.getElementById('m-trades').textContent=data.total_trades;
  const wrEl=document.getElementById('m-wr');
  wrEl.textContent=fmt(data.win_rate,1)+'%';
  wrEl.className='val '+(data.win_rate>=50?'green':'red');
  const expEl=document.getElementById('m-exp');
  expEl.textContent=fmt(data.expectancy_r,3)+'R';
  expEl.className='val '+pnlClass(data.expectancy_r);
  const pfEl=document.getElementById('m-pf');
  pfEl.textContent=fmt(data.profit_factor,2);
  pfEl.className='val '+(data.profit_factor>=1?'green':'red');
  const pnlrEl=document.getElementById('m-pnlr');
  pnlrEl.textContent=(data.total_pnl_r>=0?'+':'')+fmt(data.total_pnl_r,2)+'R';
  pnlrEl.className='val '+pnlClass(data.total_pnl_r);
  const pnlEl=document.getElementById('m-pnl');
  pnlEl.textContent='$'+(data.total_pnl_usd>=0?'+':'')+fmt(data.total_pnl_usd,2);
  pnlEl.className='val '+pnlClass(data.total_pnl_usd);
  document.getElementById('m-eq').textContent='$'+fmt(data.state?.equity||0,2);
  document.getElementById('m-pend').textContent=data.state?.pending||0;

  // Expected vs Actual
  const e=data.expected||{};
  document.getElementById('cmp-wr-a').innerHTML=`<b>${fmt(data.win_rate,1)}%</b>`;
  document.getElementById('cmp-wr-e').textContent=fmt(e.wr,1)+'%';
  document.getElementById('cmp-wr-d').innerHTML=delta(data.win_rate,e.wr);
  document.getElementById('cmp-exp-a').innerHTML=`<b>${fmt(data.expectancy_r,3)}R</b>`;
  document.getElementById('cmp-exp-e').textContent=fmt(e.exp_r,3)+'R';
  document.getElementById('cmp-exp-d').innerHTML=delta(data.expectancy_r,e.exp_r);
  document.getElementById('cmp-pf-a').innerHTML=`<b>${fmt(data.profit_factor,2)}</b>`;
  document.getElementById('cmp-pf-e').textContent=fmt(e.pf,2);
  document.getElementById('cmp-pf-d').innerHTML=delta(data.profit_factor,e.pf);
  document.getElementById('cmp-aw-a').innerHTML=`<b>${fmt(data.avg_win_r,3)}R</b>`;
  document.getElementById('cmp-aw-e').textContent=fmt(e.avg_win_r,3)+'R';
  document.getElementById('cmp-aw-d').innerHTML=delta(data.avg_win_r,e.avg_win_r);
  document.getElementById('cmp-al-a').innerHTML=`<b>${fmt(data.avg_loss_r,3)}R</b>`;
  document.getElementById('cmp-al-e').textContent=fmt(e.avg_loss_r,3)+'R';
  document.getElementById('cmp-al-d').innerHTML=delta(-data.avg_loss_r,-e.avg_loss_r);

  // Sessions
  const sesTb=document.getElementById('ses-table');
  sesTb.innerHTML=Object.entries(data.by_session||{}).map(([s,v])=>`<tr>
    <td><b>${s}</b></td><td>${v.trades}</td>
    <td class="${v.wr>=50?'green':'red'}">${fmt(v.wr,1)}%</td>
    <td class="${pnlClass(v.pnl_r)}">${fmt(v.pnl_r,2)}R</td>
    <td class="${pnlClass(v.exp_r)}">${fmt(v.exp_r,3)}R</td>
  </tr>`).join('');

  // Trail
  const trTb=document.getElementById('trail-table');
  const tr=data.trail;
  if(tr){
    trTb.innerHTML=[
      ['Trailed Trades',tr.count],
      ['Avg Peak R',fmt(tr.avg_peak_r,3)+'R'],
      ['Avg Exit R',fmt(tr.avg_exit_r,3)+'R'],
      ['Avg R Left on Table',fmt(tr.avg_left_r,3)+'R'],
      ['Trail P&L',fmt(tr.pnl_r,2)+'R'],
    ].map(([k,v])=>`<tr><td>${k}</td><td>${v}</td></tr>`).join('');
  } else {
    trTb.innerHTML='<tr><td colspan="2" style="color:#8b949e">No trailed trades yet</td></tr>';
  }

  // Pairs
  const pairTb=document.getElementById('pair-table');
  const pairs=Object.entries(data.by_pair||{});
  const maxAbs=Math.max(...pairs.map(([,v])=>Math.abs(v.pnl_r)),1);
  pairTb.innerHTML=pairs.map(([sym,v])=>{
    const w=Math.abs(v.pnl_r)/maxAbs*100;
    const cls=v.pnl_r>=0?'pos':'neg';
    return `<tr>
      <td>${sym.replace('/USDT:USDT','')}</td><td>${v.trades}</td>
      <td class="${v.wr>=50?'green':'red'}">${fmt(v.wr,1)}%</td>
      <td class="${pnlClass(v.pnl_r)}">${fmt(v.pnl_r,2)}R</td>
      <td><div class="bar-container"><div class="bar ${cls}" style="width:${w}%"></div></div></td>
    </tr>`;
  }).join('');

  // Trades
  const tradeTb=document.getElementById('trade-table');
  tradeTb.innerHTML=(data.trades||[]).reverse().map(t=>{
    const dur=t.duration_secs>0?(t.duration_secs/60).toFixed(0)+'m':'?';
    return `<tr>
      <td style="font-size:0.75em">${t.exit_time?.slice(11,19)||''}</td>
      <td>${t.symbol?.replace('/USDT:USDT','')||''}</td>
      <td>${t.direction||''}</td>
      <td>${t.session||''}</td>
      <td class="${pnlClass(t.pnl_r)}"><b>${fmt(t.pnl_r,3)}R</b></td>
      <td>${fmt(t.peak_r,2)}R</td>
      <td class="yellow">${fmt(t.r_left_on_table,2)}R</td>
      <td>${dur}</td>
      <td><span class="tag ${t.pnl_r>0?'win':'loss'}">${t.exit_reason||''}</span></td>
    </tr>`;
  }).join('');

  // Equity curve
  drawEquityCurve(data.equity_curve);

  // Pending positions
  renderPending(data.pending);
}

function refresh(){
  fetch('/api/stats')
    .then(r=>r.json())
    .then(update)
    .catch(e=>console.error('Refresh failed:',e));
}

refresh();
setInterval(refresh,60000);
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
