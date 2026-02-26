"""
Deep analysis of LONG trades only — per strategy/TF/grade/conviction.
This is the data that matters since we're LONG_ONLY_MODE.
"""
import json, glob, os
from collections import defaultdict

shadow_dir = os.path.join("v13pro", "logs", "shadow")
files = sorted(glob.glob(os.path.join(shadow_dir, "shadow_*.jsonl")))

outcomes = []
for f in files:
    for line in open(f):
        try:
            rec = json.loads(line.strip())
            if rec.get("pnl_r") is not None:
                outcomes.append(rec)
        except:
            pass

# Filter longs only
longs = [o for o in outcomes if o.get("side", "").lower() == "long"]
shorts = [o for o in outcomes if o.get("side", "").lower() == "short"]
print(f"Total outcomes: {len(outcomes)} | Longs: {len(longs)} | Shorts: {len(shorts)}")

def analyze(records, label=""):
    if not records:
        print(f"  No records for {label}")
        return {}
    wins = [r for r in records if r["pnl_r"] > 0]
    wr = len(wins)/len(records)*100
    total_r = sum(r["pnl_r"] for r in records)
    exp = total_r / len(records)
    return {"n": len(records), "wr": wr, "exp": exp, "total": total_r, "w": len(wins), "l": len(records)-len(wins)}

# ============================================================
# LONGS ONLY: Strategy x TF
# ============================================================
print("\n" + "="*70)
print("  LONGS ONLY: STRATEGY x TIMEFRAME")
print("="*70)

combos = defaultdict(list)
for o in longs:
    key = f"{o.get('strategy','?')}/{o.get('tf','?')}"
    combos[key].append(o)

results = []
for key, recs in sorted(combos.items()):
    s = analyze(recs, key)
    results.append((key, s))

results.sort(key=lambda x: x[1].get("exp", -99), reverse=True)
print(f"\n{'Combo':<30} {'N':>4} {'WR%':>7} {'ExpR':>8} {'TotalR':>8} {'W':>4} {'L':>4}")
print("-"*70)
for key, s in results:
    print(f"{key:<30} {s['n']:>4} {s['wr']:>6.1f}% {s['exp']:>+7.3f} {s['total']:>+8.1f} {s['w']:>4} {s['l']:>4}")

# ============================================================
# LONGS ONLY: By Grade
# ============================================================
print("\n" + "="*70)
print("  LONGS ONLY: BY GRADE")
print("="*70)

by_grade = defaultdict(list)
for o in longs:
    by_grade[o.get("grade", "?")].append(o)

for grade in sorted(by_grade.keys()):
    s = analyze(by_grade[grade])
    print(f"  {grade:<6} N={s['n']:>4}  WR={s['wr']:>5.1f}%  Exp={s['exp']:>+6.3f}R  Total={s['total']:>+7.1f}R")

# ============================================================
# LONGS ONLY: By Conviction Band
# ============================================================
print("\n" + "="*70)
print("  LONGS ONLY: BY CONVICTION BAND")
print("="*70)

bands = {"<60": [], "60-69": [], "70-79": [], "80-89": [], "90+": []}
for o in longs:
    c = o.get("conviction", o.get("score", 0))
    if c < 60: bands["<60"].append(o)
    elif c < 70: bands["60-69"].append(o)
    elif c < 80: bands["70-79"].append(o)
    elif c < 90: bands["80-89"].append(o)
    else: bands["90+"].append(o)

for band, recs in bands.items():
    if recs:
        s = analyze(recs)
        print(f"  {band:<8} N={s['n']:>4}  WR={s['wr']:>5.1f}%  Exp={s['exp']:>+6.3f}R  Total={s['total']:>+7.1f}R")

# ============================================================
# LONGS ONLY: Grade x Conviction Cross
# ============================================================
print("\n" + "="*70)
print("  LONGS ONLY: GRADE x CONVICTION (A+ only detail)")
print("="*70)

aplus_longs = [o for o in longs if o.get("grade") == "A+"]
print(f"\n  A+ Longs total: {len(aplus_longs)}")
if aplus_longs:
    bands_ap = {"<60": [], "60-69": [], "70-79": [], "80-89": [], "90+": []}
    for o in aplus_longs:
        c = o.get("conviction", o.get("score", 0))
        if c < 60: bands_ap["<60"].append(o)
        elif c < 70: bands_ap["60-69"].append(o)
        elif c < 80: bands_ap["70-79"].append(o)
        elif c < 90: bands_ap["80-89"].append(o)
        else: bands_ap["90+"].append(o)
    for band, recs in bands_ap.items():
        if recs:
            s = analyze(recs)
            print(f"    {band:<8} N={s['n']:>4}  WR={s['wr']:>5.1f}%  Exp={s['exp']:>+6.3f}R  Total={s['total']:>+7.1f}R")

