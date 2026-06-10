"""run.py — the backtest pipeline spine (Phases 4-5 wiring).

Reads the cached master trade frame (Phase 3), then for the full sensitivity grid:
    accuracy_metric x min_trades_per_wallet x spread_preset
runs:  strict-R1 train/test split -> score wallets -> luck filter (+FDR) ->
       out-of-sample mirror vs buy-the-favorite simulation over all N_hours.

Emits the research artifacts to results/:
    survivor_counts.csv         raw vs FDR survivors per (metric, min_trades)
    returns_by_preset_N.csv     strat/bench/edge + CIs + p_edge per (preset, N) at the headline cell
    edge_by_min_trades.csv      headline-preset edge vs min_trades (the R3 sweep)
    accuracy_vs_profit.csv      per-wallet training Brier vs test mirrored return (§7.4 scatter data)
    grid.json                   the full machine-readable grid
    summary.json / summary.md   headline numbers + a plain-language §7.6 interpretation

Plots (decay_curve.png, accuracy_vs_profit.png) are rendered by plots.py from these CSVs.

Everything is config-driven and seeded (R8). NOTHING here selects on test-window data (R1):
the scoring frame is restricted to trades whose TIMESTAMP < train_end AND whose MARKET also
resolved before train_end (so selection never uses an outcome unknowable at the cutoff).
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd

import config
from config import Config, SPREAD_PRESETS
from polymirror import schema
from polymirror.leakage import to_unix
from polymirror.scorer import select_eligible
from polymirror.simulator import simulate
from polymirror import pricing

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "data" / "cache"
RESULTS = ROOT / "results"
MASTER = CACHE / "master_trades.parquet"

# Sensitivity grid (spec §7.7: min_trades swept; R6: every result under all 3 presets).
MIN_TRADES_GRID = (10, 20, 30, 50)
METRICS = ("brier",)                 # 'log' can be added; brier is the headline metric
PRESETS = ("optimistic", "base", "conservative")
HEADLINE_METRIC = "brier"
HEADLINE_MIN_TRADES = 30
HEADLINE_PRESET = "base"
SCATTER_N = 24                       # holding horizon used for the accuracy-vs-profit scatter


# --------------------------------------------------------------------------- #
def load_master() -> pd.DataFrame:
    if not MASTER.exists():
        raise FileNotFoundError(
            f"{MASTER} not found — run Phase-3 ingestion first "
            f"(./.venv/Scripts/python.exe -m polymirror.ingest)."
        )
    df = pd.read_parquet(MASTER)
    schema.validate_trades(df, resolved=True, allow_sell=True)
    if "resolution_ts" not in df.columns:
        raise ValueError("master frame missing 'resolution_ts' column (needed for strict-R1 scoring).")
    return df


def scoring_frame(master: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Trades usable for SELECTION: before the cutoff AND in markets resolved before the cutoff (R1)."""
    cut = to_unix(cfg.train_end)
    m = master
    sel = m[(m[schema.TIMESTAMP] < cut) & (m["resolution_ts"] < cut)].copy()
    return sel


