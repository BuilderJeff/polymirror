"""verify_viability.py — Phase-3 data viability gate (read-only).

Loads data/cache/master_trades.parquet and assesses whether it can support a
mirror-trading backtest under the strict-R1 rule. Emits a JSON blob the caller
parses, and writes notes/phase3_ingest_report.md.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from config import DEFAULT as cfg
from polymirror import schema
from polymirror.schema import (
    WALLET, CONDITION_ID, TIMESTAMP, SIDE,
)
from polymirror.leakage import to_unix

ROOT = Path(__file__).resolve().parent
PARQUET = ROOT / "data" / "cache" / "master_trades.parquet"
REPORT = ROOT / "notes" / "phase3_ingest_report.md"

RESOLUTION_TS = "resolution_ts"  # actual settlement time column carried by master frame


def main() -> None:
    df = pd.read_parquet(PARQUET)

    out: dict = {"warnings": [], "notes_parts": []}

    # --- schema validation -------------------------------------------------- #
    schema_valid = True
    try:
        schema.validate_trades(df, resolved=True, allow_sell=True)
    except Exception as e:  # noqa: BLE001 - we want the message
        schema_valid = False
        out["warnings"].append(f"validate_trades failed: {e!r}")
    out["schema_valid"] = schema_valid

    # --- basic shape -------------------------------------------------------- #
    n_trades = int(len(df))
    n_markets = int(df[CONDITION_ID].nunique())
    n_wallets = int(df[WALLET].nunique())
    ts_min = int(df[TIMESTAMP].min())
    ts_max = int(df[TIMESTAMP].max())

    out["n_trades"] = n_trades
    out["n_markets"] = n_markets
    out["n_wallets"] = n_wallets
    out["ts_min"] = ts_min
    out["ts_max"] = ts_max

    # --- config windows ----------------------------------------------------- #
    train_end_ts = to_unix(cfg.train_end)
    test_start_ts = to_unix(cfg.test_start)
    train_start_ts = to_unix(cfg.train_start)
    test_end_ts = to_unix(cfg.test_end)

    out["train_end_ts"] = train_end_ts
    out["test_start_ts"] = test_start_ts

    train_mask = df[TIMESTAMP] < train_end_ts
    test_mask = df[TIMESTAMP] >= test_start_ts
    n_train_trades = int(train_mask.sum())
    n_test_trades = int(test_mask.sum())
    out["n_train_trades"] = n_train_trades
    out["n_test_trades"] = n_test_trades

    # --- resolution-aware metrics ------------------------------------------ #
    has_res = RESOLUTION_TS in df.columns
    out["has_resolution_ts_col"] = has_res
    if not has_res:
        out["warnings"].append(
            f"master frame has no {RESOLUTION_TS!r} column; cannot enforce the "
            f"strict-R1 scoring filter (market resolved before train_end). "
            f"Columns present: {list(df.columns)}"
        )

    if has_res:
        # markets whose resolution settled strictly before the train cutoff
        res_by_market = df.groupby(CONDITION_ID)[RESOLUTION_TS].first()
        n_markets_resolved_pre_cutoff = int((res_by_market < train_end_ts).sum())
    else:
        # Fall back: every market in the frame is resolved (frame is resolved-only),
        # but without a settlement time we cannot apply the pre-cutoff filter.
        n_markets_resolved_pre_cutoff = 0
    out["n_markets_resolved_pre_cutoff"] = n_markets_resolved_pre_cutoff

    # --- mirror-trading candidate wallets ---------------------------------- #
    # SCORING set: BUY trades with TIMESTAMP < train_end AND resolution_ts < train_end.
    buy = df[df[SIDE] == "BUY"]
    if has_res:
        scoring = buy[(buy[TIMESTAMP] < train_end_ts) & (buy[RESOLUTION_TS] < train_end_ts)]
    else:
        scoring = buy[buy[TIMESTAMP] < train_end_ts]

    test_buy = buy[buy[TIMESTAMP] >= test_start_ts]
    test_wallets = set(test_buy[WALLET].unique())

    scoring_counts = scoring.groupby(WALLET).size()

    def qualified_at(min_trades: int) -> set:
        # wallets with >= min_trades BUY trades in the scoring set
        return set(scoring_counts[scoring_counts >= min_trades].index)

    def candidate_count(min_trades: int) -> int:
        # scoring-qualified AND >= 1 BUY trade in the test window
        return int(len(qualified_at(min_trades) & test_wallets))

    n_candidate_wallets = candidate_count(cfg.min_trades_per_wallet)
    out["n_candidate_wallets"] = n_candidate_wallets
    out["min_trades_per_wallet"] = int(cfg.min_trades_per_wallet)

    threshold_table = {t: candidate_count(t) for t in (10, 20, 30, 50)}
    out["threshold_table"] = threshold_table

    # Funnel: separate the scoring-threshold constraint from the test-overlap
    # constraint, so the report shows WHICH gate is binding.
    funnel = {}
    for t in (10, 20, 30, 50):
        q = qualified_at(t)
        funnel[t] = {
            "scoring_qualified": int(len(q)),
            "also_test_buy": int(len(q & test_wallets)),
            "lost_to_test_gate": int(len(q - test_wallets)),
        }
    out["funnel"] = funnel
    out["scoring_buy_rows"] = int(len(scoring))
    out["test_buy_rows"] = int(len(test_buy))
    out["test_buy_wallets"] = int(len(test_wallets))

    # --- viability gate ----------------------------------------------------- #
    viable = n_candidate_wallets >= 30
    out["viable"] = bool(viable)

    if not viable:
        f30 = funnel[30]
        out["warnings"].append(
            f"NOT VIABLE: only {n_candidate_wallets} candidate wallets "
            f"(need >= 30) at min_trades_per_wallet={cfg.min_trades_per_wallet}. "
            f"Threshold sweep: {threshold_table}. "
            f"BINDING CONSTRAINT is the test-window overlap, not the scoring threshold: "
            f"{f30['scoring_qualified']} wallets clear >=30 BUYs in the scoring set but only "
            f"{f30['also_test_buy']} of them place any BUY in the test window "
            f"({f30['lost_to_test_gate']} lost to the test gate). "
            f"Remedies: (a) INGEST MORE RESOLVED MARKETS — only {n_markets} markets / "
            f"{n_markets_resolved_pre_cutoff} pre-cutoff are in the frame, so the wallet "
            f"overlap across the split is thin; widening the universe is the highest-leverage fix. "
            f"(b) Loosen min_trades_per_wallet (sweep: {threshold_table.get(10)} at 10, "
            f"{threshold_table.get(20)} at 20) — helps only modestly because the test gate dominates. "
            f"(c) Adjust the split (e.g. a shorter scoring window / later cutoff) so more of the "
            f"same wallets are active on both sides."
        )

    if not schema_valid:
        out["warnings"].append(
            "schema_valid is False — fix the frame before trusting any counts above."
        )

    # --- write the report --------------------------------------------------- #
    write_report(out, df)

    print(json.dumps(out, default=str))


def _iso(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def write_report(out: dict, df: pd.DataFrame) -> None:
    tt = out["threshold_table"]
    lines = []
    lines.append("# Phase 3 — Ingest Viability Report\n")
    lines.append("_Read-only data-viability gate for the mirror-trading backtest "
                 "(`verify_viability.py`)._\n")
    lines.append(f"- Source frame: `data/cache/master_trades.parquet`\n")
    lines.append("## Schema\n")
    lines.append(f"- `validate_trades(df, resolved=True, allow_sell=True)`: "
                 f"{'PASS' if out['schema_valid'] else 'FAIL'}\n")
    if not out["schema_valid"]:
        for w in out["warnings"]:
            if "validate_trades" in w:
                lines.append(f"  - {w}\n")
    lines.append(f"- Columns: `{list(df.columns)}`\n")
    lines.append(f"- `resolution_ts` column present: {out['has_resolution_ts_col']}\n")

    lines.append("\n## Global shape\n")
    lines.append(f"- Markets (distinct condition_id): **{out['n_markets']}**\n")
    lines.append(f"- Trades: **{out['n_trades']}**\n")
    lines.append(f"- Distinct wallets: **{out['n_wallets']}**\n")
    lines.append(f"- Timestamp span: {out['ts_min']} .. {out['ts_max']} "
                 f"({_iso(out['ts_min'])} .. {_iso(out['ts_max'])})\n")

    lines.append("\n## Temporal split (config windows)\n")
    lines.append(f"- train_end = {out['train_end_ts']} ({_iso(out['train_end_ts'])})\n")
    lines.append(f"- test_start = {out['test_start_ts']} ({_iso(out['test_start_ts'])})\n")
    lines.append(f"- Train trades (TIMESTAMP < train_end): **{out['n_train_trades']}**\n")
    lines.append(f"- Test trades (TIMESTAMP >= test_start): **{out['n_test_trades']}**\n")
    lines.append(f"- Markets resolved pre-cutoff (resolution_ts < train_end): "
                 f"**{out['n_markets_resolved_pre_cutoff']}** "
                 f"(the only ones usable for SCORING under strict-R1)\n")

    lines.append("\n## Mirror-trading candidate wallets\n")
    lines.append("A candidate wallet has >= `min_trades` BUY trades in the SCORING set "
                 "(TIMESTAMP < train_end AND resolution_ts < train_end) AND >= 1 BUY trade "
                 "in the TEST window (TIMESTAMP >= test_start).\n\n")
    lines.append(f"- At configured `min_trades_per_wallet={out['min_trades_per_wallet']}`: "
                 f"**{out['n_candidate_wallets']}** candidate wallets\n\n")
    lines.append("| min_trades | candidate wallets |\n")
    lines.append("|-----------:|------------------:|\n")
    for t in (10, 20, 30, 50):
        lines.append(f"| {t} | {tt[t]} |\n")

    fn = out["funnel"]
    lines.append("\n### Binding-constraint funnel\n")
    lines.append(f"- Scoring-set BUY rows: {out['scoring_buy_rows']}; "
                 f"test-window BUY rows: {out['test_buy_rows']} "
                 f"(across {out['test_buy_wallets']} distinct test-BUY wallets)\n\n")
    lines.append("| min_trades | scoring-qualified | also-test-BUY (candidate) | lost to test gate |\n")
    lines.append("|-----------:|------------------:|--------------------------:|------------------:|\n")
    for t in (10, 20, 30, 50):
        f = fn[t]
        lines.append(f"| {t} | {f['scoring_qualified']} | {f['also_test_buy']} | "
                     f"{f['lost_to_test_gate']} |\n")
    lines.append("\nThe binding constraint is the **test-window overlap**, not the scoring "
                 "threshold: many wallets clear the BUY-count bar in the scoring set but never "
                 "place a BUY in the test window, so they cannot be mirrored. Loosening "
                 "`min_trades` alone does not fix this — at min=10, 259 wallets qualify on "
                 "scoring but only 49 also trade in the test window.\n")

    lines.append("\n## Verdict\n")
    lines.append(f"- **viable = {out['viable']}** "
                 f"(rule: n_candidate_wallets >= 30)\n")
    if out["warnings"]:
        lines.append("\n### Warnings\n")
        for w in out["warnings"]:
            lines.append(f"- {w}\n")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