# ============================================================
# LONGS ONLY: Top combos with conviction >= 80
# ============================================================
print("\n" + "="*70)
print("  LONGS ONLY: STRATEGY x TF (conviction >= 80 only)")
print("="*70)

high_conv_longs = [o for o in longs if o.get("conviction", o.get("score", 0)) >= 80]
print(f"  High conviction (80+) longs: {len(high_conv_longs)}")

combos_hc = defaultdict(list)
for o in high_conv_longs:
    key = f"{o.get('strategy','?')}/{o.get('tf','?')}"
    combos_hc[key].append(o)

results_hc = []
for key, recs in sorted(combos_hc.items()):
    s = analyze(recs, key)
    results_hc.append((key, s))

results_hc.sort(key=lambda x: x[1].get("exp", -99), reverse=True)
print(f"\n{'Combo':<30} {'N':>4} {'WR%':>7} {'ExpR':>8} {'TotalR':>8} {'W':>4} {'L':>4}")
print("-"*70)
for key, s in results_hc:
    print(f"{key:<30} {s['n']:>4} {s['wr']:>6.1f}% {s['exp']:>+7.3f} {s['total']:>+8.1f} {s['w']:>4} {s['l']:>4}")

# ============================================================
# LONGS: Best pair performance
# ============================================================
print("\n" + "="*70)
print("  LONGS ONLY: BY PAIR (top & bottom 10)")
print("="*70)

by_pair = defaultdict(list)
for o in longs:
    by_pair[o.get("pair", "?")].append(o)

pair_results = []
for pair, recs in by_pair.items():
    s = analyze(recs)
    pair_results.append((pair, s))

pair_results.sort(key=lambda x: x[1].get("exp", -99), reverse=True)
print(f"\n  TOP 10 PAIRS:")
for pair, s in pair_results[:10]:
    print(f"    {pair:<16} N={s['n']:>3}  WR={s['wr']:>5.1f}%  Exp={s['exp']:>+6.3f}R  Total={s['total']:>+6.1f}R")

print(f"\n  BOTTOM 10 PAIRS:")
for pair, s in pair_results[-10:]:
    print(f"    {pair:<16} N={s['n']:>3}  WR={s['wr']:>5.1f}%  Exp={s['exp']:>+6.3f}R  Total={s['total']:>+6.1f}R")

# ============================================================
# Sentiment analysis for LONGS only
# ============================================================
print("\n" + "="*70)
print("  LONGS ONLY: BY SENTIMENT")
print("="*70)

by_sent = defaultdict(list)
for o in longs:
    by_sent[o.get("sentiment", "?")].append(o)

for sent in sorted(by_sent.keys()):
    s = analyze(by_sent[sent])
    print(f"  {sent:<10} N={s['n']:>4}  WR={s['wr']:>5.1f}%  Exp={s['exp']:>+6.3f}R  Total={s['total']:>+7.1f}R")

# ============================================================
# X10 PROJECTION
# ============================================================
print("\n" + "="*70)
print("  X10 PROJECTION — LONGS, CONVICTION 80+, GRADE A or A+")
print("="*70)

elite = [o for o in longs 
         if o.get("conviction", o.get("score", 0)) >= 80 
         and o.get("grade", "?") in ("A+", "A")]
if elite:
    s = analyze(elite)
    print(f"\n  Elite longs: N={s['n']}, WR={s['wr']:.1f}%, ExpR={s['exp']:+.3f}")
    if s['exp'] > 0:
        risk_pct = 3.0
        trades_per_day = max(1, s['n'] / 3)  # rough estimate
        growth_per_trade = s['exp'] * risk_pct / 100
        from math import log, ceil
        trades_to_10x = ceil(log(10) / log(1 + growth_per_trade))
        days_to_10x = ceil(trades_to_10x / trades_per_day)
        print(f"  Growth per trade: {growth_per_trade*100:.3f}%")
        print(f"  Trades to 10x: ~{trades_to_10x}")
        print(f"  Est trades/day: ~{trades_per_day:.0f}")
        print(f"  Est days to 10x: ~{days_to_10x}")
    else:
        print(f"  ⚠ Expectancy still negative — need more filtering or strategy changes")
else:
    print("  No elite longs found")

# ============================================================
# What-if: Only positive-ExpR combos (longs, any conviction)
# ============================================================
print("\n" + "="*70)
print("  WHAT-IF: Only trade combos with ExpR > 0 (longs)")
print("="*70)

positive_combos = {k for k, s in results if s.get("exp", -1) > 0}
if positive_combos:
    print(f"  Positive combos: {positive_combos}")
    whatif = [o for o in longs if f"{o.get('strategy','?')}/{o.get('tf','?')}" in positive_combos]
    s = analyze(whatif)
    print(f"  N={s['n']}, WR={s['wr']:.1f}%, ExpR={s['exp']:+.3f}, TotalR={s['total']:+.1f}")
else:
    print("  No positive combos found in longs data")

print("\nDone.")
