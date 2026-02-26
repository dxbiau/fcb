"""
x10_analysis.py -- Deep shadow data analysis for x10 strategy.

Finds the highest-expectancy setups and calculates what changes
would accelerate x10 growth the most.
"""
import json, glob, sys
from collections import defaultdict
from pathlib import Path

SHADOW_DIR = Path("v13pro/logs/shadow")

# Load all shadow outcomes
outcomes = []
for f in sorted(SHADOW_DIR.glob("shadow_*.jsonl")):
    for line in open(f, encoding="utf-8", errors="replace"):
        try:
            rec = json.loads(line.strip())
            if rec.get("outcome") in ("tp", "sl", "trail"):
                outcomes.append(rec)
        except Exception:
            continue

print(f"Loaded {len(outcomes)} completed outcomes from shadow data\n")

if not outcomes:
    print("No outcomes found!")
    sys.exit(1)

# ============================================================
#  1. STRATEGY x TIMEFRAME breakdown
# ============================================================
print("=" * 70)
print("  STRATEGY x TIMEFRAME PERFORMANCE (sorted by expectancy)")
print("=" * 70)

combo_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "total_r": 0.0, "count": 0, "pnl_list": []})
for o in outcomes:
    strat = o.get("strategy", "?")
    tf = o.get("tf", "?")
    pnl_r = o.get("pnl_r", 0)
    key = f"{strat}/{tf}"
    s = combo_stats[key]
    s["count"] += 1
    s["total_r"] += pnl_r
    s["pnl_list"].append(pnl_r)
    if pnl_r > 0:
        s["wins"] += 1
    else:
        s["losses"] += 1

# Sort by expectancy (total_r / count)
ranked = []
for key, s in combo_stats.items():
    wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
    exp = s["total_r"] / s["count"] if s["count"] > 0 else 0
    ranked.append((key, s["count"], wr, exp, s["total_r"], s["wins"], s["losses"]))

ranked.sort(key=lambda x: -x[3])  # sort by expectancy desc

print(f"\n{'Combo':<25} {'N':>5} {'WR%':>6} {'ExpR':>7} {'TotalR':>8} {'W':>4} {'L':>4}")
print("-" * 70)
for key, n, wr, exp, total_r, w, l in ranked:
    marker = " ***" if exp > 0.10 and n >= 20 else ""
    print(f"{key:<25} {n:>5} {wr:>5.1f}% {exp:>+7.3f} {total_r:>+8.1f} {w:>4} {l:>4}{marker}")

# ============================================================
#  2. BEST COMBOS (positive expectancy + enough samples)
# ============================================================
print("\n" + "=" * 70)
print("  TOP COMBOS (ExpR > 0, N >= 15)")
print("=" * 70)

top_combos = [(k, n, wr, exp, tr) for k, n, wr, exp, tr, w, l in ranked if exp > 0 and n >= 15]
if top_combos:
    total_r_top = sum(tr for _, _, _, _, tr in top_combos)
    total_n_top = sum(n for _, n, _, _, _ in top_combos)
    for k, n, wr, exp, tr in top_combos:
        print(f"  {k:<25} N={n:>4}  WR={wr:>5.1f}%  Exp={exp:>+.3f}R  Total={tr:>+.1f}R")
    print(f"\n  Combined: {total_n_top} trades, {total_r_top:+.1f}R total")
else:
    print("  No combos with positive expectancy and enough samples!")

# ============================================================
#  3. WORST COMBOS (bleeding)
# ============================================================
print("\n" + "=" * 70)
print("  WORST COMBOS (bleeding the most)")
print("=" * 70)

worst = [(k, n, wr, exp, tr) for k, n, wr, exp, tr, w, l in ranked if tr < -5 and n >= 10]
worst.sort(key=lambda x: x[4])
for k, n, wr, exp, tr in worst[:10]:
    print(f"  {k:<25} N={n:>4}  WR={wr:>5.1f}%  Exp={exp:>+.3f}R  Total={tr:>+.1f}R")

# ============================================================
#  4. SENTIMENT EDGE
# ============================================================
print("\n" + "=" * 70)
print("  SENTIMENT EDGE (bull vs bear vs neutral)")
print("=" * 70)

sent_stats = defaultdict(lambda: {"wins": 0, "count": 0, "total_r": 0.0})
for o in outcomes:
    sent = o.get("sentiment", o.get("market_sentiment", ""))
    if isinstance(sent, dict):
        sent = sent.get("bias", "?")
    sent = str(sent).lower()
    if "bull" in sent:
        bucket = "bull"
    elif "bear" in sent:
        bucket = "bear"
    else:
        bucket = "neutral"
    s = sent_stats[bucket]
    s["count"] += 1
    s["total_r"] += o.get("pnl_r", 0)
    if o.get("pnl_r", 0) > 0:
        s["wins"] += 1

for bucket in ["bull", "bear", "neutral"]:
    s = sent_stats[bucket]
    if s["count"] > 0:
        wr = s["wins"] / s["count"] * 100
        exp = s["total_r"] / s["count"]
        print(f"  {bucket:<10} N={s['count']:>5}  WR={wr:>5.1f}%  Exp={exp:>+.3f}R  Total={s['total_r']:>+.1f}R")

# ============================================================
#  5. GRADE PERFORMANCE
# ============================================================
print("\n" + "=" * 70)
print("  GRADE PERFORMANCE")
print("=" * 70)

