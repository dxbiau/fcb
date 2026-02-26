"""
preflight.py -- Pre-flight verification for v13pro bot.

Run BEFORE and AFTER every code change to catch regressions.
Exit code 0 = all checks pass.  Non-zero = problems found.

Usage:  python preflight.py
"""

import json, sys, time, importlib, os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
V13 = ROOT / "v13pro"
LOGS = V13 / "logs"
STATE = V13 / "state.json"

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"

errors = []
warnings = []
info = []

def fail(msg):
    errors.append(msg)
    print(f"  {RED}FAIL{RESET}  {msg}")

def warn(msg):
    warnings.append(msg)
    print(f"  {YELLOW}WARN{RESET}  {msg}")

def ok(msg):
    info.append(msg)
    print(f"  {GREEN} OK {RESET}  {msg}")

# ============================================================
#  1. MODULE IMPORTS
# ============================================================
print(f"\n{BOLD}[1] Module Imports{RESET}")
modules = [
    "v13pro.config", "v13pro.bot", "v13pro.guardian", "v13pro.state",
    "v13pro.exchange", "v13pro.registry", "v13pro.shadow",
    "v13pro.signal_quality", "v13pro.skill",
]
for mod in modules:
    try:
        importlib.import_module(mod)
        ok(f"{mod}")
    except Exception as e:
        fail(f"{mod}: {e}")

# ============================================================
#  2. CONFIG SANITY (the x10 goal checks)
# ============================================================
print(f"\n{BOLD}[2] Config Sanity (x10 goal){RESET}")
from v13pro import config as cfg

# CRITICAL: Nothing should hard-block trades
if cfg.MAX_TRADES_DAY < 500:
    fail(f"MAX_TRADES_DAY = {cfg.MAX_TRADES_DAY} -- TOO LOW, will block trades!")
else:
    ok(f"MAX_TRADES_DAY = {cfg.MAX_TRADES_DAY} (unlimited)")

if cfg.DAILY_GROWTH_CAP_PCT > 0 and cfg.DAILY_GROWTH_CAP_PCT < 50:
    fail(f"DAILY_GROWTH_CAP_PCT = {cfg.DAILY_GROWTH_CAP_PCT}% -- will cap profit days!")
else:
    ok(f"DAILY_GROWTH_CAP_PCT = {cfg.DAILY_GROWTH_CAP_PCT} (no cap)")

if cfg.MAX_CONCURRENT_POSITIONS < 3:
    fail(f"MAX_CONCURRENT_POSITIONS = {cfg.MAX_CONCURRENT_POSITIONS} -- too few!")
else:
    ok(f"MAX_CONCURRENT_POSITIONS = {cfg.MAX_CONCURRENT_POSITIONS}")

if cfg.PAIR_COOLDOWN_MINUTES > 60:
    warn(f"PAIR_COOLDOWN_MINUTES = {cfg.PAIR_COOLDOWN_MINUTES} -- long cooldown slows trading")
else:
    ok(f"PAIR_COOLDOWN_MINUTES = {cfg.PAIR_COOLDOWN_MINUTES}")

if cfg.HUNTER_MAX_POSITIONS < 3:
    fail(f"HUNTER_MAX_POSITIONS = {cfg.HUNTER_MAX_POSITIONS} -- too few hunter slots!")
else:
    ok(f"HUNTER_MAX_POSITIONS = {cfg.HUNTER_MAX_POSITIONS}")

# Leverage / risk
if cfg.LEVERAGE < 5:
    warn(f"LEVERAGE = {cfg.LEVERAGE} -- low leverage slows x10")
else:
    ok(f"LEVERAGE = {cfg.LEVERAGE}")

risk_pct = cfg.RISK_PCT * 100
if risk_pct < 1.0:
    warn(f"RISK_PCT = {risk_pct:.1f}% -- conservative risk")
else:
    ok(f"RISK_PCT = {risk_pct:.1f}%")

# Long-only check
if hasattr(cfg, 'LONG_ONLY_MODE') and cfg.LONG_ONLY_MODE:
    ok("LONG_ONLY_MODE = True (protecting from bad shorts)")
else:
    warn("LONG_ONLY_MODE not set or False")

# Maker entry/TP
if cfg.MAKER_ENTRY_ENABLED:
    ok("MAKER_ENTRY_ENABLED = True (limit orders, better fills)")
if cfg.MAKER_TP_ENABLED:
    ok("MAKER_TP_ENABLED = True (limit TPs, better exits)")

