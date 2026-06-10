"""pricing.py — the historical price series, reconstructed from TRADE PRINTS.

WHY THIS EXISTS (Phase 1 finding, see notes/api_schema.md)
----------------------------------------------------------
The CLOB book/spread/midpoint endpoints 404 on RESOLVED markets (the book is
deleted at settlement) and prices-history is unreliable for old/resolved markets.
There is therefore NO live order-book series to mark old positions against. The only
durable historical signal we already hold is the stream of executed TRADE PRINTS
that polyapi.get_trades() pulls and caches.

A print of token k at price p is a revealed market quote: at that instant the mid of
token k is ~= p, and — because a binary market's two tokens are arbitrage-free
complements — the mid of token 1-k is ~= 1-p. We therefore canonicalise EVERYTHING
to the implied mid of OUTCOME_INDEX 0:

    mid0 = price          if outcome_index == 0
    mid0 = 1 - price      if outcome_index == 1            (complement)
    mid(index 1) = 1 - mid0

and build a per-market step series sorted by timestamp. The price at an arbitrary ts
is the value of the MOST RECENT print AT OR BEFORE ts (a right-continuous step / last
observation carried forward), which is the only non-leaking estimate available: we
never peek at a print that has not happened yet (R1).

This module is pure pandas/numpy and fully deterministic — no I/O, no wall-clock, no
RNG. It produces exactly the `PriceLookup = (condition_id, outcome_index, ts) -> float|None`
callable and the `resolution_ts` dict that simulator.py consumes.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Optional, Union

import numpy as np
import pandas as pd

from polymirror.leakage import to_unix
from polymirror.schema import CONDITION_ID, OUTCOME_INDEX, PRICE, TIMESTAMP

# The lookup signature the simulator expects (simulator.PriceLookup is identical):
# (condition_id, outcome_index, ts) -> implied mid of that token, or None.
PriceLookup = Callable[[str, int, int], Optional[float]]

# Default Gamma key for the actual settlement instant (see resolution_ts_map).
_CLOSED_TIME_KEYS = ("closed_time_unix", "closedTime", "resolution_ts", "closed_time")


def _implied_mid0(price: np.ndarray, outcome_index: np.ndarray) -> np.ndarray:
    """Vectorised mid of OUTCOME_INDEX 0: price where index==0, else (1 - price)."""
    price = np.asarray(price, dtype=float)
    outcome_index = np.asarray(outcome_index, dtype=int)
    return np.where(outcome_index == 0, price, 1.0 - price)


def build_mid_series(trades_df: pd.DataFrame) -> dict[str, np.ndarray]:
    """Build, per market, the implied-mid step series of OUTCOME_INDEX 0 from prints.

    Returns ``{condition_id -> ndarray of shape (n, 2)}`` where each row is
    ``(ts, mid0)`` SORTED ascending by ts. ``mid0`` is the implied mid of token 0 at
    that print: ``price`` if the print touched outcome 0, else ``1 - price`` (the
    arbitrage-free complement). The mid of token 1 at any point is ``1 - mid0``.

    When several prints share a timestamp, the LAST one (in the post-sort order) wins,
    so each market's series has strictly increasing timestamps. The sort is stable, so
    "last at this ts" means the last such row in the input's original order.

    Empty input (or a market with no prints) yields an empty ``(0, 2)`` float array.
    """
    out: dict[str, np.ndarray] = {}
    if trades_df is None or len(trades_df) == 0:
        return out

    for col in (CONDITION_ID, TIMESTAMP, PRICE, OUTCOME_INDEX):
        if col not in trades_df.columns:
            raise ValueError(f"build_mid_series: trades_df missing column {col!r}")

    ts = trades_df[TIMESTAMP].to_numpy(dtype=np.int64)
    mid0 = _implied_mid0(trades_df[PRICE].to_numpy(), trades_df[OUTCOME_INDEX].to_numpy())
    cond = trades_df[CONDITION_ID].to_numpy(dtype=object)

    frame = pd.DataFrame({"cond": cond, "ts": ts, "mid0": mid0})
    # Stable sort by (cond, ts) preserves input order within a tie; keeping "last"
    # then yields the final print at any shared timestamp.
    frame = frame.sort_values(["cond", "ts"], kind="stable")
    frame = frame.drop_duplicates(subset=["cond", "ts"], keep="last")

    for condition_id, grp in frame.groupby("cond", sort=False):
        series = np.column_stack(
            (grp["ts"].to_numpy(dtype=float), grp["mid0"].to_numpy(dtype=float))
        )
        out[str(condition_id)] = series
    return out


def make_price_lookup(trades_df: pd.DataFrame) -> PriceLookup:
    """Build the at-or-before mid lookup callable from a trades frame.

    Returns ``lookup(condition_id, outcome_index, ts) -> float | None`` giving the
    implied mid of that token at the MOST RECENT print at or before ``ts``:

      * ``None`` if the market is unknown, or ``ts`` precedes the first print for it
        (we never extrapolate backwards — that would be marking against a quote that
        did not yet exist).
      * for ``outcome_index == 0`` the carried-forward ``mid0``;
      * for ``outcome_index == 1`` its complement ``1 - mid0``.

    The series are precomputed once via ``build_mid_series``; each call is an
    ``np.searchsorted`` (O(log n)), deterministic and side-effect free.
    """
    series = build_mid_series(trades_df)
    # Pre-split into parallel ts / mid0 arrays for fast searchsorted at call time.
    ts_by_cond = {c: arr[:, 0] for c, arr in series.items()}
    mid0_by_cond = {c: arr[:, 1] for c, arr in series.items()}

    def lookup(condition_id: str, outcome_index: int, ts: int) -> Optional[float]:
        key = str(condition_id)
        ts_arr = ts_by_cond.get(key)
        if ts_arr is None or ts_arr.size == 0:
            return None  # unknown market
        # rightmost index with ts_arr[idx] <= ts ; 'right' so an exact tie is included.
        pos = int(np.searchsorted(ts_arr, float(ts), side="right")) - 1
        if pos < 0:
            return None  # ts precedes the first print for this market
        mid0 = float(mid0_by_cond[key][pos])
        oi = int(outcome_index)
        if oi == 0:
            return mid0
        if oi == 1:
            return 1.0 - mid0
        raise ValueError(f"outcome_index must be 0 or 1, got {oi!r}")

    return lookup


def _coerce_closed_time(value: Any) -> Optional[int]:
    """Coerce a Gamma closedTime (ISO str / epoch / datetime) to unix secs, or None."""
    if value is None:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    try:
        return to_unix(value)
    except (TypeError, ValueError):
        return None


def resolution_ts_map(
    markets_or_df: Union[pd.DataFrame, Iterable[Mapping[str, Any]]],
) -> dict[str, int]:
    """Map ``condition_id -> settlement unix ts`` from market records or a DataFrame.

    Accepts either a DataFrame or an iterable of mapping-like market records. The
    settlement instant is the Gamma ``closedTime`` (the ACTUAL resolution time), looked
    up under any of: ``closed_time_unix``, ``closedTime``, ``resolution_ts``,
    ``closed_time`` — and coerced to unix seconds via ``leakage.to_unix`` (ISO strings
    ending ``Z`` or ``+00`` are handled). The condition id is read from ``condition_id``
    (schema) or ``conditionId`` (raw Gamma).

    Records with a missing/unparseable closed time are SKIPPED (a market with no known
    settlement instant simply never settles, per simulator semantics). This is the
    dict the simulator consumes as ``resolution_ts``.
    """
    out: dict[str, int] = {}

    if isinstance(markets_or_df, pd.DataFrame):
        records: Iterable[Mapping[str, Any]] = (
            row._asdict() for row in markets_or_df.itertuples(index=False)
        )
    else:
        records = markets_or_df

    for rec in records:
        cond = rec.get(CONDITION_ID) if hasattr(rec, "get") else None
        if cond is None and hasattr(rec, "get"):
            cond = rec.get("conditionId")
        if not cond:
            continue
        closed = None
        for k in _CLOSED_TIME_KEYS:
            if k in rec and rec[k] is not None:
                closed = _coerce_closed_time(rec[k])
                if closed is not None:
                    break
        if closed is None:
            continue
        out[str(cond)] = int(closed)
    return out


# --------------------------------------------------------------------------- #
# Self-check: tiny synthetic trades frame, run with the venv python.          #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    # A few prints on ONE market across rising timestamps, mixing both tokens so the
    # complement path is exercised. Implied mid0 (mid of outcome 0) at each ts:
    #   ts=100  BUY  index 0 @ 0.30  -> mid0 = 0.30
    #   ts=200  BUY  index 1 @ 0.55  -> mid0 = 1 - 0.55 = 0.45
    #   ts=300  BUY  index 0 @ 0.60  -> mid0 = 0.60
    #   ts=300  BUY  index 0 @ 0.62  -> mid0 = 0.62  (SAME ts: this LAST one must win)
    df = pd.DataFrame(
        {
            CONDITION_ID: ["0xc1"] * 4,
            TIMESTAMP: [100, 200, 300, 300],
            PRICE: [0.30, 0.55, 0.60, 0.62],
            OUTCOME_INDEX: [0, 1, 0, 0],
        }
    )

    # --- build_mid_series -------------------------------------------------- #
    series = build_mid_series(df)
    assert set(series.keys()) == {"0xc1"}, series.keys()
    arr = series["0xc1"]
    # Duplicate ts=300 collapsed to one row, last (0.62) kept -> 3 strictly-rising rows.
    assert arr.shape == (3, 2), arr.shape
    assert list(arr[:, 0]) == [100.0, 200.0, 300.0], arr[:, 0]
    assert np.allclose(arr[:, 1], [0.30, 0.45, 0.62]), arr[:, 1]
    # timestamps strictly increasing
    assert np.all(np.diff(arr[:, 0]) > 0), "timestamps must be strictly increasing"

    lookup = make_price_lookup(df)

    # --- None BEFORE the first print --------------------------------------- #
    assert lookup("0xc1", 0, 99) is None, "ts before first print must be None"
    assert lookup("0xc1", 1, 99) is None, "ts before first print must be None (index 1)"

    # --- exact-tie AT the print ts is included (side='right') -------------- #
    assert lookup("0xc1", 0, 100) == 0.30, "at-or-before must include exact ts"

    # --- at-or-before (LOCF) between prints -------------------------------- #
    # (mid0 at ts=250 derives from a 1-price complement, so compare with tolerance.)
    assert lookup("0xc1", 0, 150) == 0.30, "carry-forward last print before ts"
    assert abs(lookup("0xc1", 0, 250) - 0.45) < 1e-12, "carry-forward after the index-1 print"
    # last print at ts=300 was 0.62 (the dedup winner), not 0.60
    assert lookup("0xc1", 0, 300) == 0.62, "last print at a shared ts wins"
    assert lookup("0xc1", 0, 10_000) == 0.62, "carry-forward far past the last print"

    # --- the 1 - mid0 complement for outcome_index == 1 -------------------- #
    for ts, expected_mid0 in [(100, 0.30), (250, 0.45), (300, 0.62), (10_000, 0.62)]:
        m0 = lookup("0xc1", 0, ts)
        m1 = lookup("0xc1", 1, ts)
        assert m0 is not None and m1 is not None
        assert abs((m0 + m1) - 1.0) < 1e-12, (ts, m0, m1)
        assert abs(m1 - (1.0 - expected_mid0)) < 1e-12, (ts, m1, expected_mid0)

    # --- unknown market -> None -------------------------------------------- #
    assert lookup("0xUNKNOWN", 0, 300) is None, "unknown market must be None"

    # --- bad outcome_index rejected ---------------------------------------- #
    try:
        lookup("0xc1", 2, 300)
        raise AssertionError("outcome_index 2 should have raised")
    except ValueError:
        pass

    # --- resolution_ts_map: DataFrame + records, mixed key styles ---------- #
    rmap_df = resolution_ts_map(
        pd.DataFrame(
            {
                CONDITION_ID: ["0xc1", "0xc2", "0xc3"],
                "closed_time_unix": [1_735_000_000, None, None],
                "closedTime": [None, "2025-06-23T08:17:15Z", "2025-04-02 06:13:03+00"],
            }
        )
    )
    assert rmap_df["0xc1"] == 1_735_000_000, rmap_df
    assert rmap_df["0xc2"] == to_unix("2025-06-23T08:17:15Z"), rmap_df
    # space-separated +00 form (as seen in cached chosen_market_raw.json) parses too
    assert rmap_df["0xc3"] == to_unix("2025-04-02 06:13:03+00"), rmap_df

    rmap_recs = resolution_ts_map(
        [
            {"conditionId": "0xraw", "closedTime": "2025-09-01T00:00:00Z"},
            {"conditionId": "0xnoclose"},  # no closed time -> skipped
        ]
    )
    assert rmap_recs == {"0xraw": to_unix("2025-09-01T00:00:00Z")}, rmap_recs

    # --- empty input is structurally valid --------------------------------- #
    assert build_mid_series(pd.DataFrame(columns=[CONDITION_ID, TIMESTAMP, PRICE, OUTCOME_INDEX])) == {}
    empty_lookup = make_price_lookup(
        pd.DataFrame(columns=[CONDITION_ID, TIMESTAMP, PRICE, OUTCOME_INDEX])
    )
    assert empty_lookup("0xc1", 0, 1) is None
    assert resolution_ts_map(pd.DataFrame(columns=[CONDITION_ID])) == {}

    print("pricing.py self-check OK:")
    print("  build_mid_series      -> per-market (ts, mid0) step series, dedup-last")
    print("  make_price_lookup     -> at-or-before LOCF, None before first / unknown")
    print("  outcome 1 complement  -> mid0 + mid1 == 1 at every probe")
    print("  resolution_ts_map     -> closedTime (ISO/epoch/+00) -> unix, missing skipped")
