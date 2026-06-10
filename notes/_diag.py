"""Diagnostics for the market-level luck filter: p-value distribution, best
wallets regardless of significance, and where the earlier Tier-A names landed."""
from __future__ import annotations
import json, time
from collections import defaultdict
import numpy as np, pandas as pd
from polymirror import polyapi, schema as S
from polymirror.scorer import score_wallets, luck_filter
from config import DEFAULT

def _rw(p,d):
    import os
    tmp=p.with_suffix(".tmp")
    try: tmp.write_text(json.dumps(d,default=str),encoding="utf-8"); os.replace(tmp,p)
    except PermissionError: pass
polyapi.PolyClient._write_cache=staticmethod(_rw)

NOW=int(time.time()); START=polyapi._parse_iso_to_unix("2026-01-01T00:00:00Z")
cli=polyapi.PolyClient()
universe=cli.list_resolved_binary_markets(min_volume_usd=50_000,start_unix=START,end_unix=NOW,
    require_order_book=True,max_markets=60,page_budget=80)
pos=defaultdict(lambda:[0.0,0.0,0,None])
for m in universe:
    cond=m["conditionId"]; wi=m["winning_index"]
    if wi not in (0,1): continue
    for t in cli.get_trades(cond,type="TRADE"):
        if t.get("side")!="BUY": continue
        w=t.get("proxyWallet"); oi=t.get("outcomeIndex"); p=t.get("price"); sz=t.get("size")
        if w is None or oi is None or p is None: continue
        try: p=float(p); oi=int(oi); sz=float(sz or 1.0)
        except: continue
        if not (0<p<1): continue
        sz=sz if sz>0 else 1.0
        a=pos[(w,cond,oi)]; a[0]+=sz; a[1]+=sz*p; a[2]+=1; a[3]=wi
rows=[]
for (w,cond,oi),(ssz,swp,n,wi) in pos.items():
    pr=swp/ssz
    if not (0<pr<1): continue
    rows.append({S.WALLET:w,S.CONDITION_ID:cond,S.TIMESTAMP:1700000000,S.PRICE:pr,S.SIDE:"BUY",
        S.OUTCOME_INDEX:oi,S.SIZE:ssz,S.USDC_SIZE:ssz*pr,S.WINNING_INDEX:wi,S.WON:1 if oi==wi else 0,
        S.ENTRY_PROB:pr})
df=pd.DataFrame(rows)
mkts=df.groupby(S.WALLET)[S.CONDITION_ID].nunique()

cfg=DEFAULT.with_(min_trades_per_wallet=8,accuracy_metric="brier",train_end=None,fdr=True,n_bootstrap=10000)
scores=score_wallets(df,cfg)
table=luck_filter(scores,df,cfg)
table["n_markets"]=table[S.WALLET].map(mkts)
pv=table["p_value"].to_numpy()
print(f"tested={len(table)}  min_p={pv.min():.4f}  p<0.05={int((pv<0.05).sum())}  p<0.10={int((pv<0.10).sum())}")
print("p-value distribution (deciles):")
for lo in np.arange(0,1,0.1):
    print(f"  [{lo:.1f},{lo+0.1:.1f}): {int(((pv>=lo)&(pv<lo+0.1)).sum()):>4}")
print(f"mean null_margin (obs-null; >0 = WORSE than no-skill): {table['null_margin'].mean():+.4f}  "
      f"frac worse-than-null: {(table['null_margin']>0).mean():.2f}")
print("\n--- 15 BEST wallets by p_value (market-level) ---")
best=table.sort_values('p_value').head(15)
print(best[[S.WALLET,'n_trades','n_markets','brier','null_mean','null_margin','p_value']].to_string(index=False))

tierA=["0xc6587b11a2209e46dfe3928b31c5514a8e33b784","0xc6dd722558dbfbd8fa780efcbe819ed8c6604b9f",
"0x428208cf4ff7b0aaede6dfd86969cfc823f93455","0xdf17f4a8dd01a4cfa6fc3da323a2baee5f8697d1",
"0x7c3db723f1d4d8cb9c550095203b686cb11e5c6b","0xed457c7e2e3e28cf8129502010fcf8ae5b970d29",
"0xb3f15cc1478d529862282c40c1e91399724dbdc6","0xb03b826a4fc9893b35d3ddf4f11be824525b6ca1"]
print("\n--- where the earlier Tier-A names landed (n_pos = distinct positions) ---")
allpos=df.groupby(S.WALLET).size()
for w in tierA:
    npos=int(allpos.get(w,0)); nm=int(mkts.get(w,0))
    r=table[table[S.WALLET]==w]
    if len(r):
        rr=r.iloc[0]
        print(f"{w}  n_pos={npos:>3} n_mkts={nm:>2} brier={rr['brier']:.3f} null={rr['null_mean']:.3f} p={rr['p_value']:.3f}")
    else:
        print(f"{w}  n_pos={npos:>3} n_mkts={nm:>2}  (below floor=8, not tested)")