# Live combos check
if hasattr(cfg, 'LIVE_COMBOS') and cfg.LIVE_COMBOS:
    ok(f"LIVE_COMBOS = {len(cfg.LIVE_COMBOS)} combos approved for live trading")
    # Check that portfolio combos are mostly in LIVE_COMBOS
    try:
        import json as _j
        deploy = _j.load(open(cfg.DEPLOY_COMBOS))
        from v13pro.registry import _normalise_tf
        missing = []
        for c in deploy:
            key = (c['strat'], _normalise_tf(c['tf']))
            if key not in cfg.LIVE_COMBOS:
                missing.append(f"{c['strat']}/{c['tf']}")
        if missing:
            unique_missing = sorted(set(missing))
            warn(f"  Portfolio combos NOT in LIVE_COMBOS (shadow-only): {', '.join(unique_missing)}")
        else:
            ok(f"  All portfolio combos are in LIVE_COMBOS")
    except Exception as e:
        warn(f"  Could not cross-check deploy combos: {e}")
else:
    warn("LIVE_COMBOS empty — all passed signals trade live (no shadow gate)")

# ============================================================
#  3. STATE FILE HEALTH
# ============================================================
print(f"\n{BOLD}[3] State File Health{RESET}")
if not STATE.exists():
    fail("state.json missing!")
else:
    with open(STATE) as f:
        state = json.load(f)

    # Stale pending entries
    pending = state.get("pending_entries", [])
    if pending:
        now_utc = datetime.now(timezone.utc)
        for p in pending:
            sym = p.get("symbol", "?")
            ts = p.get("entry_time", "")
            try:
                entry_dt = datetime.fromisoformat(ts)
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                age_h = (now_utc - entry_dt).total_seconds() / 3600
                if age_h > 4:
                    fail(f"Stale pending entry: {sym} (age={age_h:.1f}h) -- ghost position!")
                else:
                    ok(f"Pending: {sym} (age={age_h:.1f}h)")
            except Exception:
                warn(f"Pending: {sym} (can't parse time: {ts})")
    else:
        ok("No pending entries (clean)")

    # entries_today vs reasonable
    entries = state.get("entries_today", 0)
    ok(f"entries_today = {entries}")

    # Consecutive losses -- flag extreme
    consec = state.get("consecutive_losses", {})
    extreme = {k: v for k, v in consec.items() if v >= 5}
    if extreme:
        for k, v in extreme.items():
            warn(f"consecutive_losses[{k}] = {v} (high -- may be cooldown-locked)")
    else:
        ok(f"consecutive_losses: no extreme values (max={max(consec.values()) if consec else 0})")

    # Equity
    eq = state.get("equity", 0)
    day_start = state.get("day_start_equity", 0)
    if eq > 0:
        ok(f"Equity: ${eq:.2f}  (day_start: ${day_start:.2f})")
    else:
        warn("Equity = 0 in state")

# ============================================================
#  4. GUARDIAN CHECKS
# ============================================================
print(f"\n{BOLD}[4] Guardian Config{RESET}")
from v13pro.guardian import Guardian
if hasattr(Guardian, 'MIN_GRACE_SECS'):
    grace = Guardian.MIN_GRACE_SECS
    if grace < 30:
        warn(f"Guardian.MIN_GRACE_SECS = {grace} -- very short, risk of ghost exits")
    else:
        ok(f"Guardian.MIN_GRACE_SECS = {grace}")
else:
    fail("Guardian.MIN_GRACE_SECS not defined -- ghost exit bug may be present!")

# ============================================================
#  5. EXIT MODE REGISTRY
# ============================================================
print(f"\n{BOLD}[5] Exit Mode Registry{RESET}")
from v13pro.registry import EXIT_PARAMS
from v13pro.config import HUNTER_EXIT_MAP, HUNTER_EXIT_DEFAULT

# Check default exists
if HUNTER_EXIT_DEFAULT not in EXIT_PARAMS:
    fail(f"HUNTER_EXIT_DEFAULT '{HUNTER_EXIT_DEFAULT}' not in EXIT_PARAMS!")
else:
    ok(f"Default exit: {HUNTER_EXIT_DEFAULT} = {EXIT_PARAMS[HUNTER_EXIT_DEFAULT]}")

# Check all mapped exits exist
for key, mode in HUNTER_EXIT_MAP.items():
    if mode not in EXIT_PARAMS:
        fail(f"HUNTER_EXIT_MAP['{key}'] = '{mode}' -- NOT in EXIT_PARAMS!")
ok(f"HUNTER_EXIT_MAP: {len(HUNTER_EXIT_MAP)} entries, all valid")