grade_stats = defaultdict(lambda: {"wins": 0, "count": 0, "total_r": 0.0})
for o in outcomes:
    grade = o.get("grade", "?")
    s = grade_stats[grade]
    s["count"] += 1
    s["total_r"] += o.get("pnl_r", 0)
    if o.get("pnl_r", 0) > 0:
        s["wins"] += 1

for grade in sorted(grade_stats.keys()):
    s = grade_stats[grade]
    if s["count"] >= 5:
        wr = s["wins"] / s["count"] * 100
        exp = s["total_r"] / s["count"]
        print(f"  {grade:<6} N={s['count']:>5}  WR={wr:>5.1f}%  Exp={exp:>+.3f}R  Total={s['total_r']:>+.1f}R")

# ============================================================
#  6. LONGS vs SHORTS
# ============================================================
print("\n" + "=" * 70)
print("  SIDE PERFORMANCE")
print("=" * 70)

side_stats = defaultdict(lambda: {"wins": 0, "count": 0, "total_r": 0.0})
for o in outcomes:
    side = o.get("side", "?").lower()
    s = side_stats[side]
    s["count"] += 1
    s["total_r"] += o.get("pnl_r", 0)
    if o.get("pnl_r", 0) > 0:
        s["wins"] += 1

for side in ["long", "short"]:
    s = side_stats[side]
    if s["count"] > 0:
        wr = s["wins"] / s["count"] * 100
        exp = s["total_r"] / s["count"]
        print(f"  {side:<8} N={s['count']:>5}  WR={wr:>5.1f}%  Exp={exp:>+.3f}R  Total={s['total_r']:>+.1f}R")

# ============================================================
#  7. CONVICTION BANDS
# ============================================================
print("\n" + "=" * 70)
print("  CONVICTION PERFORMANCE (only passed signals)")
print("=" * 70)

conv_stats = defaultdict(lambda: {"wins": 0, "count": 0, "total_r": 0.0})
for o in outcomes:
    if not o.get("passed", False):
        continue
    conv = o.get("conviction", 0)
    if conv < 60:
        bucket = "<60"
    elif conv < 70:
        bucket = "60-69"
    elif conv < 80:
        bucket = "70-79"
    elif conv < 90:
        bucket = "80-89"
    else:
        bucket = "90+"
    s = conv_stats[bucket]
    s["count"] += 1
    s["total_r"] += o.get("pnl_r", 0)
    if o.get("pnl_r", 0) > 0:
        s["wins"] += 1

for bucket in ["<60", "60-69", "70-79", "80-89", "90+"]:
    s = conv_stats[bucket]
    if s["count"] > 0:
        wr = s["wins"] / s["count"] * 100
        exp = s["total_r"] / s["count"]
        print(f"  {bucket:<8} N={s['count']:>5}  WR={wr:>5.1f}%  Exp={exp:>+.3f}R  Total={s['total_r']:>+.1f}R")

# ============================================================
#  8. X10 PROJECTION
# ============================================================
print("\n" + "=" * 70)
print("  X10 PROJECTION")
print("=" * 70)

equity = 490
target = 5000
risk_pct = 0.03
leverage = 10

# If we only traded the top combos
if top_combos:
    avg_exp = sum(exp for _, _, _, exp, _ in top_combos) / len(top_combos)
    trades_per_day = total_n_top / 2  # 2 days of data
    daily_r = trades_per_day * avg_exp
    daily_pct = daily_r * risk_pct * 100

    print(f"\n  Current equity: ${equity:.0f}")
    print(f"  Target: ${target:.0f} (x{target/equity:.1f})")
    print(f"  Top combos avg expectancy: {avg_exp:+.3f}R")
    print(f"  Estimated trades/day (top combos): {trades_per_day:.0f}")
    print(f"  Daily R expected: {daily_r:+.1f}R")
    print(f"  Daily % growth (at {risk_pct*100:.0f}% risk): {daily_pct:+.1f}%")

    if daily_pct > 0:
        import math
        days_needed = math.log(target / equity) / math.log(1 + daily_pct / 100)
        print(f"  Days to x10: {days_needed:.1f}")
    else:
        print(f"  Days to x10: INFINITE (negative expectancy!)")

# ============================================================
#  9. ACTIONABLE RECOMMENDATIONS
# ============================================================
print("\n" + "=" * 70)
print("  ACTIONABLE RECOMMENDATIONS")
print("=" * 70)

# Identify combos that should be avoided
avoid = [k for k, n, wr, exp, tr, w, l in ranked if exp < -0.20 and n >= 20]
if avoid:
    print(f"\n  DANGER COMBOS (exp < -0.20, N>=20) -- reduce size or skip:")
    for k in avoid:
        s = combo_stats[k]
        exp = s["total_r"] / s["count"]
        wr = s["wins"] / s["count"] * 100
        print(f"    {k}: WR={wr:.0f}% Exp={exp:+.3f}R ({s['count']} trades)")

# Identify combos that should get more size
boost = [(k, n, wr, exp) for k, n, wr, exp, tr, w, l in ranked if exp > 0.15 and n >= 15]
if boost:
    print(f"\n  BEST COMBOS (exp > +0.15, N>=15) -- INCREASE size:")
    for k, n, wr, exp in boost:
        print(f"    {k}: WR={wr:.0f}% Exp={exp:+.3f}R ({n} trades)")

print("\nDone.")
