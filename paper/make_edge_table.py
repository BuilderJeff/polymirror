"""Generate data/forward/edge_curve_final.csv — the per-horizon edge table WITH
bootstrap inference under both clusterings (by market-cell and by wallet).

Pure-local: reads data/forward/experiment.json, no network. Run from repo root:
  .venv\\Scripts\\python.exe paper\\make_edge_table.py

Same dedup as forward_pull.snapshot / make_figures_forward.cells_at: collapse
repeat fills to ONE position per (wallet, market, eff_side) BEFORE averaging,
then aggregate at the clustering unit (spec §7.5).
"""
from __future__ import annotations
import csv, json, sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from polymirror.stats import bootstrap_mean_ci, bootstrap_two_sided_p
from config import DEFAULT

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "forward" / "edge_curve_final.csv"
SEED = DEFAULT.seed
N_BOOT = 10_000


def _ret(e, x):
    return None if (e is None or x is None or e <= 0) else x / e - 1.0


def main() -> int:
    state = json.loads((ROOT / "data" / "forward" / "experiment.json").read_text(encoding="utf-8"))
    pos = list(state["positions"].values())
    horizons = sorted({int(h) for p in pos for h in p["marks"]})

    rows = []
    for h in horizons:
        cell = defaultdict(list)  # (wallet, market, eff_side) -> [(strat, bench)]
        for p in pos:
            mk = p["marks"].get(str(h))
            if not mk or mk.get("p0") is None:
                continue
            eff, fav = p["eff_index"], p["favorite_index"]
            em, mp = p["entry_mids"], [mk["p0"], mk["p1"]]
            st, bn = _ret(em[eff], mp[eff]), _ret(em[fav], mp[fav])
            if st is None or bn is None:
                continue
            cell[(p["wallet"], p["condition_id"], eff)].append((st, bn))
        if not cell:
            continue
        # per-cell means, then the two clusterings
        m_strat, m_bench, m_edge = [], [], []
        bw = defaultdict(lambda: ([], []))
        for (w, _m, _e), v in cell.items():
            s_ = sum(x[0] for x in v) / len(v)
            b_ = sum(x[1] for x in v) / len(v)
            m_strat.append(s_); m_bench.append(b_); m_edge.append(s_ - b_)
            bw[w][0].append(s_); bw[w][1].append(b_)
        ws = np.array([np.mean(s) for s, _ in bw.values()])
        wb = np.array([np.mean(b) for _, b in bw.values()])
        we = ws - wb
        me = np.array(m_edge)

        ew, lo_w, hi_w = bootstrap_mean_ci(we, N_BOOT, SEED)
        em_, lo_m, hi_m = bootstrap_mean_ci(me, N_BOOT, SEED)
        rows.append({
            "horizon_h": h, "n_wallets": len(we), "n_market_cells": len(me),
            "strat": round(float(ws.mean()), 6), "bench": round(float(wb.mean()), 6),
            "edge_by_wallet": round(float(ew), 6),
            "w_ci_lo": round(float(lo_w), 6), "w_ci_hi": round(float(hi_w), 6),
            "w_p": round(bootstrap_two_sided_p(we, N_BOOT, SEED), 6),
            "edge_by_market": round(float(em_), 6),
            "m_ci_lo": round(float(lo_m), 6), "m_ci_hi": round(float(hi_m), 6),
            "m_p": round(bootstrap_two_sided_p(me, N_BOOT, SEED), 6),
        })

    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUT} ({len(rows)} horizons)")
    for r in rows:
        print(f"  h={r['horizon_h']:>2} n={r['n_market_cells']:>4} "
              f"edge_w={r['edge_by_wallet']:+.4f} (p={r['w_p']:.3f}) "
              f"edge_m={r['edge_by_market']:+.4f} (p={r['m_p']:.3f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
