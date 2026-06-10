"""phase1_slice.py — Phase-1 vertical slice: one resolved market, end to end.

R0: read-only historical backtest. No keys, no live trades, no funded wallet.

Pipeline for the chosen resolved market (Toronto Raptors 2025 NBA Finals, conditionId
below, winning_index=1 → "No"):

  1. Pull the FULL observable wallet-level TRADE history (paginate to completion).
  2. Pull the Gamma resolution and DERIVE the winning outcomeIndex from the settled
     outcomePrices (never hard-code it — the market-finder's 1 is only a cross-check).
  3. Join each trade to its realized outcome:
       won = 1 if the trade's bought outcomeIndex == winning_index else 0.
     For this slice we attach the realized winner to EVERY row regardless of side;
     downstream phases restrict to BUY entries (see schema.py outcome semantics).
  4. Write data/cache/phase1_trades.parquet.
  5. VERIFY + ASSERT (the resolution join MUST cover 100% of rows — a wrong/partial
     join silently inverts returns, the single most dangerous bug here).

Run:  ./.venv/Scripts/python.exe phase1_slice.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

# Local imports (run from project root; config.py + polymirror/ are importable there).
from polymirror import polyapi
from polymirror import schema as S

# --- The chosen market (notes/chosen_market.md) ------------------------------ #
CONDITION_ID = "0x37963e2b3194455fe768cd470571640c758c049b7cda6967449a43a8bd5c835f"
QUESTION = "Will the Toronto Raptors win the 2025 NBA Finals?"
EXPECTED_WINNING_INDEX = 1  # from market-finder; we DERIVE and cross-check, not trust.

ROOT = Path(__file__).resolve().parent
OUT_PARQUET = ROOT / "data" / "cache" / "phase1_trades.parquet"


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_raw_frame(trades: list[dict]) -> pd.DataFrame:
    """Map raw Data-API trade dicts onto the canonical schema columns (schema.py)."""
    rows = []
    for t in trades:
        size = _to_float(t.get("size"))
        price = _to_float(t.get("price"))
        # Data API trades expose `size` and `price` but not usdcSize; derive notional.
        usdc = size * price if (size is not None and price is not None) else None
        rows.append({
            S.WALLET: t.get("proxyWallet"),
            S.CONDITION_ID: t.get("conditionId"),
            S.TIMESTAMP: _to_int(t.get("timestamp")),
            S.PRICE: price,
            S.SIDE: t.get("side"),
            S.OUTCOME_INDEX: _to_int(t.get("outcomeIndex")),
            S.SIZE: size,
            S.USDC_SIZE: usdc,
            S.TITLE: t.get("title"),
            S.SLUG: t.get("slug"),
            "outcome_label": t.get("outcome"),  # human label e.g. "Yes"/"No"
            "transaction_hash": t.get("transactionHash"),
        })
    return pd.DataFrame(rows)


def join_resolution(df: pd.DataFrame, res: "polyapi.ResolutionInfo") -> pd.DataFrame:
    """Attach the realized winner to EVERY row (immutable: returns a new frame).

    won = 1 iff the trade's bought outcomeIndex == winning_index. entry_prob is the
    implied prob of the bought token = price for a BUY (see schema.to_long_entry); for
    this slice we record price as entry_prob uniformly (downstream uses BUY rows).
    """
    if res.winning_index is None:
        raise RuntimeError("market is not resolved — cannot attach a realized winner.")
    out = df.copy()
    out[S.WINNING_INDEX] = int(res.winning_index)
    out[S.WON] = (out[S.OUTCOME_INDEX] == int(res.winning_index)).astype("Int64")
    out[S.ENTRY_PROB] = out[S.PRICE]
    return out


def main() -> int:
    print("=" * 70)
    print("  polymirror Phase-1 vertical slice")
    print(f"  market: {QUESTION}")
    print(f"  conditionId: {CONDITION_ID}")
    print("=" * 70)

    # --- 1. full TRADE history (paginate to completion) ---------------------
    trades = polyapi.get_trades(CONDITION_ID, type="TRADE")
    total_trades = len(trades)
    print(f"\n[1] pulled {total_trades} TRADE records (paginated to completion)")

    # --- 2. Gamma resolution → derive winning outcomeIndex ------------------
    market = polyapi.get_market_gamma(CONDITION_ID)
    res = polyapi.resolution_from_gamma(market)
    print(f"[2] resolution: resolved={res.resolved} outcomes={res.outcomes} "
          f"prices={res.outcome_prices} winning_index={res.winning_index}")
    assert res.resolved, "market did not parse as resolved (closed + settled 0/1 prices)"
    assert res.winning_index == EXPECTED_WINNING_INDEX, (
        f"derived winning_index {res.winning_index} != market-finder "
        f"{EXPECTED_WINNING_INDEX} — investigate before trusting the join."
    )

    # --- 3. build + join ----------------------------------------------------
    raw = build_raw_frame(trades)
    joined = join_resolution(raw, res)

    # --- VERIFY -------------------------------------------------------------
    distinct_wallets = int(joined[S.WALLET].nunique())

    crit = [S.PRICE, S.TIMESTAMP, S.SIDE, S.OUTCOME_INDEX]
    null_counts = {c: int(joined[c].isna().sum()) for c in crit}
    all_rows_have_price_ts_side_idx = all(v == 0 for v in null_counts.values())

    # Resolution-join coverage MUST be 100% (a wrong join silently inverts returns).
    join_non_null = int(joined[S.WINNING_INDEX].notna().sum() & joined[S.WON].notna().sum())
    covered = int((joined[S.WINNING_INDEX].notna() & joined[S.WON].notna()).sum())
    join_coverage_pct = (100.0 * covered / total_trades) if total_trades else 0.0

    print(f"\n[VERIFY] total TRADE records ........ {total_trades}")
    print(f"[VERIFY] distinct proxyWallets ...... {distinct_wallets}")
    print(f"[VERIFY] null counts (critical) ..... {null_counts}")
    print(f"[VERIFY] all rows price/ts/side/idx . {all_rows_have_price_ts_side_idx}")
    print(f"[VERIFY] resolution join coverage ... {join_coverage_pct:.4f}% "
          f"({covered}/{total_trades})")
    print(f"[VERIFY] winning outcomeIndex ....... {res.winning_index} "
          f"(label='{res.outcomes[res.winning_index]}')")

    # Hard asserts: critical columns complete + 100% resolution coverage.
    assert all_rows_have_price_ts_side_idx, (
        f"nulls in critical columns (silent-corruption risk): {null_counts}"
    )
    assert covered == total_trades, (
        f"resolution join covered {covered}/{total_trades} rows — MUST be 100%. "
        "A partial/wrong join silently inverts realized returns (R1/schema contract)."
    )

    # Schema-level validation (resolved contract): explodes on any inconsistency.
    S.validate_trades(joined, resolved=True, allow_sell=True)

    # --- 5 sample joined rows ----------------------------------------------
    sample_cols = [S.WALLET, S.PRICE, S.TIMESTAMP, S.SIDE, S.OUTCOME_INDEX,
                   "outcome_label", S.WON]
    sample_df = joined[sample_cols].head(5)
    print("\n[SAMPLE] 5 joined rows (wallet, price, timestamp, side, outcomeIndex, label, won):")
    for _, r in sample_df.iterrows():
        print(f"  {r[S.WALLET]}  p={r[S.PRICE]:.4f}  ts={r[S.TIMESTAMP]}  "
              f"{r[S.SIDE]:<4}  idx={r[S.OUTCOME_INDEX]}  "
              f"{str(r['outcome_label']):<3}  won={int(r[S.WON])}")

    sample_rows = [
        {
            "wallet": str(r[S.WALLET]),
            "price": float(r[S.PRICE]),
            "timestamp": int(r[S.TIMESTAMP]),
            "side": str(r[S.SIDE]),
            "outcome_index": int(r[S.OUTCOME_INDEX]),
            "outcome_label": str(r["outcome_label"]),
            "won": int(r[S.WON]),
        }
        for _, r in sample_df.iterrows()
    ]

    # --- mean entry price: winning side vs losing side ----------------------
    win_idx = int(res.winning_index)
    win_mask = joined[S.OUTCOME_INDEX] == win_idx
    mean_entry_winning_side = float(joined.loc[win_mask, S.PRICE].mean())
    mean_entry_losing_side = float(joined.loc[~win_mask, S.PRICE].mean())
    print(f"\n[PRICE] mean entry price, WINNING side (idx={win_idx}) . "
          f"{mean_entry_winning_side:.4f}  (n={int(win_mask.sum())})")
    print(f"[PRICE] mean entry price, LOSING side ................. "
          f"{mean_entry_losing_side:.4f}  (n={int((~win_mask).sum())})")
    print("[PRICE] (reported, NOT asserted — informative if prices are calibrated)")

    # --- 4. write parquet ---------------------------------------------------
    OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    joined.to_parquet(OUT_PARQUET, index=False)
    print(f"\n[WRITE] {OUT_PARQUET}  ({len(joined)} rows)")

    # Emit a machine-readable summary for the report / structured return.
    summary = {
        "total_trades": total_trades,
        "distinct_wallets": distinct_wallets,
        "join_coverage_pct": round(join_coverage_pct, 6),
        "winning_index": win_idx,
        "winning_label": res.outcomes[win_idx],
        "all_rows_have_price_ts_side_idx": all_rows_have_price_ts_side_idx,
        "null_counts": null_counts,
        "mean_entry_winning_side": round(mean_entry_winning_side, 6),
        "mean_entry_losing_side": round(mean_entry_losing_side, 6),
        "sample_rows": sample_rows,
        "parquet": str(OUT_PARQUET),
    }
    (ROOT / "data" / "cache" / "phase1_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print("\n[DONE] slice ran clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
