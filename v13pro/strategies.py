"""
v13pro/strategies.py -- 12 strategies + ensemble (exact match to discovery v13).
"""
import numpy as np
from collections import defaultdict
from v13pro.indicators import ema, sma, atr, bollinger_bands, donchian_channels, rsi, stochastic


class Signal:
    __slots__ = ('bar', 'side', 'entry', 'stop_dist', 'strategy', 'pair', 'tf')
    def __init__(self, bar, side, entry, stop_dist, strategy='', pair='', tf=''):
        self.bar = bar; self.side = side; self.entry = entry
        self.stop_dist = stop_dist; self.strategy = strategy
        self.pair = pair; self.tf = tf


def msl(price, atr_val, maker=True):
    fee = 0.0004 if maker else 0.00105
    return max(price * fee * 2 * 3, atr_val, price * 0.003)


# ── 12 STRATEGIES ─────────────────────────────────────────────

def S_ema_rib(o,h,l,c,v,a,mk=True):
    e8,e21,e55 = ema(c,8), ema(c,21), ema(c,55); sigs=[]
    for i in range(2,len(c)):
        if np.isnan(e55[i]) or np.isnan(a[i]) or a[i]<=0: continue
        sd=msl(c[i],a[i],mk); body=abs(c[i]-o[i]); rng=h[i]-l[i]
        if rng<=0: continue
        if e8[i]>e21[i]>e55[i] and l[i]<=e8[i]*1.005 and c[i]>o[i] and body/rng>0.3:
            sigs.append(Signal(i,'long',c[i],sd))
        elif e8[i]<e21[i]<e55[i] and h[i]>=e8[i]*0.995 and c[i]<o[i] and body/rng>0.3:
            sigs.append(Signal(i,'short',c[i],sd))
    return sigs

def S_bb_break(o,h,l,c,v,a,mk=True):
    upper,mid,lower=bollinger_bands(c); e50=ema(c,50); vm=sma(v,20); sigs=[]
    for i in range(1,len(c)):
        if np.isnan(upper[i]) or np.isnan(e50[i]) or np.isnan(a[i]) or a[i]<=0: continue
        sd=msl(c[i],a[i],mk)
        vok=not np.isnan(vm[i]) and vm[i]>0 and v[i]>vm[i]*1.2
        if c[i]>upper[i] and c[i]>e50[i] and c[i]>o[i] and vok:
            sigs.append(Signal(i,'long',c[i],sd))
        elif c[i]<lower[i] and c[i]<e50[i] and c[i]<o[i] and vok:
            sigs.append(Signal(i,'short',c[i],sd))
    return sigs

def S_donchian(o,h,l,c,v,a,mk=True):
    du,dl=donchian_channels(h,l); sigs=[]
    for i in range(1,len(c)):
        if np.isnan(du[i]) or np.isnan(a[i]) or a[i]<=0: continue
        sd=msl(c[i],a[i],mk)
        if not np.isnan(du[i-1]) and c[i]>du[i-1]:
            sigs.append(Signal(i,'long',c[i],sd))
        elif not np.isnan(dl[i-1]) and c[i]<dl[i-1]:
            sigs.append(Signal(i,'short',c[i],sd))
    return sigs

def S_rsi_fade(o,h,l,c,v,a,mk=True):
    r=rsi(c,14); sigs=[]
    for i in range(2,len(c)):
        if np.isnan(r[i]) or np.isnan(r[i-1]) or np.isnan(a[i]) or a[i]<=0: continue
        sd=msl(c[i],a[i],mk)
        if r[i-1]<25 and r[i]>25 and c[i]>o[i]:
            sigs.append(Signal(i,'long',c[i],sd))
        elif r[i-1]>75 and r[i]<75 and c[i]<o[i]:
            sigs.append(Signal(i,'short',c[i],sd))
    return sigs

