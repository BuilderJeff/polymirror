"""Generate Study-1 (historical luck-filter) figures for paper/figs/.
Rebuilds collapsed positions from the CACHED trade pulls, runs the repo scorer +
luck filter, and plots the p-value distribution, observed-vs-null Brier scatter,
and null-margin distribution. Run from repo root.
"""
from __future__ import annotations
import json, time, sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from polymirror import polyapi, schema as S
from polymirror.scorer import score_wallets, luck_filter
from config import DEFAULT

FIGS = Path(__file__).resolve().parent / "figs"; FIGS.mkdir(exist_ok=True)
PNGS = Path(__file__).resolve().parent / "figs_png"; PNGS.mkdir(exist_ok=True)


def _save(fig, stem):
    fig.savefig(FIGS / f"{stem}.pdf")
    fig.savefig(PNGS / f"{stem}.png", dpi=300)
# "Classroom-clean" design system: ink text, thin 1pt ink rules, white figure
# backgrounds (the same PDFs go into the research paper).
INK = "#1F1F1F"
GREEN = "#1E7A52"   # mirror strategy / money
GRAY  = "#6B7280"   # benchmark / null
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

# Cache-only writes (mirror the notes scripts' Windows hardening); harmless on hits.
def _rw(p, d):
    import os
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(d, default=str), encoding="utf-8"); os.replace(tmp, p)
    except PermissionError:
        pass
polyapi.PolyClient._write_cache = staticmethod(_rw)

NOW = int(time.time())
START = polyapi._parse_iso_to_unix("2026-01-01T00:00:00Z")


def build_positions():
    cli = polyapi.PolyClient()
    universe = cli.list_resolved_binary_markets(
        min_volume_usd=50_000, start_unix=START, end_unix=NOW,
        require_order_book=True, max_markets=60, page_budget=80)
    pos = defaultdict(lambda: [0.0, 0.0, 0, 1 << 62, None])
    for m in universe:
        cond, wi = m["conditionId"], m["winning_index"]
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
            a = pos[(w, cond, oi)]
            a[0] += sz; a[1] += sz * p; a[2] += 1; a[3] = min(a[3], ts); a[4] = wi
    rows = []
    for (w, cond, oi), (ssz, swp, n, ts, wi) in pos.items():
        pr = swp / ssz if ssz else None
        if pr is None or not (0.0 < pr < 1.0):
            continue
        rows.append({S.WALLET: w, S.CONDITION_ID: cond, S.TIMESTAMP: ts, S.PRICE: pr,
                     S.SIDE: "BUY", S.OUTCOME_INDEX: oi, S.SIZE: ssz, S.USDC_SIZE: ssz * pr,
                     S.WINNING_INDEX: wi, S.WON: 1 if oi == wi else 0, S.ENTRY_PROB: pr})
    return pd.DataFrame(rows)


def main():
    df = build_positions()
    cfg = DEFAULT.with_(min_trades_per_wallet=8, accuracy_metric="brier",
                        train_end=None, fdr=True, n_bootstrap=10_000)
    scores = score_wallets(df, cfg)
    table = luck_filter(scores, df, cfg)

    # Export the luck-filter table for the web app (no scorer re-run needed).
    tbl = table.reset_index() if "wallet" not in table.columns else table
    keep = [c for c in ("wallet", "n", "n_trades", "brier",
                        "null_mean", "null_margin", "p_value") if c in tbl.columns]
    table_json = Path(__file__).resolve().parent / "study1_table.json"
    table_json.write_text(
        json.dumps(tbl[keep].to_dict(orient="records"), indent=2), encoding="utf-8")
    print(f"wrote {table_json} ({len(tbl)} rows)")

    pv = table["p_value"].to_numpy()
    brier = table["brier"].to_numpy()
    null = table["null_mean"].to_numpy()
    margin = table["null_margin"].to_numpy()
    print(f"tested={len(table)} min_p={pv.min():.3f} p<0.05={(pv<0.05).sum()}")

    # ---- S1: p-value distribution (deciles) with uniform-null expectation ---
    fig, ax = plt.subplots(figsize=(7, 4))
    bins = np.linspace(0, 1, 11)
    counts, _ = np.histogram(pv, bins=bins)
    ax.bar(bins[:-1] + 0.05, counts, width=0.09, color=GREEN, alpha=0.85,
           edgecolor="white", label="observed")
    ax.axhline(len(pv) / 10, color=GRAY, ls="--", lw=1.5,
               label=f"uniform-null expectation ({len(pv)/10:.1f}/bin)")
    ax.axvline(0.05, color=INK, ls=":", lw=1)
    ax.text(0.06, ax.get_ylim()[1] * 0.85,
            f"$p<0.05$:\n{(pv < 0.05).sum()} wallets", fontsize=9)
    ax.set_xlabel("Luck-null $p$-value"); ax.set_ylabel("Wallets")
    ax.set_title(f"Study 1: {len(pv)} wallets tested, {(pv < 0.05).sum()} below 0.05 "
                 f"(min $p={pv.min():.3f}$)", fontsize=11)
    ax.legend(frameon=False)
    _save(fig, 'fig_s1_pvalue_dist'); plt.close(fig)

    # ---- S2: observed Brier vs price-null Brier scatter ---------------------
    fig, ax = plt.subplots(figsize=(5.4, 5.0))
    worse = brier > null
    ax.scatter(null[~worse], brier[~worse], s=22, color=GREEN, alpha=0.7,
               label="better than null")
    ax.scatter(null[worse], brier[worse], s=22, color=BRICK, alpha=0.7,
               label="worse than null")
    lim = max(brier.max(), null.max()) * 1.05
    ax.plot([0, lim], [0, lim], ls="--", color=INK, lw=1, label="observed = null")
    ax.set_xlim(0, lim); ax.set_ylim(0, lim)
    ax.set_xlabel("Price-null Brier (no skill)"); ax.set_ylabel("Observed Brier")
    ax.set_title("Study 1: no wallet beats its price-null significantly", fontsize=10.5)
    ax.legend(frameon=False, fontsize=9)
    _save(fig, 'fig_s1_brier_scatter'); plt.close(fig)

    # ---- S3: null-margin distribution ---------------------------------------
    fig, ax = plt.subplots(figsize=(7, 3.6))
    ax.hist(margin, bins=30, color=GRAY, alpha=0.8, edgecolor="white")
    ax.axvline(0, color=INK, ls=":", lw=1)
    ax.axvline(margin.mean(), color=BRICK, ls="--", lw=1.5,
               label=f"mean {margin.mean():+.4f}")
    ax.set_xlabel("Null margin  (observed $-$ null Brier;  $>0$ = worse than no skill)")
    ax.set_ylabel("Wallets"); ax.legend(frameon=False, fontsize=9)
    _save(fig, 'fig_s1_null_margin'); plt.close(fig)

    print("wrote Study-1 figures to", FIGS)


if __name__ == "__main__":
    main()
