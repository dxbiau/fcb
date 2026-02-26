"""Pick top 15 OBR pairs from deep scan + extended validation."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ccxt

ex = ccxt.bybit({"apiKey": os.environ.get("BYBIT_API_KEY",""),
                  "secret": os.environ.get("BYBIT_API_SECRET",""),
                  "enableRateLimit": True, "options": {"defaultType": "swap"}})
ex.load_markets()

def fetch(sym, limit=1000):
    raw = ex.fetch_ohlcv(sym, "5m", limit=limit)
    return [{"ts":r[0],"o":r[1],"h":r[2],"l":r[3],"c":r[4],"v":r[5]} for r in raw]

def obr(p, c):
    if p["h"]==p["l"] or c["h"]==c["l"]: return 0
    if c["o"]>c["c"] and c["h"]>p["h"] and c["l"]<p["l"] and c["c"]<p["l"]: return 2
    if c["o"]<c["c"] and c["h"]>p["h"] and c["l"]<p["l"] and c["c"]>p["h"]: return 1
    return 0

def confirm(s, c):
    if s==2: return c["c"]>c["o"]
    if s==1: return c["c"]<c["o"]
    return False

def bt(candles, tp_r, fee=0.04):
    trades=[]; i=0
    while i<len(candles)-3:
        s=obr(candles[i],candles[i+1])
        if s==0: i+=1; continue
        if not confirm(s,candles[i+2]): i+=1; continue
        ep=candles[i+3]["o"]; ob_c=candles[i+1]
        if s==2:
            sl=ob_c["l"]; rpu=ep-sl
            if rpu<=0: i+=1; continue
            tp=ep+tp_r*rpu; res=None; ei=i+3
            for j in range(i+3,len(candles)):
                if candles[j]["l"]<=sl: res=-1.0-fee; ei=j; break
                if candles[j]["h"]>=tp: res=tp_r-fee; ei=j; break
            if res is None: res=(candles[-1]["c"]-ep)/rpu-fee; ei=len(candles)-1
        else:
            sl=ob_c["h"]; rpu=sl-ep
            if rpu<=0: i+=1; continue
            tp=ep-tp_r*rpu; res=None; ei=i+3
            for j in range(i+3,len(candles)):
                if candles[j]["h"]>=sl: res=-1.0-fee; ei=j; break
                if candles[j]["l"]<=tp: res=tp_r-fee; ei=j; break
            if res is None: res=(ep-candles[-1]["c"])/rpu-fee; ei=len(candles)-1
        trades.append({"s":"L" if s==2 else "S","r":round(res,3)})
        i=ei+1
    return trades

# Candidates: all liquid USDT perps
usdt = [sym for sym, m in ex.markets.items()
        if m.get("swap") and m.get("linear") and m.get("settle")=="USDT" and m.get("active")]
tickers = ex.fetch_tickers(symbols=usdt[:500])
liquid = [(sym, float(t.get("quoteVolume") or 0)) for sym, t in tickers.items()
          if float(t.get("quoteVolume") or 0) > 2_000_000]
liquid.sort(key=lambda x: -x[1])

print(f"Scanning {len(liquid)} pairs with 1000 candles each...\n")

rankings = []

for idx, (sym, vol) in enumerate(liquid):
    try:
        candles = fetch(sym, 1000)
        if len(candles) < 100: continue

        best_tp = None; best_r = -999; best_stats = None
        for tp_r in [1.0, 1.5, 2.0, 2.5, 3.0]:
            trades = bt(candles, tp_r)
            if len(trades) < 5: continue
            wins = sum(1 for t in trades if t["r"]>0)
            total_r = sum(t["r"] for t in trades)
            wr = wins/len(trades)*100
            eq=100; pk=100; mdd=0
            for t in trades:
                eq+=t["r"]*2; 
                if eq>pk: pk=eq
                dd=(pk-eq)/pk*100
                if dd>mdd: mdd=dd
            wr_pos = sum(t["r"] for t in trades if t["r"]>0)
            wr_neg = abs(sum(t["r"] for t in trades if t["r"]<0))
            pf = wr_pos/wr_neg if wr_neg>0 else 999

            if total_r > best_r:
                best_r = total_r; best_tp = tp_r
                best_stats = {"tp": tp_r, "n": len(trades), "w": wins,
                              "wr": wr, "r": round(total_r,2), "dd": round(mdd,1),
                              "pf": round(pf,2)}

        if best_stats and best_stats["r"] > 0 and best_stats["n"] >= 5:
            # Quality score: reward WR, PF, low DD, positive R
            qs = 0
            if best_stats["wr"] >= 50: qs += 20
            if best_stats["wr"] >= 55: qs += 10
            if best_stats["pf"] >= 1.5: qs += 15
            if best_stats["pf"] >= 2.0: qs += 10
            if best_stats["dd"] < 10: qs += 15
            if best_stats["dd"] < 6: qs += 10
            if best_stats["r"] >= 5: qs += 10
            if best_stats["r"] >= 10: qs += 10

            rankings.append({"pair": sym, "vol": vol, "qs": qs, **best_stats})

        if (idx+1) % 30 == 0:
            print(f"  {idx+1}/{len(liquid)} scanned... {len(rankings)} with edge")
        time.sleep(0.1)
    except:
        pass

rankings.sort(key=lambda x: -x["qs"])

print(f"\n{'='*70}")
print(f"  TOP 20 PAIRS BY QUALITY SCORE (1000 candles backtest)")
print(f"{'='*70}")
print(f"  {'#':>2}  {'Pair':<22} {'TP':>3}  {'Trades':>6}  {'WR':>5}  {'TotalR':>7}  {'DD':>5}  {'PF':>5}  {'QS':>3}  {'Vol$M':>6}")
print(f"  {'-'*2}  {'-'*22} {'-'*3}  {'-'*6}  {'-'*5}  {'-'*7}  {'-'*5}  {'-'*5}  {'-'*3}  {'-'*6}")

for i, r in enumerate(rankings[:20]):
    print(f"  {i+1:2d}  {r['pair']:<22} {r['tp']:>3}  {r['n']:>6}  {r['wr']:>4.0f}%  {r['r']:>+6.1f}  {r['dd']:>4.1f}%  {r['pf']:>5.2f}  {r['qs']:>3}  {r['vol']/1e6:>5.0f}M")

# Select top 15
top15 = rankings[:15]
print(f"\n{'='*70}")
print(f"  SELECTED 15 PAIRS FOR LIVE BOT")
print(f"{'='*70}")
for i, r in enumerate(top15):
    print(f"  {i+1:2d}. {r['pair']:<22}  TP={r['tp']}R  WR={r['wr']:.0f}%  R={r['r']:+.1f}  DD={r['dd']:.0f}%  PF={r['pf']:.2f}")

# Output config format
print(f"\n  --- Config snippet ---")
print("PAIR_TP = {")
for r in top15:
    print(f'    "{r["pair"]}": {r["tp"]},')
print("}")