def S_bb_fade(o,h,l,c,v,a,mk=True):
    upper,mid,lower=bollinger_bands(c,20,2.0); sigs=[]
    for i in range(2,len(c)):
        if np.isnan(upper[i]) or np.isnan(a[i]) or a[i]<=0: continue
        sd=msl(c[i],a[i],mk)
        if l[i]<=lower[i] and c[i]>lower[i] and c[i]>o[i]:
            sigs.append(Signal(i,'long',c[i],sd))
        elif h[i]>=upper[i] and c[i]<upper[i] and c[i]<o[i]:
            sigs.append(Signal(i,'short',c[i],sd))
    return sigs

def S_stoch_x(o,h,l,c,v,a,mk=True):
    k,d=stochastic(h,l,c,14,3,3); s50=sma(c,50); sigs=[]
    for i in range(2,len(c)):
        if (np.isnan(k[i]) or np.isnan(d[i]) or np.isnan(k[i-1]) or
            np.isnan(d[i-1]) or np.isnan(s50[i]) or np.isnan(a[i]) or a[i]<=0): continue
        sd=msl(c[i],a[i],mk)
        if k[i-1]<d[i-1] and k[i]>d[i] and k[i-1]<25 and c[i]>s50[i]:
            sigs.append(Signal(i,'long',c[i],sd))
        elif k[i-1]>d[i-1] and k[i]<d[i] and k[i-1]>75 and c[i]<s50[i]:
            sigs.append(Signal(i,'short',c[i],sd))
    return sigs

def S_pin_bar(o,h,l,c,v,a,mk=True):
    s50=sma(c,50); sigs=[]
    for i in range(2,len(c)):
        if np.isnan(s50[i]) or np.isnan(a[i]) or a[i]<=0: continue
        sd=msl(c[i],a[i],mk); body=abs(c[i]-o[i]); rng=h[i]-l[i]
        if rng<=0 or body<=0: continue
        uw=h[i]-max(c[i],o[i]); lw=min(c[i],o[i])-l[i]
        if lw>2*body and uw<body*0.5 and c[i]>o[i] and c[i]>s50[i]:
            sigs.append(Signal(i,'long',c[i],sd))
        elif uw>2*body and lw<body*0.5 and c[i]<o[i] and c[i]<s50[i]:
            sigs.append(Signal(i,'short',c[i],sd))
    return sigs

def S_ib_break(o,h,l,c,v,a,mk=True):
    s50=sma(c,50); sigs=[]
    for i in range(3,len(c)):
        if np.isnan(a[i]) or a[i]<=0 or np.isnan(s50[i]): continue
        sd=msl(c[i],a[i],mk)
        if h[i-1]<h[i-2] and l[i-1]>l[i-2]:
            if c[i]>h[i-1] and c[i]>s50[i]:
                sigs.append(Signal(i,'long',c[i],sd))
            elif c[i]<l[i-1] and c[i]<s50[i]:
                sigs.append(Signal(i,'short',c[i],sd))
    return sigs

def S_engulf(o,h,l,c,v,a,mk=True):
    s50=sma(c,50); vm=sma(v,20); sigs=[]
    for i in range(2,len(c)):
        if np.isnan(s50[i]) or np.isnan(a[i]) or a[i]<=0: continue
        sd=msl(c[i],a[i],mk)
        vok=not np.isnan(vm[i]) and vm[i]>0 and v[i]>vm[i]*1.0
        bc=c[i]-o[i]; bp=c[i-1]-o[i-1]
        if bp<0 and bc>0 and o[i]<=c[i-1] and c[i]>=o[i-1] and c[i]>s50[i] and vok:
            sigs.append(Signal(i,'long',c[i],sd))
        elif bp>0 and bc<0 and o[i]>=c[i-1] and c[i]<=o[i-1] and c[i]<s50[i] and vok:
            sigs.append(Signal(i,'short',c[i],sd))
    return sigs

def S_mtf_rsi(o,h,l,c,v,a,mk=True):
    s200=sma(c,200); r=rsi(c,14); sigs=[]
    for i in range(2,len(c)):
        if np.isnan(s200[i]) or np.isnan(r[i]) or np.isnan(r[i-1]) or np.isnan(a[i]) or a[i]<=0: continue
        sd=msl(c[i],a[i],mk)
        if c[i]>s200[i] and r[i-1]<40 and r[i]>40 and c[i]>o[i]:
            sigs.append(Signal(i,'long',c[i],sd))
        elif c[i]<s200[i] and r[i-1]>60 and r[i]<60 and c[i]<o[i]:
            sigs.append(Signal(i,'short',c[i],sd))
    return sigs

