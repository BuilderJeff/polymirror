"""ingest.py — Phase 3: build the canonical master trade frame for a UNIVERSE.

R0: read-only historical backtest. No keys, no live trades, no funded wallet.

WHAT THIS DOES
--------------
Ingests a universe of RESOLVED BINARY Polymarket markets into ONE cached, canonical
trade frame that the scorer (Phase 4) and simulator (Phase 4/5) consume. It is the
generalisation of phase1_slice.py from a single chosen market to the full universe.

PIPELINE (ingest_universe)
  1. List the universe via polyapi.list_resolved_binary_markets (highest-volume
     resolved binaries first, settlement time inside the [train_start, test_end)
     window, volume floor + order-book filter applied per config).
  2. For each market, pull its FULL observable TRADE history (get_trades, <=4000
     records). Markets with <50 trades are SKIPPED and logged. Each raw /trades dict
     is mapped onto the canonical schema columns and the resolution is attached from
     the universe record (winning_index + closed_time_unix derived at list time from
     the settled outcomePrices — never trusted blindly: we ASSERT the join covers
     100% of the market's rows, winning_index in {0,1}, no nulls). A market that
     fails any per-market check is DROPPED and logged.
  3. concat all kept markets -> master DataFrame; schema.validate_trades(resolved=
     True, allow_sell=True) must pass; write data/cache/master_trades.parquet and
     data/cache/universe_meta.parquet (one row per kept market).
  4. PRINT a summary: markets kept/dropped, n_trades, distinct wallets, global date
     span, and counts of markets resolving pre- vs post- cfg.train_end.

Run:  ./.venv/Scripts/python.exe -m polymirror.ingest
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from config import Config, DEFAULT
from polymirror import polyapi
from polymirror import schema as S
from polymirror.leakage import to_unix

# --- Constants --------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "cache"
MASTER_PARQUET = CACHE_DIR / "master_trades.parquet"
META_PARQUET = CACHE_DIR / "universe_meta.parquet"

# A market with fewer than this many TRADE prints is too thin to score; skip it.
MIN_TRADES_PER_MARKET = 50

# Extra (non-schema) column carrying the actual settlement instant for the leakage
# filter the scorer applies in run.py (see config.py train_end docstring).
RESOLUTION_TS = "resolution_ts"


# --- Small pure coercion helpers (mirror phase1_slice.py) -------------------- #
def _to_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class IngestResult:
    """Outcome of an ingest run (returned for the structured report)."""
    n_markets: int
    n_dropped: int
    n_trades: int
    n_wallets: int
    parquet_path: str
    meta_path: str
    per_market_join_ok: bool
    date_span: str
    ts_min: Optional[int]
    ts_max: Optional[int]
    n_resolved_pre_train_end: int
    n_resolved_post_train_end: int
    dropped_reasons: list[dict] = field(default_factory=list)
    ran_clean: bool = False
    notes: str = ""


def _normalize_market_trades(trades: list[dict], market: dict) -> pd.DataFrame:
    """Map one market's raw /trades dicts onto the canonical schema + resolution.

    `market` is a universe record from list_resolved_binary_markets, carrying
    conditionId, slug, question, winning_index and closed_time_unix. The winning
    index and settlement instant were DERIVED from the settled outcomePrices at list
    time; we attach them to every row and (caller) assert 100% coverage.

    Immutable: builds and returns a fresh DataFrame; never mutates `market`/`trades`.
    """
    winning_index = market.get("winning_index")
    closed_time_unix = market.get("closed_time_unix")
    slug = market.get("slug")
    question = market.get("question")

    rows: list[dict] = []
    for t in trades:
        size = _to_float(t.get("size"))
        price = _to_float(t.get("price"))
        # Data API trades expose `size` and `price` but not usdcSize; derive notional.
        usdc = size * price if (size is not None and price is not None) else None
        outcome_index = _to_int(t.get("outcomeIndex"))
        # won = 1 iff the trade's bought outcomeIndex == winning_index (BUY semantics).
        won = (
            S.compute_won(outcome_index, winning_index)
            if (outcome_index is not None and winning_index is not None)
            else None
        )
        rows.append({
            S.WALLET: t.get("proxyWallet"),
            S.CONDITION_ID: t.get("conditionId"),
            S.TIMESTAMP: _to_int(t.get("timestamp")),
            S.PRICE: price,
            S.SIDE: t.get("side"),
            S.OUTCOME_INDEX: outcome_index,
            S.SIZE: size,
            S.USDC_SIZE: usdc,
            # Prefer the per-trade title/slug; fall back to the universe record so the
            # column is never null when the Data API omits it on a row.
            S.TITLE: t.get("title") if t.get("title") is not None else question,
            S.SLUG: t.get("slug") if t.get("slug") is not None else slug,
            S.WINNING_INDEX: winning_index,
            S.WON: won,
            S.ENTRY_PROB: price,  # implied prob of the bought token = price (BUY)
            RESOLUTION_TS: closed_time_unix,
        })
    return pd.DataFrame(rows)


def _assert_market_join(df: pd.DataFrame, market: dict) -> None:
    """Assert the resolution join covered 100% of one market's rows, else raise.

    A wrong/partial join silently inverts realized returns — the single most
    dangerous bug in this project (schema.py docstring). We require: winning_index in
    {0,1}; no nulls in winning_index / won / the trade-critical columns; and won
    consistent with (outcome_index == winning_index) on every row.
    """
    n = len(df)
    if n == 0:
        raise ValueError("market has no rows after normalisation")

    wi = market.get("winning_index")
    if wi not in (0, 1):
        raise ValueError(f"winning_index {wi!r} not in {{0,1}}")

    crit = [
        S.WALLET, S.CONDITION_ID, S.TIMESTAMP, S.PRICE, S.SIDE,
        S.OUTCOME_INDEX, S.WINNING_INDEX, S.WON,
    ]
    nulls = {c: int(df[c].isna().sum()) for c in crit if df[c].isna().any()}
    if nulls:
        raise ValueError(f"nulls in critical columns (silent-corruption risk): {nulls}")

    # 100% coverage: every row carries the (constant) winning_index for this market.
    if not (df[S.WINNING_INDEX] == wi).all():
        raise ValueError("winning_index not constant/covered across all rows")

    # won must equal (outcome_index == winning_index) on every row.
    expected_won = (df[S.OUTCOME_INDEX] == wi).astype(int)
    if not (df[S.WON].astype(int) == expected_won).all():
        raise ValueError("won inconsistent with (outcome_index == winning_index)")


def _market_meta_row(market: dict, df: pd.DataFrame) -> dict:
    """One universe_meta row per kept market (conditionId, slug, question, ...)."""
    return {
        "conditionId": market.get("conditionId"),
        "slug": market.get("slug"),
        "question": market.get("question"),
        "volumeNum": _to_float(market.get("volumeNum")),
        "closed_time_unix": _to_int(market.get("closed_time_unix")),
        "winning_index": _to_int(market.get("winning_index")),
        "n_trades": int(len(df)),
        "n_wallets": int(df[S.WALLET].nunique()),
        "ts_min": int(df[S.TIMESTAMP].min()),
        "ts_max": int(df[S.TIMESTAMP].max()),
    }


def _iso(ts: Optional[int]) -> str:
    if ts is None:
        return "n/a"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def ingest_universe(
    cfg: Config = DEFAULT,
    max_markets: int | None = None,
    page_budget: int | None = None,
) -> IngestResult:
    """Ingest a universe of resolved binary markets into the canonical master frame.

    max_markets / page_budget default to the config values (the single source of
    truth, R8); pass explicit ints only to override. Returns an IngestResult; writes
    master_trades.parquet and universe_meta.parquet under data/cache/.
    """
    cfg.validate()
    if max_markets is None:
        max_markets = cfg.universe_max_markets
    if page_budget is None:
        page_budget = cfg.universe_page_budget
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("  polymirror Phase-3 universe ingest")
    print(f"  window: {cfg.train_start} .. {cfg.test_end}")
    print(f"  min_volume_usd={cfg.universe_min_volume_usd} "
          f"require_order_book={cfg.universe_require_order_book}")
    print(f"  max_markets={max_markets} page_budget={page_budget} "
          f"min_trades_per_market={MIN_TRADES_PER_MARKET}")
    print("=" * 72)

    start_unix = to_unix(cfg.train_start)
    end_unix = to_unix(cfg.test_end)
    train_end_unix = to_unix(cfg.train_end)

    # --- 1. list the universe ----------------------------------------------- #
    universe = polyapi.list_resolved_binary_markets(
        min_volume_usd=cfg.universe_min_volume_usd,
        start_unix=start_unix,
        end_unix=end_unix,
        require_order_book=cfg.universe_require_order_book,
        max_markets=max_markets,
        page_budget=page_budget,
    )
    print(f"\n[1] universe: {len(universe)} resolved binary markets listed")

    # --- 2. per-market trades + resolution join ----------------------------- #
    frames: list[pd.DataFrame] = []
    meta_rows: list[dict] = []
    dropped_reasons: list[dict] = []

    for i, market in enumerate(universe, start=1):
        cond = market.get("conditionId")
        slug = market.get("slug")
        try:
            trades = polyapi.get_trades(cond, type="TRADE")
        except polyapi.PolyAPIError as e:
            dropped_reasons.append({"conditionId": cond, "slug": slug,
                                    "reason": f"get_trades failed: {e}"})
            print(f"  [{i}/{len(universe)}] DROP {slug or cond}: get_trades failed: {e}")
            continue

        if len(trades) < MIN_TRADES_PER_MARKET:
            dropped_reasons.append({"conditionId": cond, "slug": slug,
                                    "reason": f"too few trades ({len(trades)} < "
                                              f"{MIN_TRADES_PER_MARKET})"})
            print(f"  [{i}/{len(universe)}] SKIP {slug or cond}: "
                  f"{len(trades)} trades < {MIN_TRADES_PER_MARKET}")
            continue

        df = _normalize_market_trades(trades, market)
        try:
            _assert_market_join(df, market)
        except (ValueError, AssertionError) as e:
            dropped_reasons.append({"conditionId": cond, "slug": slug,
                                    "reason": f"join check failed: {e}"})
            print(f"  [{i}/{len(universe)}] DROP {slug or cond}: join check failed: {e}")
            continue

        frames.append(df)
        meta_rows.append(_market_meta_row(market, df))
        print(f"  [{i}/{len(universe)}] OK   {slug or cond}: "
              f"{len(df)} trades, {df[S.WALLET].nunique()} wallets")

    if not frames:
        raise RuntimeError(
            "no markets survived ingestion — nothing to write. "
            f"({len(universe)} listed, {len(dropped_reasons)} dropped/skipped)"
        )

    # --- 3. concat -> validate -> write ------------------------------------- #
    master = pd.concat(frames, ignore_index=True)
    # Schema contract: explodes on any inconsistency (resolved, SELLs allowed).
    S.validate_trades(master, resolved=True, allow_sell=True)

    meta = pd.DataFrame(meta_rows)

    master.to_parquet(MASTER_PARQUET, index=False, engine="pyarrow")
    meta.to_parquet(META_PARQUET, index=False, engine="pyarrow")

    # --- 4. summary --------------------------------------------------------- #
    n_markets = len(frames)
    n_trades = int(len(master))
    n_wallets = int(master[S.WALLET].nunique())
    ts_min = int(master[S.TIMESTAMP].min())
    ts_max = int(master[S.TIMESTAMP].max())
    date_span = f"{_iso(ts_min)} .. {_iso(ts_max)}"

    # Pre/post split is on SETTLEMENT time (closed_time_unix), not trade time:
    # a market resolves pre-train_end iff its settlement instant < train_end.
    closed_times = meta["closed_time_unix"].dropna().astype(int)
    n_pre = int((closed_times < train_end_unix).sum())
    n_post = int((closed_times >= train_end_unix).sum())

    result = IngestResult(
        n_markets=n_markets,
        n_dropped=len(dropped_reasons),
        n_trades=n_trades,
        n_wallets=n_wallets,
        parquet_path=str(MASTER_PARQUET),
        meta_path=str(META_PARQUET),
        per_market_join_ok=True,  # every kept market passed _assert_market_join
        date_span=date_span,
        ts_min=ts_min,
        ts_max=ts_max,
        n_resolved_pre_train_end=n_pre,
        n_resolved_post_train_end=n_post,
        dropped_reasons=dropped_reasons,
        ran_clean=True,
        notes=(
            f"kept {n_markets}/{len(universe)} listed markets; "
            f"dropped/skipped {len(dropped_reasons)}; "
            f"pre/post train_end ({cfg.train_end}) = {n_pre}/{n_post}"
        ),
    )

    print("\n" + "=" * 72)
    print("  INGEST SUMMARY")
    print("=" * 72)
    print(f"  markets listed ............. {len(universe)}")
    print(f"  markets kept ............... {n_markets}")
    print(f"  markets dropped/skipped .... {len(dropped_reasons)}")
    print(f"  total TRADE records ........ {n_trades}")
    print(f"  distinct wallets ........... {n_wallets}")
    print(f"  global date span ........... {date_span}")
    print(f"  resolved PRE  train_end .... {n_pre}  (closedTime < {cfg.train_end})")
    print(f"  resolved POST train_end .... {n_post}  (closedTime >= {cfg.train_end})")
    print(f"  per-market join 100% ....... {result.per_market_join_ok}")
    print(f"  master parquet ............. {MASTER_PARQUET}")
    print(f"  meta parquet ............... {META_PARQUET}")

    # Emit a machine-readable summary alongside the parquets.
    summary_json = {
        "n_markets": n_markets,
        "n_dropped": len(dropped_reasons),
        "n_trades": n_trades,
        "n_wallets": n_wallets,
        "date_span": date_span,
        "ts_min": ts_min,
        "ts_max": ts_max,
        "n_resolved_pre_train_end": n_pre,
        "n_resolved_post_train_end": n_post,
        "per_market_join_ok": result.per_market_join_ok,
        "parquet_path": str(MASTER_PARQUET),
        "meta_path": str(META_PARQUET),
        "dropped_reasons": dropped_reasons,
    }
    (CACHE_DIR / "universe_meta_summary.json").write_text(
        json.dumps(summary_json, indent=2), encoding="utf-8"
    )
    print("\n[DONE] universe ingest ran clean.")
    return result


def main() -> int:
    ingest_universe()
    return 0


if __name__ == "__main__":
    sys.exit(main())
