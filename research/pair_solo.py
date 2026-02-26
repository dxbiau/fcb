"""Per-pair solo analysis: measure each pair's TRUE edge without slot competition."""
import sys, statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from research.session_sim import load_all_pairs, SimConfig, simulate

pair_data = load_all_pairs()
cfg = SimConfig(risk_pct=0.03, trail_activation_r=0.95, trail_distance_r=0.15)

pair_results = []
total = len(pair_data)
for i, (pair, candles) in enumerate(sorted(pair_data.items())):
    sys.stdout.write(f"\r  {i+1}/{total}")
    sys.stdout.flush()
    res = simulate({pair: candles}, cfg)
    rv = res["r_vals"]
    if not rv:
        continue
    w = sum(1 for r in rv if r > 0)
    wr = w / len(rv)
    avg = statistics.mean(rv)
    tot = sum(rv)
    wins = [r for r in rv if r > 0]
    losses = [r for r in rv if r <= 0]
    avg_w = statistics.mean(wins) if wins else 0
    avg_l = statistics.mean(losses) if losses else 0
    payoff = avg_w / abs(avg_l) if avg_l < 0 else 99
    pair_results.append({
        "pair": pair.replace("/USDT:USDT", ""), "trades": len(rv),
        "wr": wr, "avg_r": avg, "total_r": tot, "payoff": payoff,
        "avg_win": avg_w, "avg_loss": avg_l, "full_pair": pair,
    })

pair_results.sort(key=lambda x: -x["total_r"])

print(f"\nTotal pairs: {len(pair_results)}")
profitable = sum(1 for p in pair_results if p["total_r"] > 0)
losing = sum(1 for p in pair_results if p["total_r"] <= 0)
print(f"Profitable: {profitable}  |  Losing: {losing}\n")

print(f"{'Pair':>15s}  {'#':>3s}  {'WR':>5s}  {'AvgR':>7s}  {'TotR':>6s}  "
      f"{'Payoff':>6s}  {'AvgW':>6s}  {'AvgL':>6s}")
print("-" * 70)
for p in pair_results:
    flag = "***" if p["total_r"] > 3 and p["wr"] >= 0.35 else (
           "  *" if p["total_r"] > 0 else "   ")
    print(f"{p['pair']:>15s}  {p['trades']:>3d}  {p['wr']:>4.0%}  "
          f"{p['avg_r']:>+.4f}  {p['total_r']:>+5.1f}  {p['payoff']:>5.2f}  "
          f"{p['avg_win']:>+.3f}  {p['avg_loss']:>+.3f}  {flag}")

print("\n=== CUMULATIVE EDGE BY UNIVERSE SIZE ===")
for n in [5, 10, 15, 20, 25, 30, 40, 50, 60, 80, 100, len(pair_results)]:
    top_n = pair_results[:min(n, len(pair_results))]
    all_r = sum(p["total_r"] for p in top_n)
    all_t = sum(p["trades"] for p in top_n)
    all_w = sum(round(p["wr"] * p["trades"]) for p in top_n)
    wr = all_w / all_t if all_t > 0 else 0
    avg = all_r / all_t if all_t > 0 else 0
    losers = sum(1 for p in top_n if p["total_r"] <= 0)
    print(f"  Top {n:>3d}: {all_t:>4d}t  WR={wr:.1%}  AvgR={avg:+.4f}  "
          f"TotR={all_r:>+6.1f}  ({losers} losers in set)")

# Save top pairs list for use in focused sim
print("\n=== TOP 30 PAIR NAMES (for focused sim) ===")
for p in pair_results[:30]:
    print(f"  {p['full_pair']}")
