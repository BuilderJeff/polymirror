"""plots.py — render the research figures from the CSVs run.py writes.

    ./.venv/Scripts/python.exe plots.py

Produces:
    results/decay_curve.png        edge = strategy - benchmark vs holding horizon N,
                                   one line per spread preset, with bootstrap CI bands (THE headline plot)
    results/accuracy_vs_profit.png per-wallet training Brier vs test mirrored return (§7.4)

Decoupled from compute: reads results/*.csv only. Uses the non-interactive Agg backend.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker
import pandas as pd

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"

PRESET_STYLE = {
    "optimistic":   dict(color="#2a9d8f", marker="o"),
    "base":         dict(color="#264653", marker="s"),
    "conservative": dict(color="#e76f51", marker="^"),
}


def decay_curve() -> None:
    f = RESULTS / "returns_by_preset_N.csv"
    if not f.exists():
        print(f"[plots] skip decay_curve: {f} missing")
        return
    df = pd.read_csv(f)
    fig, ax = plt.subplots(figsize=(8, 5))
    for preset, g in df.groupby("preset"):
        g = g.sort_values("N")
        style = PRESET_STYLE.get(preset, dict())
        ax.plot(g["N"], g["mean_edge"], label=preset, linewidth=2, **style)
        ax.fill_between(g["N"], g["edge_lo"], g["edge_hi"], alpha=0.15,
                        color=style.get("color"))
    ax.axhline(0.0, color="#888", linestyle="--", linewidth=1, zorder=0)
    ax.set_xscale("log")
    ax.set_xticks(sorted(df["N"].unique()))
    ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    ax.set_xlabel("holding horizon N (hours, log scale)")
    ax.set_ylabel("edge = mirror − buy-the-favorite (net return)")
    ax.set_title("Edge-decay curve, by spread preset (95% bootstrap CI)\n"
                 "headline cell: Brier selection, min_trades=30")
    ax.legend(title="spread preset", frameon=False)
    fig.tight_layout()
    fig.savefig(RESULTS / "decay_curve.png", dpi=150)
    plt.close(fig)
    print(f"[plots] wrote {RESULTS / 'decay_curve.png'}")


def accuracy_vs_profit() -> None:
    f = RESULTS / "accuracy_vs_profit.csv"
    if not f.exists():
        print(f"[plots] skip accuracy_vs_profit: {f} missing")
        return
    df = pd.read_csv(f)
    if not len(df):
        print("[plots] skip accuracy_vs_profit: no rows")
        return
    fig, ax = plt.subplots(figsize=(8, 5))
    for elig, g in df.groupby(df.get("eligible_fdr", pd.Series([False] * len(df)))):
        ax.scatter(g["brier"], g["mirror_ret"],
                   s=12 + 3 * g.get("n_trades", 1).clip(upper=40),
                   alpha=0.6,
                   label=("FDR-eligible" if elig else "not eligible"),
                   color=("#e76f51" if elig else "#90a4ae"),
                   edgecolors="none")
    ax.axhline(0.0, color="#888", linestyle="--", linewidth=1)
    ax.set_xlabel("training-window Brier score (lower = better calibrated)")
    ax.set_ylabel("test-window mirrored net return (N=24h)")
    ax.set_title("Accuracy vs. profitability, per wallet (§7.4)\n"
                 "well-calibrated need not mean profitable-to-copy once priced")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(RESULTS / "accuracy_vs_profit.png", dpi=150)
    plt.close(fig)
    print(f"[plots] wrote {RESULTS / 'accuracy_vs_profit.png'}")


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    decay_curve()
    accuracy_vs_profit()


if __name__ == "__main__":
    main()
