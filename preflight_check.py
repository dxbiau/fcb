"""FCB Live Bot — Pre-launch Readiness Check.

Tests all strategy logic, config consistency, and module imports
WITHOUT placing any trades or needing exchange access.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

issues = []
warnings = []
checks_passed = 0

print("=" * 60)
print("  FCB LIVE BOT -- PRE-LAUNCH READINESS CHECK")
print("=" * 60)
print()

# ── 1. IMPORTS ──
print("1. MODULE IMPORTS")
try:
    from live.config import (
        API_KEY, API_SECRET, MAINNET,
        TP_R, MIN_RANGE_PCT, FEE_RATE, RISK_PCT,
        MAX_TRADES_SESSION, MAX_TRADES_DAY, LEVERAGE,
        SESSIONS, PAIRS, ALL_PAIRS,
        EQUITY_FLOOR, TIMEFRAME, POLL_INTERVAL,
        STATE_FILE, LOG_DIR, TRADE_LOG,
    )
    print("   [OK] live.config -- all symbols imported")
    checks_passed += 1
except ImportError as e:
    issues.append(f"live.config: {e}")
    print(f"   [FAIL] live.config: {e}")

try:
    from live.strategy import (
        FirstCandle, TradeSignal,
        capture_first_candle, check_breakout, compute_signal,
    )
    print("   [OK] live.strategy -- all functions imported")
    checks_passed += 1
except ImportError as e:
    issues.append(f"live.strategy: {e}")
    print(f"   [FAIL] live.strategy: {e}")

try:
    from live.state import BotState
    print("   [OK] live.state -- BotState imported")
    checks_passed += 1
except ImportError as e:
    issues.append(f"live.state: {e}")
    print(f"   [FAIL] live.state: {e}")

try:
    from live import exchange as exch
    print("   [OK] live.exchange imported")
    checks_passed += 1
except ImportError as e:
    issues.append(f"live.exchange: {e}")
    print(f"   [FAIL] live.exchange: {e}")

try:
    from live import logger as log
    print("   [OK] live.logger imported")
    checks_passed += 1
except ImportError as e:
    issues.append(f"live.logger: {e}")
    print(f"   [FAIL] live.logger: {e}")

try:
    from live import trades as trade_log
    print("   [OK] live.trades imported")
    checks_passed += 1
except ImportError as e:
    issues.append(f"live.trades: {e}")
    print(f"   [FAIL] live.trades: {e}")

try:
    from live.bot import FCBBot, current_session, next_session_start
    print("   [OK] live.bot -- FCBBot imported")
    checks_passed += 1
except ImportError as e:
    issues.append(f"live.bot: {e}")
    print(f"   [FAIL] live.bot: {e}")

print()

# ── 2. CONFIG ──
print("2. CONFIG PARAMETERS (frozen strategy)")
params = {
    "TP_R": TP_R,
    "MIN_RANGE_PCT": f"{MIN_RANGE_PCT} ({MIN_RANGE_PCT*100:.2f}%)",
    "FEE_RATE": f"{FEE_RATE} ({FEE_RATE*100:.3f}%)",
    "RISK_PCT": f"{RISK_PCT} ({RISK_PCT*100:.1f}%)",
    "LEVERAGE": f"{LEVERAGE}x",
    "MAX_TRADES_SESSION": MAX_TRADES_SESSION,
    "MAX_TRADES_DAY": MAX_TRADES_DAY,
    "EQUITY_FLOOR": f"${EQUITY_FLOOR:.0f}",
    "MAINNET": MAINNET,
}
for k, v in params.items():
    print(f"   {k} = {v}")

if TP_R != 1.5:
    warnings.append(f"TP_R={TP_R}, expected 1.5 for frozen strategy")
if RISK_PCT != 0.02:
    warnings.append(f"RISK_PCT={RISK_PCT}, expected 0.02")
if MIN_RANGE_PCT != 0.003:
    warnings.append(f"MIN_RANGE_PCT={MIN_RANGE_PCT}, expected 0.003")
if FEE_RATE != 0.0002:
    warnings.append(f"FEE_RATE={FEE_RATE}, expected 0.0002")
if LEVERAGE < 1 or LEVERAGE > 20:
    issues.append(f"LEVERAGE={LEVERAGE} outside safe range 1-20")
if not API_KEY or not API_SECRET:
    issues.append("API keys are empty!")
else:
    print(f"   API_KEY present (len={len(API_KEY)})")
    print(f"   API_SECRET present (len={len(API_SECRET)})")
    checks_passed += 1

if MAINNET:
    print("   ** MAINNET MODE -- REAL MONEY **")

checks_passed += 1
print()

# ── 3. SESSIONS & PAIRS ──
print("3. SESSIONS & PAIRS")
for s, (start, end) in SESSIONS.items():
    print(f"   {s}: {start:02d}:00 -- {end:02d}:00 UTC")

hours_covered = set()
for s, (start, end) in SESSIONS.items():
    for h in range(start, end if end < 24 else 24):
        hours_covered.add(h)
if len(hours_covered) == 24:
    print("   [OK] Full 24-hour coverage")
    checks_passed += 1
else:
    missing = sorted(set(range(24)) - hours_covered)
    warnings.append(f"Hour gaps: {missing}")
    print(f"   [WARN] Missing hours: {missing}")

for sess, pairs in PAIRS.items():
    print(f"   {sess}: {len(pairs)} pairs")
print(f"   ALL_PAIRS (unique): {len(ALL_PAIRS)}")
checks_passed += 1

for sess in PAIRS:
    if sess not in SESSIONS:
        issues.append(f"Session '{sess}' in PAIRS but not in SESSIONS")
for sess in SESSIONS:
    if sess not in PAIRS:
        warnings.append(f"Session '{sess}' defined but has no pairs")
checks_passed += 1
print()

# ── 4. STRATEGY LOGIC TESTS ──
print("4. STRATEGY LOGIC TESTS")

# 4a: Valid first candle
fc = capture_first_candle("TEST/USDT:USDT", "asia", {
    "open": 1.0, "high": 1.01, "low": 0.99, "close": 1.005,
})
assert fc.valid, "Valid range should pass"
assert abs(fc.range_pct - 0.02) < 1e-9
assert abs(fc.midpoint - 1.0) < 1e-9
print("   [OK] First candle capture (valid range 2%)")
checks_passed += 1

# 4b: Tiny range rejected
fc_small = capture_first_candle("TEST/USDT:USDT", "asia", {
    "open": 1.0, "high": 1.001, "low": 0.999, "close": 1.0005,
})
assert not fc_small.valid
print("   [OK] Rejects tiny range (<0.3%)")
checks_passed += 1

# 4c-4e: Breakout detection
assert check_breakout(fc, 1.015) == "long"
print("   [OK] Long breakout (close > high)")
checks_passed += 1

assert check_breakout(fc, 0.985) == "short"
print("   [OK] Short breakout (close < low)")
checks_passed += 1

assert check_breakout(fc, 1.005) is None
print("   [OK] No breakout (close inside range)")
checks_passed += 1

# 4f: Long signal
sig = compute_signal(
    fc=fc, direction="long", entry_price=1.015, equity=1000,
    contract_size=1, price_precision=4, qty_precision=2,
    min_qty=0.01, min_notional=5.0,
)
assert sig is not None
assert sig.direction == "long"
assert sig.stop_loss == fc.midpoint  # SL = midpoint
assert sig.take_profit > sig.entry_price
expected_tp = 1.015 + TP_R * abs(1.015 - fc.midpoint)
assert abs(sig.take_profit - expected_tp) < 1e-9
risk_usd = 1000 * RISK_PCT
expected_qty = risk_usd / abs(1.015 - fc.midpoint)
assert abs(sig.position_size - expected_qty) < 1e-6
print(f"   [OK] Long signal: entry=1.015 SL={sig.stop_loss:.4f} "
      f"TP={sig.take_profit:.4f} qty={sig.position_size:.2f}")
checks_passed += 1

# 4g: Short signal
sig_s = compute_signal(
    fc=fc, direction="short", entry_price=0.985, equity=1000,
    contract_size=1, price_precision=4, qty_precision=2,
    min_qty=0.01, min_notional=5.0,
)
assert sig_s is not None and sig_s.take_profit < sig_s.entry_price
print(f"   [OK] Short signal: entry=0.985 SL={sig_s.stop_loss:.4f} "
      f"TP={sig_s.take_profit:.4f}")
checks_passed += 1

# 4h: Rejects tiny position
sig_tiny = compute_signal(
    fc=fc, direction="long", entry_price=1.015, equity=1,
    contract_size=1, price_precision=4, qty_precision=2,
    min_qty=100, min_notional=5000,
)
assert sig_tiny is None
print("   [OK] Rejects position below min_qty/min_notional")
checks_passed += 1

# 4i: Fee R formula
expected_fee_r = 2.0 * FEE_RATE * 1.015 / abs(1.015 - fc.midpoint)
assert abs(sig.fee_r - expected_fee_r) < 1e-9
print(f"   [OK] Fee R = {sig.fee_r:.4f} (correct formula)")
checks_passed += 1

print()

# ── 5. STATE MANAGEMENT ──
print("5. STATE MANAGEMENT")
state = BotState()
assert state.can_trade("TEST/USDT:USDT", "asia", 1, 3) is True
print("   [OK] Fresh state allows trade")
checks_passed += 1

state.equity = 1000
state.record_entry("TEST/USDT:USDT", "asia",
                    {"symbol": "TEST/USDT:USDT", "session": "asia"})
assert state.can_trade("TEST/USDT:USDT", "asia", 1, 3) is False
print("   [OK] Blocks same pair same session")
checks_passed += 1

assert state.can_trade("TEST/USDT:USDT", "london", 1, 3) is True
print("   [OK] Allows same pair different session")
checks_passed += 1

state.daily_counts["TEST/USDT:USDT"] = 3
assert state.can_trade("TEST/USDT:USDT", "london", 1, 3) is False
print("   [OK] Blocks at daily limit (3)")
checks_passed += 1

state.date = "2025-01-01"
state.check_new_day()
assert state.daily_counts == {}
print("   [OK] Day rollover resets counters")
checks_passed += 1

print()

# ── 6. SESSION LOGIC ──
print("6. BOT SESSION LOGIC")
from datetime import datetime, timezone
sess_now = current_session()
print(f"   UTC now: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
print(f"   Current session: {sess_now or 'none'}")
name, when = next_session_start()
print(f"   Next session: {name} at {when.strftime('%H:%M')} UTC")
checks_passed += 1
print()

# ── 7. RISK MATH ──
print("7. RISK MATH SANITY CHECK")
equity = 1000
risk_per_trade = equity * RISK_PCT
print(f"   Risk/trade: ${risk_per_trade:.2f} ({RISK_PCT*100:.0f}% of equity)")
max_daily = len(ALL_PAIRS) * risk_per_trade
print(f"   Max daily risk (all {len(ALL_PAIRS)} pairs fire): "
      f"${max_daily:.2f} ({max_daily/equity*100:.1f}%)")
print(f"   Realistic daily risk (5-10 triggers): "
      f"${5*risk_per_trade:.2f} -- ${10*risk_per_trade:.2f}")
be_wr = 1.0 / (1.0 + TP_R) * 100
print(f"   Breakeven WR at {TP_R}R: {be_wr:.1f}%")
checks_passed += 1
print()

# ── RESULTS ──
print("=" * 60)
print(f"  CHECKS PASSED: {checks_passed}")
if warnings:
    print(f"  WARNINGS: {len(warnings)}")
    for w in warnings:
        print(f"    ! {w}")
if issues:
    print(f"  ISSUES: {len(issues)}")
    for i in issues:
        print(f"    X {i}")
else:
    print(f"  ISSUES: 0")
    print()
    print("  >>> FCB STRATEGY IS READY FOR LIVE MARKET <<<")
print("=" * 60)

# Clean up test state file
import json
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        # Only clean up if it's our test data
        if data.get("entries_today", 0) <= 1 and data.get("total_trades", 0) == 0:
            os.remove(STATE_FILE)
            print("\n(Cleaned up test state file)")
    except:
        pass