def S_tr_pull(o,h,l,c,v,a,mk=True):
    e21=ema(c,21); e55=ema(c,55); r=rsi(c,14); sigs=[]
    for i in range(3,len(c)):
        if (np.isnan(e21[i]) or np.isnan(e55[i]) or np.isnan(a[i]) or a[i]<=0
            or np.isnan(r[i]) or np.isnan(r[i-1])): continue
        sd=msl(c[i],a[i],mk)
        if e21[i]>e55[i] and l[i]<=e21[i]*1.005 and c[i]>o[i] and r[i]>r[i-1] and r[i]<60:
            sigs.append(Signal(i,'long',c[i],sd))
        elif e21[i]<e55[i] and h[i]>=e21[i]*0.995 and c[i]<o[i] and r[i]<r[i-1] and r[i]>40:
            sigs.append(Signal(i,'short',c[i],sd))
    return sigs

def S_mom_surge(o,h,l,c,v,a,mk=True):
    vm=sma(v,20); e20=ema(c,20); sigs=[]
    for i in range(2,len(c)):
        if np.isnan(a[i]) or a[i]<=0 or np.isnan(vm[i]) or vm[i]<=0 or np.isnan(e20[i]): continue
        sd=msl(c[i],a[i],mk); body=abs(c[i]-o[i]); rng=h[i]-l[i]
        if rng<=0: continue
        if c[i]>o[i] and body>1.5*a[i] and v[i]>vm[i]*2.0 and c[i]>e20[i] and body/rng>0.6:
            sigs.append(Signal(i,'long',c[i],sd))
        elif c[i]<o[i] and body>1.5*a[i] and v[i]>vm[i]*2.0 and c[i]<e20[i] and body/rng>0.6:
            sigs.append(Signal(i,'short',c[i],sd))
    return sigs


STRATEGIES = {
    'EMA_RIB': S_ema_rib, 'BB_BREAK': S_bb_break, 'DONCHIAN': S_donchian,
    'RSI_FADE': S_rsi_fade, 'BB_FADE': S_bb_fade, 'STOCH_X': S_stoch_x,
    'PIN_BAR': S_pin_bar, 'IB_BREAK': S_ib_break, 'ENGULF': S_engulf,
    'MTF_RSI': S_mtf_rsi, 'TR_PULL': S_tr_pull, 'MOM_SURGE': S_mom_surge,
}
# NOTE: ORB + FCB lab strategies are registered at startup by bot.py
# (avoids circular import: strat_orb_fcb → strategies → strat_orb_fcb)


def scan_last_bar(o, h, l, c, v, pair='', tf='',
                  strategies=None, maker=True):
    a = atr(h, l, c, 14)
    bar_idx = len(c) - 1
    strats = strategies or list(STRATEGIES.keys())
    fired = []
    for name in strats:
        fn = STRATEGIES.get(name)
        if fn is None: continue
        for sig in fn(o, h, l, c, v, a, maker):
            if sig.bar == bar_idx:
                sig.strategy = name
                sig.pair = pair
                sig.tf = tf
                fired.append(sig)
    return fired


def ensemble_signals(signals, pair='', tf='', min_agree=2):
    groups = defaultdict(list)
    for sig in signals:
        groups[(sig.bar, sig.side)].append(sig)
    ens = []
    for (bar, side), sigs in groups.items():
        if len(sigs) >= min_agree:
            sds = sorted(s.stop_dist for s in sigs)
            median_sd = sds[len(sds)//2]
            names = '+'.join(s.strategy for s in sigs)
            ens.append(Signal(bar, side, sigs[0].entry, median_sd,
                              strategy=f"ENS{min_agree}({names})",
                              pair=pair, tf=tf))
    return ens