# ============================================================
#  6. SIGNAL QUALITY ENGINE
# ============================================================
print(f"\n{BOLD}[6] Signal Quality Engine{RESET}")
try:
    from v13pro.signal_quality import SignalQualityEngine
    sq = SignalQualityEngine()
    combos = len(sq._stats) if hasattr(sq, '_stats') else 0
    total = len(sq._outcomes) if hasattr(sq, '_outcomes') else 0
    if total == 0:
        warn("SignalQuality: 0 outcomes loaded -- no shadow data?")
    elif total < 100:
        warn(f"SignalQuality: only {total} outcomes ({combos} combos) -- thin data")
    else:
        ok(f"SignalQuality: {total} outcomes, {combos} combos, WR={sq._global_wr*100:.1f}%")
except Exception as e:
    warn(f"SignalQuality load error: {e}")

# ============================================================
#  7. PROCESS CHECK
# ============================================================
print(f"\n{BOLD}[7] Bot Process{RESET}")
import subprocess
result = subprocess.run(
    ["powershell", "-Command", "Get-Process python* -ErrorAction SilentlyContinue | Select-Object Id, StartTime | Format-Table -AutoSize"],
    capture_output=True, text=True, timeout=10
)
lines = [l.strip() for l in result.stdout.strip().split('\n') if l.strip() and not l.strip().startswith('-') and 'Id' not in l]
if len(lines) >= 2:  # at least 2 python processes (main + event loop)
    ok(f"Bot running ({len(lines)} python processes)")
else:
    warn(f"Bot may not be running (found {len(lines)} python processes)")

# ============================================================
#  8. LOG FRESHNESS
# ============================================================
print(f"\n{BOLD}[8] Log Freshness{RESET}")
today = datetime.now().strftime("%Y%m%d")
log_file = LOGS / f"v13pro_{today}.log"
if log_file.exists():
    age_sec = time.time() - log_file.stat().st_mtime
    if age_sec > 600:
        warn(f"Log file last written {age_sec/60:.0f} min ago -- bot may be stuck!")
    else:
        ok(f"Log updated {age_sec:.0f}s ago")
else:
    warn(f"No log file for today ({log_file.name})")

# Shadow data freshness
today_dash = datetime.now().strftime("%Y-%m-%d")
shadow_file = LOGS / "shadow" / f"shadow_{today_dash}.jsonl"
if shadow_file.exists():
    age_sec = time.time() - shadow_file.stat().st_mtime
    lines_count = sum(1 for _ in open(shadow_file))
    if age_sec > 600:
        warn(f"Shadow file last written {age_sec/60:.0f} min ago")
    else:
        ok(f"Shadow: {lines_count} entries today, updated {age_sec:.0f}s ago")
else:
    warn("No shadow data for today")

# ============================================================
#  9. TRADE FLOW CHECK  (can signals actually reach execution?)
# ============================================================
print(f"\n{BOLD}[9] Trade Flow Audit{RESET}")
# Read recent log to check if signals are passing or all blocked
if log_file.exists():
    try:
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            lines_all = f.readlines()
        # Last 500 lines
        recent = lines_all[-500:] if len(lines_all) > 500 else lines_all
        recent_text = ''.join(recent)

        skip_count = recent_text.count("SKIP(")
        pass_count = recent_text.count("PASS")
        entry_count = recent_text.count("ENTRY LONG") + recent_text.count("ENTRY SHORT")
        risk_gate = recent_text.count("hunter_risk_gate")
        slots_full = recent_text.count("hunter_slots_full")

        if skip_count > 0 and pass_count == 0 and entry_count == 0:
            fail(f"ALL signals blocked! {skip_count} SKIPs, 0 PASS, 0 entries in recent log")
            if risk_gate > skip_count * 0.8:
                fail(f"  -> {risk_gate}/{skip_count} blocked by hunter_risk_gate (check state.json!)")
            if slots_full > 0:
                warn(f"  -> {slots_full} blocked by hunter_slots_full")
        elif pass_count > 0:
            ok(f"Signals flowing: {pass_count} PASS, {entry_count} entries, {skip_count} skips")
        else:
            ok(f"Log stats: {skip_count} skips, {pass_count} pass, {entry_count} entries")
    except Exception as e:
        warn(f"Could not parse log: {e}")

# ============================================================
#  SUMMARY
# ============================================================
print(f"\n{'='*60}")
if errors:
    print(f"{RED}{BOLD}PREFLIGHT FAILED: {len(errors)} error(s), {len(warnings)} warning(s){RESET}")
    for e in errors:
        print(f"  {RED}X{RESET} {e}")
    sys.exit(1)
elif warnings:
    print(f"{YELLOW}{BOLD}PREFLIGHT PASSED with {len(warnings)} warning(s){RESET}")
    for w in warnings:
        print(f"  {YELLOW}!{RESET} {w}")
    sys.exit(0)
else:
    print(f"{GREEN}{BOLD}PREFLIGHT PASSED: All checks clean{RESET}")
    sys.exit(0)