def test_frame(master: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Trades in the strictly-later test window (the simulator filters to BUY + eligible itself)."""
    ts = to_unix(cfg.test_start)
    return master[master[schema.TIMESTAMP] >= ts].copy()


# --------------------------------------------------------------------------- #
def run_grid(master: pd.DataFrame, base_cfg: Config = config.DEFAULT) -> dict:
    """Run the full metric x min_trades x preset grid; return a structured result dict."""
    score_df = scoring_frame(master, base_cfg)
    test_df = test_frame(master, base_cfg)
    price_lookup = pricing.make_price_lookup(master)
    resmap = pricing.resolution_ts_map(master)

    survivors = []        # rows: metric, min_trades, n_tested, n_raw, n_fdr
    returns_rows = []     # rows: metric, min_trades, preset, N, strat/bench/edge + CIs + p_edge
    scatter_records = []  # per-wallet (headline cell): brier vs mirrored return
    grid = {}

    for metric in METRICS:
        for mt in MIN_TRADES_GRID:
            cfg_sel = base_cfg.with_(accuracy_metric=metric, min_trades_per_wallet=mt)
            eligible, table = select_eligible(score_df, cfg_sel)
            n_tested = int(table.attrs.get("n_tested", len(table)))
            n_raw = int(table.attrs.get("n_eligible_raw", int(table["eligible_raw"].sum()) if len(table) else 0))
            n_fdr = int(table.attrs.get("n_eligible_fdr", int(table["eligible_fdr"].sum()) if len(table) else 0))
            survivors.append(dict(metric=metric, min_trades=mt, n_tested=n_tested,
                                  n_raw=n_raw, n_fdr=n_fdr, n_eligible=len(eligible)))

            for preset in PRESETS:
                cfg_sim = cfg_sel.with_(spread_preset=preset)
                res = simulate(eligible, test_df, price_lookup, cfg_sim,
                               resolution_ts=resmap, preset=SPREAD_PRESETS[preset])
                for row in res.per_n:
                    returns_rows.append(dict(metric=metric, min_trades=mt, preset=preset, **row))
                grid[f"{metric}|{mt}|{preset}"] = res.per_n

                # accuracy-vs-profit scatter from the headline cell only (§7.4)
                if (metric == HEADLINE_METRIC and mt == HEADLINE_MIN_TRADES
                        and preset == HEADLINE_PRESET and len(res.trades)):
                    scatter_records.extend(_scatter_from(res, table))

    return dict(
        survivors=pd.DataFrame(survivors),
        returns=pd.DataFrame(returns_rows),
        scatter=pd.DataFrame(scatter_records),
        grid=grid,
        meta=dict(
            n_master_trades=int(len(master)),
            n_scoring_trades=int(len(score_df)),
            n_test_trades=int(len(test_df)),
            n_markets=int(master[schema.CONDITION_ID].nunique()),
            headline=dict(metric=HEADLINE_METRIC, min_trades=HEADLINE_MIN_TRADES, preset=HEADLINE_PRESET),
            windows=dict(train_start=base_cfg.train_start, train_end=base_cfg.train_end,
                         test_start=base_cfg.test_start, test_end=base_cfg.test_end),
            n_bootstrap=base_cfg.n_bootstrap, alpha=base_cfg.alpha, seed=base_cfg.seed,
        ),
    )


def _scatter_from(res, table: pd.DataFrame) -> list[dict]:
    """Join per-wallet training Brier (selection) to per-wallet test mirrored return (at SCATTER_N)."""
    tr = res.trades
    sub = tr[tr["N"] == SCATTER_N]
    if not len(sub):
        return []
    per_wallet = sub.groupby("wallet").agg(mirror_ret=("strat_ret", "mean"),
                                           bench_ret=("bench_ret", "mean"),
                                           n_legs=("strat_ret", "size")).reset_index()
    sc = table[["wallet", "brier", "n_trades", "eligible_fdr"]].merge(per_wallet, on="wallet", how="inner")
    return sc.to_dict("records")


# --------------------------------------------------------------------------- #
def write_artifacts(out: dict) -> None:
    RESULTS.mkdir(exist_ok=True)
    out["survivors"].to_csv(RESULTS / "survivor_counts.csv", index=False)

    ret = out["returns"]
    headline = ret[(ret["metric"] == HEADLINE_METRIC) & (ret["min_trades"] == HEADLINE_MIN_TRADES)]
    headline.to_csv(RESULTS / "returns_by_preset_N.csv", index=False)

    # R3 sweep: headline-preset edge vs min_trades
    sweep = ret[(ret["metric"] == HEADLINE_METRIC) & (ret["preset"] == HEADLINE_PRESET)]
    sweep[["min_trades", "N", "mean_edge", "edge_lo", "edge_hi", "p_edge",
           "n_trades", "n_wallets"]].to_csv(RESULTS / "edge_by_min_trades.csv", index=False)

    out["scatter"].to_csv(RESULTS / "accuracy_vs_profit.csv", index=False)
    (RESULTS / "grid.json").write_text(json.dumps(out["grid"], indent=2, default=float))

    summary = _summary(out)
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (RESULTS / "summary.md").write_text(_summary_md(summary, out))


def _summary(out: dict) -> dict:
    ret = out["returns"]
    h = ret[(ret["metric"] == HEADLINE_METRIC) & (ret["min_trades"] == HEADLINE_MIN_TRADES)
            & (ret["preset"] == HEADLINE_PRESET)].sort_values("N")
    surv = out["survivors"]
    hs = surv[(surv["metric"] == HEADLINE_METRIC) & (surv["min_trades"] == HEADLINE_MIN_TRADES)]
    return dict(
        meta=out["meta"],
        headline_survivors=hs.to_dict("records"),
        headline_edge_by_N=h[["N", "mean_strat", "mean_bench", "mean_edge",
                              "edge_lo", "edge_hi", "p_edge", "n_trades", "n_wallets"]].to_dict("records"),
    )


def _summary_md(summary: dict, out: dict) -> str:
    m = summary["meta"]
    lines = ["# polymirror — headline results\n",
             f"Universe: {m['n_markets']} resolved 2025 markets, {m['n_master_trades']:,} trades. "
             f"Scoring trades (strict-R1): {m['n_scoring_trades']:,}; test trades: {m['n_test_trades']:,}.\n",
             f"Headline cell: metric={m['headline']['metric']}, min_trades={m['headline']['min_trades']}, "
             f"spread={m['headline']['preset']}; bootstrap n={m['n_bootstrap']}, seed={m['seed']}.\n",
             "\n## Edge = strategy − benchmark, by holding horizon N (after costs)\n",
             "| N (h) | mean strat | mean bench | **edge** | 95% CI | p(edge≠0) | trades | wallets |",
             "|---|---|---|---|---|---|---|---|"]
    for r in summary["headline_edge_by_N"]:
        lines.append(f"| {r['N']:g} | {r['mean_strat']:+.4f} | {r['mean_bench']:+.4f} | "
                     f"**{r['mean_edge']:+.4f}** | [{r['edge_lo']:+.4f}, {r['edge_hi']:+.4f}] | "
                     f"{r['p_edge']:.3f} | {r['n_trades']} | {r['n_wallets']} |")
    lines += ["\n## Skill-filter survivors (headline metric)\n",
              "| min_trades | tested | raw survivors | FDR survivors |",
              "|---|---|---|---|"]
    for r in summary["headline_survivors"]:
        lines.append(f"| {r['min_trades']} | {r['n_tested']} | {r['n_raw']} | {r['n_fdr']} |")
    lines += ["\n## What this does and does NOT license (§7.6)\n",
              "- An edge > 0 whose CI excludes 0 under the **base and conservative** presets is "
              "evidence *against* full short-horizon efficiency — not a proof, and not a live strategy (R0).",
              "- An edge ≈ 0 (CI contains 0) is *consistent with* efficiency but does **not** prove it; "
              "this specific copy rule may simply be weak.",
              "- A result that survives only the **optimistic** spread is reported as **not** a result (R6).",
              "- Few skill-filter survivors is expected (most apparent winners are lucky — Akey et al.; "
              "Gómez-Cram et al.) and is itself a finding, not a bug.",
              "- Unit of analysis is the **wallet**, not the person (R7)."]
    return "\n".join(lines) + "\n"


def main() -> None:
    master = load_master()
    out = run_grid(master)
    write_artifacts(out)
    print(f"[run] master={out['meta']['n_master_trades']:,} trades, "
          f"{out['meta']['n_markets']} markets; "
          f"scoring={out['meta']['n_scoring_trades']:,}, test={out['meta']['n_test_trades']:,}.")
    print(f"[run] wrote artifacts to {RESULTS}")
    print((RESULTS / "summary.md").read_text())


if __name__ == "__main__":
    main()
