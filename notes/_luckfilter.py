"""Rigorous market-level luck filter (read-only, runs off the cached trades).

Fixes the within-market correlation that inflates naive t-stats: collapse each
wallet's fills into ONE observation per (conditionId, outcomeIndex) position
(size-weighted mean entry price, single won outcome), then run the repo's
sign/Bernoulli-null luck filter + BH-FDR (scorer.py). The Bernoulli null also
subsumes the favorite-buyer issue: buying at 0.84 and winning is NOT significant
vs a Bernoulli(0.84) null. Reports raw + FDR survivors at a few position floors.
"""
from __future__ import annotations
import json, time
from collections import defaultdict
from pathlib import Path
import pandas as pd

from polymirror import polyapi
from polymirror import schema as S
from polymirror.scorer import select_eligible
from config import DEFAULT

# Same hardening as discovery (Windows atomic-write race) — harmless on cache hits.
def _robust_write_cache(p, data):
    import os
    tmp = p.with_suffix(".tmp"); payload = json.dumps(data, default=str)
    for a in range(6):
        try:
            tmp.write_text(payload, encoding="utf-8"); os.replace(tmp, p); return
        except PermissionError:
            time.sleep(0.2*(a+1))
    try: p.write_text(payload, encoding="utf-8")
    except PermissionError: pass
polyapi.PolyClient._write_cache = staticmethod(_robust_write_cache)

NOW = int(time.time())
START = polyapi._parse_iso_to_unix("2026-01-01T00:00:00Z")

def build_positions() -> pd.DataFrame:
    cli = polyapi.PolyClient()
    universe = cli.list_resolved_binary_markets(
        min_volume_usd=50_000, start_unix=START, end_unix=NOW,
        require_order_book=True, max_markets=60, page_budget=80)
    print(f"[lf] {len(universe)} markets (cache)")
    # (wallet, cond, oi) -> [sum_size, sum_size*price, n_fills, min_ts, wi]
    pos = defaultdict(lambda: [0.0, 0.0, 0, 1 << 62, None])
    for m in universe:
        cond = m["conditionId"]; wi = m["winning_index"]
        if wi not in (0, 1):
            continue
        for t in cli.get_trades(cond, type="TRADE"):
            if t.get("side") != "BUY":
                continue
            w = t.get("proxyWallet"); oi = t.get("outcomeIndex")
            p = t.get("price"); ts = t.get("timestamp"); sz = t.get("size")
            if w is None or oi is None or p is None:
                continue
            try:
                p = float(p); oi = int(oi); ts = int(ts); sz = float(sz or 0.0)
            except (TypeError, ValueError):
                continue
            if not (0.0 < p < 1.0):
                continue
            sz = sz if sz > 0 else 1.0
            k = (w, cond, oi)
            a = pos[k]
            a[0] += sz; a[1] += sz*p; a[2] += 1; a[3] = min(a[3], ts); a[4] = wi
    rows = []
    for (w, cond, oi), (ssz, swp, n, ts, wi) in pos.items():
        price = swp/ssz if ssz else None
        if price is None or not (0.0 < price < 1.0):
            continue
        won = 1 if oi == wi else 0
        rows.append({S.WALLET: w, S.CONDITION_ID: cond, S.TIMESTAMP: ts,
                     S.PRICE: price, S.SIDE: "BUY", S.OUTCOME_INDEX: oi,
                     S.SIZE: ssz, S.USDC_SIZE: ssz*price,
                     S.WINNING_INDEX: wi, S.WON: won, S.ENTRY_PROB: price})
    df = pd.DataFrame(rows)
    print(f"[lf] {len(df)} positions across {df[S.WALLET].nunique()} wallets "
          f"({df[S.CONDITION_ID].nunique()} markets)")
    return df

def main():
    df = build_positions()
    npos = df.groupby(S.WALLET).size()
    for floor in (8, 10, 15):
        cfg = DEFAULT.with_(min_trades_per_wallet=floor, accuracy_metric="brier",
                            train_end=None, fdr=True, n_bootstrap=10_000)
        eligible, table = select_eligible(df, cfg)
        nt = table.attrs.get("n_tested", 0)
        nraw = table.attrs.get("n_eligible_raw", 0)
        nfdr = table.attrs.get("n_eligible_fdr", 0)
        bh = table.attrs.get("bh_crit", float("nan"))
        print(f"\n=== position-floor >= {floor}: tested={nt}  raw_survivors={nraw}  "
              f"FDR_survivors={nfdr}  (BH crit p={bh:.4g}) ===")
        # show FDR survivors (or raw if none) with breadth
        flag = "eligible_fdr" if nfdr else "eligible_raw"
        surv = table[table[flag]].copy()
        # attach breadth (distinct markets) for context
        mkts = df.groupby(S.WALLET)[S.CONDITION_ID].nunique()
        surv["n_markets"] = surv[S.WALLET].map(mkts)
        if floor == 8:
            surv.to_json(Path(__file__).parent / "_survivors.json", orient="records", indent=2)
        cols = [S.WALLET, "n_trades", "n_markets", "brier", "null_mean",
                "null_margin", "p_value", "eligible_raw", "eligible_fdr"]
        if len(surv):
            print(surv[cols].to_string(index=False))
        else:
            print("  (no survivors at this floor)")

if __name__ == "__main__":
    main()
