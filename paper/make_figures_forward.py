"""Generate the forward-experiment figure set (vector PDFs) for paper/figs/.
Pure-local: reads data/forward/experiment.json, no network. Run from repo root:
  .venv\\Scripts\\python.exe paper\\make_figures_forward.py
"""
from __future__ import annotations
import json, sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from polymirror.stats import bootstrap_mean_ci, bootstrap_two_sided_p
from config import DEFAULT

ROOT = Path(__file__).resolve().parents[1]
FIGS = Path(__file__).resolve().parent / "figs"
FIGS.mkdir(exist_ok=True)
PNGS = Path(__file__).resolve().parent / "figs_png"
PNGS.mkdir(exist_ok=True)
SEED = DEFAULT.seed


def _save(fig, stem):
    fig.savefig(FIGS / f"{stem}.pdf")
    fig.savefig(PNGS / f"{stem}.png", dpi=300)

# "Classroom-clean" design system: ink text, thin 1pt ink rules, white figure
# backgrounds (the same PDFs go into the research paper).
INK = "#1F1F1F"
GREEN = "#1E7A52"   # mirror strategy / money
GRAY  = "#6B7280"   # benchmark (favorite)
GOLD  = "#D98E04"   # edge / highlight
BRICK = "#B23A2F"   # losses / warnings

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 12,
    "axes.grid": True, "grid.alpha": 0.18, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.edgecolor": INK, "axes.linewidth": 1.0,
    "axes.labelcolor": INK, "axes.labelweight": "bold",
    "xtick.color": INK, "ytick.color": INK,
    "axes.titlecolor": INK, "axes.titlesize": 12.5, "axes.titleweight": "bold",
    "figure.facecolor": "white", "axes.facecolor": "white",
    "figure.dpi": 120, "savefig.bbox": "tight", "savefig.facecolor": "white",
    "legend.frameon": False,
})


def sparse_hours(H, step=None):
    """Tick locations every `step` hours (auto 4h, 6h once span exceeds 24h).
    Always keeps the first horizon so the axis starts labelled."""
    H = sorted(H)
    if not H:
        return []
    if step is None:
        step = 6 if max(H) > 24 else 4
    ticks = [h for h in H if h % step == 0]
    if ticks and ticks[0] == H[0]:
        return ticks
    return [H[0]] + ticks


def load():
    s = json.loads((ROOT / "data" / "forward" / "experiment.json").read_text(encoding="utf-8"))
    return list(s["positions"].values()), s["meta"]


def ret(e, x):
    return None if (e is None or x is None or e <= 0) else x / e - 1.0


def cells_at(pos, h):
    """(wallet,market,side) -> (mean strat, mean bench, mean edge) at horizon h."""
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


