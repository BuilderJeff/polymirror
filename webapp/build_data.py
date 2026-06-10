"""build_data.py : export precomputed aggregates for the static webapp.

Reads data/forward/experiment.json (+ paper/study1_table.json if present) and
writes webapp/data.json. Pure-local and DATA-DRIVEN: every number is derived
from the input files at run time. Never writes under data/forward/.

Run from repo root:
  .venv\\Scripts\\python.exe webapp\\build_data.py
NOTE: with the full 48h dataset (experiment.json ~150 MB, 10k-draw bootstraps x47
horizons x2 clusterings) a run takes 30-60s : always run it in the BACKGROUND,
never under a 30s foreground timeout.
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from polymirror.stats import bootstrap_mean_ci, bootstrap_two_sided_p  # noqa: E402
from config import DEFAULT  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent / "data.json"
SEED = DEFAULT.seed
N_BOOT = 10_000
MARKET_EDGE_HORIZONS = [1, 6, 12, 24, 48]
PCTS = [5, 25, 50, 75, 95]


def load():
    s = json.loads((ROOT / "data" / "forward" / "experiment.json").read_text(encoding="utf-8"))
    return list(s["positions"].values()), s["meta"]


def ret(e, x):
    return None if (e is None or x is None or e <= 0) else x / e - 1.0


def cells_at(pos, h):
    """(wallet,market,side) -> (mean strat, mean bench, mean edge) at horizon h.

    Copied EXACTLY from paper/make_figures_forward.py: collapse to one cell per
    (wallet, market, eff_side) before any averaging, so repeated fills of the
    same bet do not get double-counted.
    """
    cell = defaultdict(list)
    for p in pos:
        mk = p["marks"].get(str(h))
        if not mk or mk.get("p0") is None:
            continue
        eff, fav = p["eff_index"], p["favorite_index"]
        em, mp = p["entry_mids"], [mk["p0"], mk["p1"]]
        st, bn = ret(em[eff], mp[eff]), ret(em[fav], mp[fav])
        if st is None or bn is None:
            continue
        cell[(p["wallet"], p["condition_id"], eff)].append((st, bn))
    out = {}
    for k, v in cell.items():
        s_ = sum(x[0] for x in v) / len(v)
        b_ = sum(x[1] for x in v) / len(v)
        out[k] = (s_, b_, s_ - b_)
    return out


def horizons_present(pos):
    hs = set()
    for p in pos:
        hs.update(int(h) for h in p["marks"])
    return sorted(hs)


def r(x, nd=5):
    """Round for JSON; None for non-finite."""
    if x is None:
        return None
    x = float(x)
    if not np.isfinite(x):
        return None
    return round(x, nd)


def iso(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def main():
    t0 = time.time()
    pos, meta = load()
    HS = horizons_present(pos)
    n_pos = len(pos)

    wallet_name = {}
    for p in pos:
        wallet_name.setdefault(p["wallet"], p.get("name") or p["wallet"][:8])

    # ---- meta ---------------------------------------------------------------
    n_markets = len({p["condition_id"] for p in pos})
    n_buys = sum(1 for p in pos if p.get("side") == "BUY")
    total_marks = sum(len(p["marks"]) for p in pos)
    meta_out = {
        "experiment_start": iso(meta["experiment_start"]),
        "experiment_end": iso(meta["experiment_end"]),
        "window_hours": meta.get("window_hours"),
        "last_pull": iso(meta["last_pull_ts"]) if meta.get("last_pull_ts") else None,
        "pulls": meta.get("pulls"),
        "n_positions": n_pos,
        "n_markets": n_markets,
        "n_wallets_active": len(wallet_name),
        "n_buys": n_buys,
        "n_sells": n_pos - n_buys,
        "total_marks": total_marks,
        "horizons": HS,
        "built": iso(time.time()),
        "seed": SEED,
        "n_boot": N_BOOT,
    }

    # ---- per-horizon aggregates: edge curve, distributions, per-wallet -------
    edge_curve, dist, mark_sources = [], [], []
    pw_curve = defaultdict(list)          # wallet -> [{h,...}]
    market_edge = defaultdict(dict)       # condition_id -> {h: mean edge}
    for h in HS:
        c = cells_at(pos, h)
        # mark-source composition (counts over ALL positions at this horizon)
        hist_n = settle_n = 0
        for p in pos:
            mk = p["marks"].get(str(h))
            if mk and mk.get("p0") is not None and mk.get("source") == "history":
                hist_n += 1
            elif mk and mk.get("p0") is not None and mk.get("source") == "settle":
                settle_n += 1
        mark_sources.append({"h": h, "history": hist_n, "settle": settle_n,
                             "missing": n_pos - hist_n - settle_n})
        if not c:
            continue
        strat = np.array([v[0] for v in c.values()])
        bench = np.array([v[1] for v in c.values()])
        edge = np.array([v[2] for v in c.values()])
        # cluster by wallet (same as paper figure script)
        bw_s, bw_b, bw_e = defaultdict(list), defaultdict(list), defaultdict(list)
        for (w, _m, _e), v in c.items():
            bw_s[w].append(v[0]); bw_b[w].append(v[1]); bw_e[w].append(v[2])
        ws = np.array([np.mean(x) for x in bw_s.values()])
        wb = np.array([np.mean(x) for x in bw_b.values()])
        we = np.array([np.mean(x) for x in bw_e.values()])
        em, lo_m, hi_m = bootstrap_mean_ci(edge, N_BOOT, SEED)
        ew, lo_w, hi_w = bootstrap_mean_ci(we, N_BOOT, SEED)
        p_m = bootstrap_two_sided_p(edge, N_BOOT, SEED)
        p_w = bootstrap_two_sided_p(we, N_BOOT, SEED)
        edge_curve.append({
            "h": h, "n_cells": int(len(edge)), "n_wallets": int(len(we)),
            "strat": r(ws.mean()), "bench": r(wb.mean()),
            "strat_cell": r(strat.mean()), "bench_cell": r(bench.mean()),
            "edge_m": r(em), "lo_m": r(lo_m), "hi_m": r(hi_m), "p_m": r(p_m),
            "edge_w": r(ew), "lo_w": r(lo_w), "hi_w": r(hi_w), "p_w": r(p_w),
        })
        dist.append({
            "h": h, "n": int(len(strat)),
            "strat": [r(np.percentile(strat, q)) for q in PCTS],
            "bench": [r(np.percentile(bench, q)) for q in PCTS],
        })
        # per-wallet means at this horizon
        for w in bw_e:
            pw_curve[w].append({
                "h": h, "n": len(bw_e[w]),
                "strat": r(np.mean(bw_s[w])), "bench": r(np.mean(bw_b[w])),
                "edge": r(np.mean(bw_e[w])),
            })
        # per-market mean edge at the table horizons
        if h in MARKET_EDGE_HORIZONS:
            bym = defaultdict(list)
            for (_w, m, _e), v in c.items():
                bym[m].append(v[2])
            for m, vals in bym.items():
                market_edge[m][str(h)] = r(float(np.mean(vals)))
        print(f"  h={h:>2}  cells={len(edge):>4}  wallets={len(we)}  "
              f"edge_m={em:+.4f} [{lo_m:+.4f},{hi_m:+.4f}] p={p_m:.3f}", flush=True)

    # ---- per_wallet -----------------------------------------------------------
    byw_pos = defaultdict(int)
    for p in pos:
        byw_pos[p["wallet"]] += 1
    per_wallet = sorted(
        ({"name": wallet_name[w], "wallet": w, "n_positions": byw_pos[w],
          "curve": pw_curve.get(w, [])} for w in byw_pos),
        key=lambda d: -d["n_positions"])

    # ---- markets ---------------------------------------------------------------
    bym_pos = defaultdict(list)
    for p in pos:
        bym_pos[p["condition_id"]].append(p)
    markets = []
    eh = [h for h in MARKET_EDGE_HORIZONS if h in HS]
    for m, ps in bym_pos.items():
        cells = {(p["wallet"], p["eff_index"]) for p in ps}
        title = next((p.get("title") for p in ps if p.get("title")), None)
        slug = next((p.get("slug") for p in ps if p.get("slug")), None)
        markets.append({
            "title": title or slug or m[:14],
            "slug": slug,
            "n_fills": len(ps),
            "n_cells": len(cells),
            "resolved": bool(any(p.get("resolved") for p in ps)),
            "entry": r(float(np.mean([p["entry_price"] for p in ps])), 4),
            "edge": {str(h): market_edge.get(m, {}).get(str(h)) for h in eh},
        })
    markets.sort(key=lambda d: -d["n_fills"])

    # ---- entries timeline + entry-price histogram ------------------------------
    wh = int(meta.get("window_hours") or 48)
    t_start = meta["experiment_start"]
    counts = defaultdict(int)
    for p in pos:
        hh = int((p["entry_ts"] - t_start) // 3600)
        counts[min(max(hh, 0), wh - 1)] += 1
    entries_timeline = [{"h": hh, "n": counts.get(hh, 0)} for hh in range(wh)]

    eps = np.array([p["entry_price"] for p in pos], dtype=float)
    edges = np.linspace(0.0, 1.0, 26)
    hist, _ = np.histogram(eps, bins=edges)
    entry_hist = {"edges": [r(e, 3) for e in edges], "counts": [int(x) for x in hist]}

    # ---- study 1 ----------------------------------------------------------------
    s1_path = ROOT / "paper" / "study1_table.json"
    study1 = {"pending": True, "n_tested": 151, "n_passed": 0, "min_p": 0.305}
    if s1_path.exists():
        raw = json.loads(s1_path.read_text(encoding="utf-8"))
        rows = raw if isinstance(raw, list) else raw.get("rows", raw)
        study1 = {"pending": False, "n_tested": 151, "n_passed": 0, "min_p": 0.305,
                  "rows": rows}

    out = {
        "meta": meta_out,
        "edge_curve": edge_curve,
        "per_wallet": per_wallet,
        "markets": markets,
        "entries_timeline": entries_timeline,
        "entry_hist": entry_hist,
        "dist": dist,
        "mark_sources": mark_sources,
        "study1": study1,
    }
    OUT.write_text(json.dumps(out, separators=(",", ":"), allow_nan=False),
                   encoding="utf-8")
    kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT}  ({kb:.1f} KB, {len(markets)} markets, "
          f"{len(edge_curve)} horizons) in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
