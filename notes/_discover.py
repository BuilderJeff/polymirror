"""One-shot SELECTION discovery (read-only). Scores wallets on recent resolved
binary markets to build a candidate watchlist. Skill signal = mean(y - p) over a
wallet's BUY entries (won minus entry price): >0 means it beat the market price,
not merely bought favorites. Brier = calibration. n = resolved BUYs (evidence).
Writes notes/_candidates.json. Recency (48h) is checked separately afterward.
"""
from __future__ import annotations
import json, time, math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from polymirror import polyapi

# Windows atomic-write hardening: os.replace() can transiently fail with WinError 32
# when Defender/the indexer briefly locks the just-written .tmp file. Retry a few
# times, then fall back to a direct (non-atomic) write rather than crash the run.
def _robust_write_cache(p, data):
    import os
    tmp = p.with_suffix(".tmp")
    payload = json.dumps(data, default=str)
    for attempt in range(6):
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.2 * (attempt + 1))
    try:
        p.write_text(payload, encoding="utf-8")  # last resort: non-atomic
    except PermissionError:
        pass  # cache is an optimization; a miss is non-fatal
polyapi.PolyClient._write_cache = staticmethod(_robust_write_cache)

NOW = int(time.time())
START = polyapi._parse_iso_to_unix("2026-01-01T00:00:00Z")   # score on 2026 skill
END = NOW
MIN_VOL = 50_000
MAX_MARKETS = 60
PAGE_BUDGET = 80
MIN_RESOLVED_BUYS = 15      # skill-evidence floor (separate from the 48h activity gate)

def main():
    cli = polyapi.PolyClient()
    print(f"[discover] listing resolved binary markets vol>={MIN_VOL} closed in "
          f"2026-01-01..now (max {MAX_MARKETS})")
    universe = cli.list_resolved_binary_markets(
        min_volume_usd=MIN_VOL, start_unix=START, end_unix=END,
        require_order_book=True, max_markets=MAX_MARKETS, page_budget=PAGE_BUDGET)
    print(f"[discover] {len(universe)} markets")

    # per-wallet accumulation of (price, won, ts) over BUY entries
    wp = defaultdict(list)            # wallet -> list of (p, y, ts)
    wmkts = defaultdict(set)          # wallet -> set of conditionIds (breadth)
    wname = {}                        # wallet -> last seen display name
    n_trades = 0
    for i, m in enumerate(universe, 1):
        cond = m["conditionId"]; wi = m["winning_index"]
        if wi not in (0, 1):
            continue
        try:
            trades = cli.get_trades(cond, type="TRADE")
        except polyapi.PolyAPIError as e:
            print(f"  [{i}/{len(universe)}] skip {cond[:10]}: {e}")
            continue
        for t in trades:
            if t.get("side") != "BUY":
                continue
            oi = t.get("outcomeIndex"); p = t.get("price"); ts = t.get("timestamp")
            w = t.get("proxyWallet")
            if oi is None or p is None or w is None:
                continue
            try:
                p = float(p); oi = int(oi); ts = int(ts)
            except (TypeError, ValueError):
                continue
            if not (0.0 < p < 1.0):
                continue
            y = 1 if oi == wi else 0
            wp[w].append((p, y, ts))
            wmkts[w].add(cond)
            nm = t.get("name") or t.get("pseudonym")
            if nm:
                wname[w] = nm
            n_trades += 1
        print(f"  [{i}/{len(universe)}] {m.get('slug','')[:46]:46}  "
              f"buys+={sum(1 for t in trades if t.get('side')=='BUY')}")

    print(f"[discover] {n_trades} resolved BUY rows across {len(wp)} wallets")

    rows = []
    for w, lst in wp.items():
        n = len(lst)
        if n < MIN_RESOLVED_BUYS:
            continue
        ps = [a for a, _, _ in lst]; ys = [b for _, b, _ in lst]
        brier = sum((a - b) ** 2 for a, b in zip(ps, ys)) / n
        mean_edge = sum(b - a for a, b in zip(ps, ys)) / n     # won - price
        winrate = sum(ys) / n
        mean_p = sum(ps) / n
        # rough SE of mean_edge so we can see if edge is distinguishable from 0
        var = sum(((b - a) - mean_edge) ** 2 for a, b in zip(ps, ys)) / n
        se = math.sqrt(var / n) if n > 1 else float("nan")
        rows.append(dict(wallet=w, name=wname.get(w, ""), n=n,
                         n_markets=len(wmkts[w]), mean_edge=round(mean_edge, 4),
                         se=round(se, 4), t_stat=round(mean_edge / se, 2) if se else None,
                         brier=round(brier, 4), winrate=round(winrate, 3),
                         mean_p=round(mean_p, 3),
                         last_ts=max(c for _, _, c in lst)))
    rows.sort(key=lambda r: r["mean_edge"], reverse=True)
    out = Path(__file__).parent / "_candidates.json"
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"[discover] {len(rows)} wallets with >= {MIN_RESOLVED_BUYS} resolved BUYs "
          f"-> {out}")
    print("\nTOP 25 by mean_edge (won - entry price):")
    print(f"{'wallet':44} {'n':>4} {'mkts':>4} {'edge':>7} {'t':>5} {'brier':>6} {'win':>5} {'mean_p':>6}  name")
    for r in rows[:25]:
        print(f"{r['wallet']:44} {r['n']:>4} {r['n_markets']:>4} {r['mean_edge']:>7} "
              f"{str(r['t_stat']):>5} {r['brier']:>6} {r['winrate']:>5} {r['mean_p']:>6}  {r['name'][:20]}")

if __name__ == "__main__":
    main()