def main():
    pos, meta = load()
    HS = horizons_present(pos)

    # Precompute per-horizon aggregates
    agg = {}
    for h in HS:
        c = cells_at(pos, h)
        if not c:
            continue
        strat = np.array([v[0] for v in c.values()])
        bench = np.array([v[1] for v in c.values()])
        edge = np.array([v[2] for v in c.values()])
        # cluster by wallet
        bw_s, bw_b, bw_e = defaultdict(list), defaultdict(list), defaultdict(list)
        for (w, _m, _e), v in c.items():
            bw_s[w].append(v[0]); bw_b[w].append(v[1]); bw_e[w].append(v[2])
        ws = np.array([np.mean(x) for x in bw_s.values()])
        wb = np.array([np.mean(x) for x in bw_b.values()])
        we = np.array([np.mean(x) for x in bw_e.values()])
        em, lo_m, hi_m = bootstrap_mean_ci(edge, 10000, SEED)
        ew, lo_w, hi_w = bootstrap_mean_ci(we, 10000, SEED)
        agg[h] = dict(strat=strat, bench=bench, edge=edge, n=len(edge),
                      ws=ws, wb=wb, we=we, nw=len(we),
                      edge_m=em, lo_m=lo_m, hi_m=hi_m,
                      edge_w=ew, lo_w=lo_w, hi_w=hi_w,
                      strat_mean=ws.mean(), bench_mean=wb.mean())
    H = sorted(agg)

    # ---- F1: edge-decay curve (three lines) ---------------------------------
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(H, [agg[h]["strat_mean"] for h in H], "-o", color=GREEN, lw=2, label="Mirror (strategy)")
    ax.plot(H, [agg[h]["bench_mean"] for h in H], "-s", color=GRAY, lw=2, label="Favorite (benchmark)")
    ax.plot(H, [agg[h]["edge_w"] for h in H], "--^", color=GOLD, lw=2, label="Edge = mirror $-$ favorite")
    ax.axhline(0, color=INK, lw=0.8, ls=":")
    ax.set_xlabel("Holding horizon $N$ (hours)"); ax.set_ylabel("Gross return")
    ax.set_xticks(sparse_hours(H)); ax.legend(loc="lower left", frameon=False)
    _save(fig, 'fig_edge_curve'); plt.close(fig)

    # ---- F2: edge with bootstrap CI bands (both clusterings) ----------------
    fig, ax = plt.subplots(figsize=(7, 4))
    em = np.array([agg[h]["edge_m"] for h in H])
    lom = np.array([agg[h]["lo_m"] for h in H]); him = np.array([agg[h]["hi_m"] for h in H])
    ew = np.array([agg[h]["edge_w"] for h in H])
    low = np.array([agg[h]["lo_w"] for h in H]); hiw = np.array([agg[h]["hi_w"] for h in H])
    ax.fill_between(H, lom, him, color=GRAY, alpha=0.25, label="by-market 95% CI")
    ax.plot(H, em, "-o", color=INK, lw=1.8, label="edge (by market)")
    n_wallets = max(agg[h]["nw"] for h in H)          # data-driven, not hardcoded
    dx = 0.008 * max(1, H[-1] - H[0])                 # x-offset scales with axis span
    ax.errorbar([h + dx for h in H], ew, yerr=[ew - low, hiw - ew], fmt="D", color=GOLD,
                capsize=3, lw=1.5,
                label=f"edge (by wallet, $n{{=}}{n_wallets}$) $\\pm$95% CI")
    ax.axhline(0, color=BRICK, lw=1.0, ls="--")
    ax.set_xlabel("Holding horizon $N$ (hours)"); ax.set_ylabel("Edge (gross)")
    ax.set_xticks(sparse_hours(H)); ax.legend(loc="upper left", frameon=False, fontsize=9)
    _save(fig, 'fig_edge_ci'); plt.close(fig)

    # ---- F3: return distribution by horizon (boxplots, strat vs bench) ------
    # Subset horizons (48 hourly pairs is unreadable); categorical x positions
    # keep the boxes evenly spaced despite the uneven hour gaps.
    BOX_HS = [h for h in (1, 2, 4, 8, 12, 24, 36, 48) if h in agg]
    fig, ax = plt.subplots(figsize=(7.5, 4))
    width = 0.36
    xs = np.arange(len(BOX_HS), dtype=float)
    data_s = [agg[h]["strat"] for h in BOX_HS]; data_b = [agg[h]["bench"] for h in BOX_HS]
    bp1 = ax.boxplot(data_s, positions=xs - 0.2, widths=width,
                     patch_artist=True, showfliers=False)
    bp2 = ax.boxplot(data_b, positions=xs + 0.2, widths=width,
                     patch_artist=True, showfliers=False)
    for b in bp1["boxes"]: b.set(facecolor=GREEN, alpha=0.55)
    for b in bp2["boxes"]: b.set(facecolor=GRAY, alpha=0.55)
    for bp in (bp1, bp2):
        for m in bp["medians"]: m.set(color=INK, lw=1.2)
    ax.axhline(0, color=INK, lw=0.8, ls=":")
    ax.set_xticks(xs); ax.set_xticklabels(BOX_HS)
    ax.set_xlabel("Holding horizon $N$ (hours)"); ax.set_ylabel("Per-position gross return")
    ax.legend([bp1["boxes"][0], bp2["boxes"][0]], ["Mirror", "Favorite"],
              loc="lower left", frameon=False)
    _save(fig, 'fig_return_box'); plt.close(fig)

    # ---- F4: sample size per horizon ----------------------------------------
    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    ax.bar(H, [agg[h]["n"] for h in H], color=GREEN, alpha=0.8)
    n_max = max(agg[h]["n"] for h in H)
    for h in H:                       # value-labels every 4th hour only
        if h % 4 == 0 or h == H[0]:
            ax.text(h, agg[h]["n"] + 0.02 * n_max, str(agg[h]["n"]),
                    ha="center", fontsize=8)
    ax.set_xticks(sparse_hours(H)); ax.set_xlabel("Holding horizon $N$ (hours)")
    ax.set_ylabel("Market-cells (deduped)")
    _save(fig, 'fig_sample_size'); plt.close(fig)

    # ---- F5: per-wallet activity --------------------------------------------
    byw = defaultdict(int)
    for p in pos:
        byw[p["name"] or p["wallet"][:8]] += 1
    items = sorted(byw.items(), key=lambda kv: kv[1])
    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    ax.barh([k for k, _ in items], [v for _, v in items], color=GREEN, alpha=0.85)
    v_max = max(v for _, v in items)
    for i, (_, v) in enumerate(items):
        ax.text(v + 0.015 * v_max, i, str(v), va="center", fontsize=8)
    ax.set_xlabel("Positions captured")
    _save(fig, 'fig_wallets'); plt.close(fig)

    # ---- F6: entry-price distribution ---------------------------------------
    eps = np.array([p["entry_price"] for p in pos])
    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    ax.hist(eps, bins=np.linspace(0, 1, 26), color=GREEN, alpha=0.8, edgecolor="white")
    ax.axvline(0.5, color=GRAY, ls="--", lw=1.2, label="favorite threshold (0.5)")
    ax.set_xlabel("Entry price (implied probability of touched token)")
    ax.set_ylabel("Positions"); ax.legend(frameon=False, fontsize=9)
    _save(fig, 'fig_entry_price'); plt.close(fig)

    # ---- F7: fills-per-market distribution (log y) --------------------------
    bym = defaultdict(int)
    for p in pos:
        bym[p["condition_id"]] += 1
    fills = np.array(sorted(bym.values()))
    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    ax.hist(fills, bins=np.logspace(0, np.log10(fills.max() + 1), 30),
            color=GRAY, alpha=0.85, edgecolor="white")
    ax.set_xscale("log")
    ax.axvline(np.median(fills), color=GREEN, ls="--", lw=1,
               label=f"median {int(np.median(fills))}")
    ax.axvline(fills.max(), color=BRICK, ls="--", lw=1, label=f"max {fills.max()}")
    ax.set_xlabel("Fills per market (log scale)"); ax.set_ylabel("Markets")
    ax.legend(frameon=False, fontsize=9)
    _save(fig, 'fig_fills_per_market'); plt.close(fig)

    # ---- F8: mark-source composition by horizon (stacked) -------------------
    hist_n, settle_n = [], []
    for h in HS:
        hh = ss = 0
        for p in pos:
            mk = p["marks"].get(str(h))
            if not mk:
                continue
            if mk["source"] == "history":
                hh += 1
            elif mk["source"] == "settle":
                ss += 1
        hist_n.append(hh); settle_n.append(ss)
    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    ax.bar(HS, hist_n, color=GREEN, alpha=0.8, label="live history mark")
    ax.bar(HS, settle_n, bottom=hist_n, color=GOLD, alpha=0.8, label="settled 0/1")
    ax.set_xticks(sparse_hours(HS)); ax.set_xlabel("Holding horizon $N$ (hours)")
    ax.set_ylabel("Marks"); ax.legend(frameon=False, fontsize=9)
    _save(fig, 'fig_mark_sources'); plt.close(fig)

    print("wrote forward figures to", FIGS)
    for f in sorted(FIGS.glob("fig_*.pdf")):
        print("  ", f.name)


if __name__ == "__main__":
    main()
