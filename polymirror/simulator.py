"""simulator.py — CUSTOM COMPONENT #2: out-of-sample mirror simulator + buy-the-
favorite benchmark + edge-decay (spec §6.2, §7.3, §7.5).

WHAT THIS DOES
--------------
For every eligible wallet's TEST-window BUY entry, we open the SAME directional
long the wallet opened (mirror), and — as the benchmark — we open a long on the
market FAVORITE at the same instant. The two legs differ ONLY in which side is
bought (rule R5); entry timing, exit timing, spread model, and settlement logic
are byte-for-byte identical. We then mark each leg to market (or to settlement)
at several horizons N and measure the EDGE = strategy_return - benchmark_return.

NULL HYPOTHESIS (stated, because a defensible null is a success):
    "Mirroring eligible wallets does NOT beat buy-the-favorite after costs."
i.e. mean per-wallet edge <= 0. We report a bootstrap two-sided p-value for it.

KEY DESIGN POINTS
-----------------
* Exit pricing is DEPENDENCY-INJECTED (`price_lookup`) so this module is unit-
  testable with synthetic closures — no network, no cached data, no wall-clock.
* NO LOOK-AHEAD (R1/R4): the favorite is chosen from mids AT ENTRY only. We never
  peek at the resolution to pick a side.
* SPREAD is a labelled assumption (R6): every fill goes through apply_spread with
  the active preset. Entry pays the half-spread up (BUY); a mark-to-market exit
  pays the half-spread down (SELL). A SETTLEMENT exit (token already resolved to
  0/1 before the horizon) pays NO spread — there is no book to cross at expiry.
* CLUSTER BY WALLET (R7, §7.5): we first average within each wallet, then bootstrap
  ACROSS wallets, so one hyperactive wallet cannot dominate the inference.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

# Path bootstrap (self-demo): when run as a top-level script
# (`python polymirror/simulator.py`), only this file's directory is on sys.path,
# so `import config` and `from polymirror ...` would fail. Prepend the project
# root in that case. No effect when imported as part of the package.
if __package__ in (None, ""):
    import sys
    from pathlib import Path

    _ROOT = Path(__file__).resolve().parent.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

import numpy as np
import pandas as pd

import config
from config import Config, SpreadPreset
from polymirror.costs import apply_spread
from polymirror.leakage import to_unix
from polymirror.schema import (
    CONDITION_ID,
    OUTCOME_INDEX,
    PRICE,
    SIDE,
    TIMESTAMP,
    USDC_SIZE,
    WALLET,
    WINNING_INDEX,
    WON,
    validate_trades,
)
from polymirror.stats import bootstrap_mean_ci, bootstrap_two_sided_p

# itertuples exposes columns as attributes under their column NAMES; the schema
# constants are valid identifiers, so getattr(row, <const>) reads each field.
WALLET_ATTR = WALLET
COND_ATTR = CONDITION_ID
TS_ATTR = TIMESTAMP
PRICE_ATTR = PRICE
SIDE_ATTR = SIDE
OUTCOME_ATTR = OUTCOME_INDEX
WON_ATTR = WON
VOL_ATTR = USDC_SIZE

# Type alias for the injected mid-price oracle: returns the mid of `outcome_index`
# in market `condition_id` at-or-before `ts`, or None when no quote is available.
PriceLookup = Callable[[str, int, int], Optional[float]]

SECONDS_PER_HOUR = 3600
EXIT_SETTLED = "settled"   # exited at 0/1 because the market resolved by the horizon
EXIT_MARK = "mark"         # exited by marking to market via price_lookup

# Columns of the raw per-(trade, N) DataFrame returned in Results.trades.
TRADE_COLUMNS = [
    "wallet", "condition_id", "N", "t0", "exit_ts",
    "entry_exec", "fav_entry_exec", "favorite_index",
    "strat_exit", "bench_exit", "strat_ret", "bench_ret", "edge", "exit_mode",
]


@dataclass(frozen=True)
class Results:
    """Immutable simulation output (§7.5).

    per_n   : list of per-horizon aggregate dicts (one per N in cfg.N_hours),
              each with strategy/benchmark/edge means, 95% bootstrap CIs, the
              edge two-sided p-value, and trade/wallet/drop counts.
    trades  : the raw per-(trade, N) DataFrame (TRADE_COLUMNS) — every simulated leg.
    meta    : {preset_name, n_eligible, null} provenance for the run.
    """
    per_n: list[dict]
    trades: pd.DataFrame
    meta: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Entry-leg construction (shared timing/spread; differs only in side bought)   #
# --------------------------------------------------------------------------- #
def _favorite_at_entry(
    cond: str, k: int, entry_mid: float, t0: int,
    price_lookup: PriceLookup, threshold: float,
) -> tuple[int, float]:
    """Pick the benchmark favorite from mids AT ENTRY ONLY (R4, no hindsight).

    Returns (favorite_index, favorite_mid). The other-token mid is read at t0;
    if unavailable we fall back to the complement (1 - touched_mid), which is the
    arbitrage-free implied mid of the opposite binary leg. The favorite is the
    side with the higher mid (the one priced above `threshold`).
    """
    other_mid = price_lookup(cond, 1 - k, t0)
    if other_mid is None:
        other_mid = 1.0 - entry_mid
    if entry_mid >= other_mid:
        return k, float(entry_mid)
    return 1 - k, float(other_mid)


def _exit_leg(
    cond: str, side_index: int, exit_ts: int, res: Optional[int],
    settled_value: float, price_lookup: PriceLookup,
    preset: SpreadPreset, vol: Optional[float],
) -> Optional[float]:
    """Exit price for one leg at `exit_ts`.

    If the market settled at-or-before the horizon (res is not None and
    exit_ts >= res) the leg exits at its 0/1 settlement value with NO spread.
    Otherwise it marks to market via price_lookup and pays the exit half-spread
    as a SELL. Returns None when a mark-to-market quote is missing (caller drops).
    """
    if res is not None and exit_ts >= res:
        return float(settled_value)
    mid = price_lookup(cond, side_index, exit_ts)
    if mid is None:
        return None
    return apply_spread(mid, "SELL", preset, vol)


# --------------------------------------------------------------------------- #
# Core simulation                                                             #
# --------------------------------------------------------------------------- #
def _simulate_rows(
    test_trades: pd.DataFrame, eligible: set[str], price_lookup: PriceLookup,
    cfg: Config, resolution_ts: Optional[dict], preset: SpreadPreset,
) -> tuple[list[dict], dict[int, int]]:
    """Build every per-(trade, N) row. Returns (rows, dropped_by_N).

    One BUY entry fans out into len(cfg.N_hours) candidate legs. A leg is DROPPED
    (counted, not recorded) when a required mark-to-market quote is missing.
    """
    # winning_index per condition is read from the test frame (ground truth join).
    winning_index = (
        test_trades[[CONDITION_ID, WINNING_INDEX]]
        .dropna()
        .drop_duplicates(CONDITION_ID)
        .set_index(CONDITION_ID)[WINNING_INDEX]
        .astype(int)
        .to_dict()
    )

    rows: list[dict] = []
    dropped: dict[int, int] = {int(N): 0 for N in cfg.N_hours}
    has_vol = USDC_SIZE in test_trades.columns

    for row in test_trades.itertuples(index=False):
        wallet = getattr(row, WALLET_ATTR)
        if wallet not in eligible:
            continue
        if getattr(row, SIDE_ATTR) != cfg.mirror_side:  # BUY-only by default
            continue

        cond = getattr(row, COND_ATTR)
        k = int(getattr(row, OUTCOME_ATTR))            # bought token index
        t0 = int(getattr(row, TS_ATTR))
        entry_mid = float(getattr(row, PRICE_ATTR))    # fill ~ mid at t0
        won = int(getattr(row, WON_ATTR))
        vol = float(getattr(row, VOL_ATTR)) if has_vol else None

        # Entry legs share timing/spread; they differ ONLY in which side is bought.
        entry_exec = apply_spread(entry_mid, "BUY", preset, vol)
        fav_index, fav_mid = _favorite_at_entry(
            cond, k, entry_mid, t0, price_lookup, cfg.favorite_threshold
        )
        fav_entry_exec = apply_spread(fav_mid, "BUY", preset, vol)

        res = resolution_ts.get(cond) if resolution_ts else None
        res = int(res) if res is not None else None
        win_k = winning_index.get(cond)

        for N in cfg.N_hours:
            N = int(N)
            exit_ts = t0 + N * SECONDS_PER_HOUR

            # Strategy leg: bought token k settles to WON (1 if it won else 0).
            strat_exit = _exit_leg(
                cond, k, exit_ts, res, float(won), price_lookup, preset, vol
            )
            # Benchmark leg: favorite settles to 1 iff it is the winning index.
            bench_settled = 1.0 if (win_k is not None and fav_index == win_k) else 0.0
            bench_exit = _exit_leg(
                cond, fav_index, exit_ts, res, bench_settled,
                price_lookup, preset, vol
            )

            if strat_exit is None or bench_exit is None:
                dropped[N] += 1
                continue

            strat_ret = (strat_exit - entry_exec) / entry_exec
            bench_ret = (bench_exit - fav_entry_exec) / fav_entry_exec
            exit_mode = EXIT_SETTLED if (res is not None and exit_ts >= res) else EXIT_MARK

            rows.append({
                "wallet": wallet, "condition_id": cond, "N": N,
                "t0": t0, "exit_ts": exit_ts,
                "entry_exec": entry_exec, "fav_entry_exec": fav_entry_exec,
                "favorite_index": fav_index,
                "strat_exit": float(strat_exit), "bench_exit": float(bench_exit),
                "strat_ret": float(strat_ret), "bench_ret": float(bench_ret),
                "edge": float(strat_ret - bench_ret), "exit_mode": exit_mode,
            })

    return rows, dropped


def _aggregate_for_n(
    df_n: pd.DataFrame, N: int, n_dropped: int, cfg: Config,
) -> dict:
    """Cluster-by-wallet aggregate + bootstrap inference for one horizon (§7.5).

    Per wallet take the MEAN strat_ret/bench_ret/edge, THEN across wallets compute
    the mean + 95% bootstrap CI; the edge p-value is the bootstrap two-sided test
    of H0: mean per-wallet edge == 0.
    """
    if len(df_n) == 0:
        nan = float("nan")
        return {
            "N": N, "mean_strat": nan, "strat_lo": nan, "strat_hi": nan,
            "mean_bench": nan, "bench_lo": nan, "bench_hi": nan,
            "mean_edge": nan, "edge_lo": nan, "edge_hi": nan, "p_edge": nan,
            "n_trades": 0, "n_wallets": 0, "n_dropped": int(n_dropped),
        }

    per_wallet = df_n.groupby("wallet")[["strat_ret", "bench_ret", "edge"]].mean()
    strat_vals = per_wallet["strat_ret"].to_numpy()
    bench_vals = per_wallet["bench_ret"].to_numpy()
    edge_vals = per_wallet["edge"].to_numpy()

    mean_strat, strat_lo, strat_hi = bootstrap_mean_ci(
        strat_vals, cfg.n_bootstrap, seed=cfg.seed, ci=0.95
    )
    mean_bench, bench_lo, bench_hi = bootstrap_mean_ci(
        bench_vals, cfg.n_bootstrap, seed=cfg.seed, ci=0.95
    )
    mean_edge, edge_lo, edge_hi = bootstrap_mean_ci(
        edge_vals, cfg.n_bootstrap, seed=cfg.seed, ci=0.95
    )
    p_edge = bootstrap_two_sided_p(edge_vals, cfg.n_bootstrap, cfg.seed)

    return {
        "N": N,
        "mean_strat": mean_strat, "strat_lo": strat_lo, "strat_hi": strat_hi,
        "mean_bench": mean_bench, "bench_lo": bench_lo, "bench_hi": bench_hi,
        "mean_edge": mean_edge, "edge_lo": edge_lo, "edge_hi": edge_hi,
        "p_edge": p_edge,
        "n_trades": int(len(df_n)), "n_wallets": int(per_wallet.shape[0]),
        "n_dropped": int(n_dropped),
    }


def simulate(
    eligible_wallets, test_trades: pd.DataFrame, price_lookup: PriceLookup,
    cfg: Config = config.DEFAULT, *,
    resolution_ts: Optional[dict] = None, preset: Optional[SpreadPreset] = None,
) -> Results:
    """Run the mirror strategy vs buy-the-favorite benchmark on TEST trades.

    Parameters
    ----------
    eligible_wallets : iterable of wallet ids selected on TRAINING data only (R1).
    test_trades      : resolved trades in the test window (schema.RESOLVED_COLUMNS).
    price_lookup     : (condition_id, outcome_index, ts) -> mid at-or-before ts | None.
    cfg              : Config (defaults to config.DEFAULT).
    resolution_ts    : {condition_id -> settlement unix ts}; missing => never settles
                       within any horizon (always marked to market).
    preset           : spread preset; defaults to cfg.preset().

    Returns
    -------
    Results(per_n, trades, meta).
    """
    # We tolerate SELL rows in the INPUT (allow_sell=True) and filter to BUY-only
    # ourselves via cfg.mirror_side below — that side filter is the BUY-only policy
    # (schema docstring: a SELL may be a close, not a fresh opening prediction).
    validate_trades(test_trades, resolved=True, allow_sell=True)
    preset = preset if preset is not None else cfg.preset()
    eligible = set(eligible_wallets)

    # R1 guard: the test window must not start before test_start. We assert on the
    # EARLIEST test timestamp so a mis-split frame explodes here, not silently.
    if cfg.test_start is not None and len(test_trades) > 0:
        ts_min = int(test_trades[TIMESTAMP].min())
        ts_start = to_unix(cfg.test_start)
        if ts_min < ts_start:
            raise ValueError(
                f"R1: test_trades earliest timestamp {ts_min} < test_start "
                f"{ts_start} — selection/simulation windows overlap."
            )

    rows, dropped = _simulate_rows(
        test_trades, eligible, price_lookup, cfg, resolution_ts, preset
    )
    trades_df = pd.DataFrame(rows, columns=TRADE_COLUMNS)

    per_n: list[dict] = []
    for N in cfg.N_hours:
        N = int(N)
        df_n = trades_df[trades_df["N"] == N] if len(trades_df) else trades_df
        per_n.append(_aggregate_for_n(df_n, N, dropped.get(N, 0), cfg))

    meta = {
        "preset_name": preset.name,
        "n_eligible": len(eligible),
        "null": "mirroring does not beat buy-the-favorite after costs.",
    }
    return Results(per_n=per_n, trades=trades_df, meta=meta)


def decay_curve(results: Results):
    """Extract (Ns, edges, edge_los, edge_his) across horizons for plotting (§7.3).

    The edge-decay curve shows how the mirror advantage erodes (or never existed)
    as the holding horizon N grows.
    """
    Ns = np.array([r["N"] for r in results.per_n], dtype=float)
    edges = np.array([r["mean_edge"] for r in results.per_n], dtype=float)
    edge_los = np.array([r["edge_lo"] for r in results.per_n], dtype=float)
    edge_his = np.array([r["edge_hi"] for r in results.per_n], dtype=float)
    return Ns, edges, edge_los, edge_his


# --------------------------------------------------------------------------- #
# Self-demo (run with the venv python)                                        #
# --------------------------------------------------------------------------- #
def _demo() -> Results:
    """Synthetic end-to-end run: eligible wallets, test trades, a closure price_lookup
    and a resolution_ts dict. Exercises BOTH exit modes (mark + settled) and the
    favorite/complement fallback. Fully seeded — no global RNG, no wall-clock (R8).
    """
    from polymirror.schema import (
        WALLET, CONDITION_ID as C, TIMESTAMP as T, PRICE as P, SIDE as S,
        OUTCOME_INDEX as OI, SIZE, USDC_SIZE as U, WINNING_INDEX as WI, WON as W,
        ENTRY_PROB as EP,
    )
    from polymirror.stats import make_rng

    rng = make_rng(config.DEFAULT.seed)
    t_base = 1_700_000_000  # fixed epoch anchor (no wall-clock)

    # Three conditions. cond_A resolves early (settles within 24h), cond_B/C late.
    conds = {
        "0x" + "a" * 64: {"win": 1, "res": t_base + 20 * SECONDS_PER_HOUR},
        "0x" + "b" * 64: {"win": 0, "res": t_base + 500 * SECONDS_PER_HOUR},
        "0x" + "c" * 64: {"win": 1, "res": None},  # never settles in horizon
    }

    # Build a deterministic mid path per (cond, outcome) as a small random walk in
    # (0,1). We hash with hashlib (NOT the salted built-in hash()) so the demo is
    # bit-identical across processes (R8: never rely on PYTHONHASHSEED).
    import hashlib

    def _stable_unit(cond: str, oi: int) -> float:
        h = hashlib.sha256(f"{cond}:{oi}".encode()).hexdigest()
        return (int(h[:8], 16) % 7) / 7.0

    def mid_at(cond: str, oi: int, ts: int) -> float:
        steps = max(0, (ts - t_base)) // SECONDS_PER_HOUR
        base = 0.45 + 0.10 * _stable_unit(cond, oi)
        drift = 0.002 * steps * (1 if oi == conds[cond]["win"] else -1)
        val = base + drift + 0.01 * float(rng.standard_normal())
        return float(min(max(val, 0.02), 0.98))

    price_cache: dict = {}

    def price_lookup(cond: str, oi: int, ts: int):
        # Deterministic + cached so repeated lookups for the same key are stable.
        key = (cond, int(oi), int(ts))
        if key not in price_cache:
            price_cache[key] = mid_at(cond, int(oi), int(ts))
        return price_cache[key]

    # Synthetic test trades: 4 wallets, several BUY entries (+ a SELL to prove it's dropped).
    records = []
    cond_ids = list(conds.keys())
    for w in range(4):
        wallet = "0xW" + str(w).rjust(40, "0")
        for j in range(6):
            cond = cond_ids[(w + j) % len(cond_ids)]
            k = (w + j) % 2
            t0 = t_base + (j + 1) * SECONDS_PER_HOUR
            entry = price_lookup(cond, k, t0)
            won = int(k == conds[cond]["win"])
            records.append({
                WALLET: wallet, C: cond, T: t0, P: entry, S: "BUY",
                OI: k, SIZE: 100.0, U: 250.0 + 50.0 * j,
                WI: conds[cond]["win"], W: won, EP: entry,
            })
    # One SELL row (eligible wallet) — must be ignored by BUY-only policy.
    c0 = cond_ids[0]
    records.append({
        WALLET: "0xW" + "0" * 41, C: c0, T: t_base + 2 * SECONDS_PER_HOUR,
        P: 0.5, S: "SELL", OI: 0, SIZE: 100.0, U: 300.0,
        WI: conds[c0]["win"], W: int(0 == conds[c0]["win"]), EP: 0.5,
    })

    test_trades = pd.DataFrame.from_records(records)
    # allow_sell for validation since we deliberately included a SELL row.
    validate_trades(test_trades, resolved=True, allow_sell=True)

    eligible = {"0xW" + str(w).rjust(40, "0") for w in range(3)}  # wallet 3 excluded
    resolution_ts = {c: v["res"] for c, v in conds.items() if v["res"] is not None}

    # Use a config without a test_start gate (synthetic timestamps are bare epochs).
    cfg = config.DEFAULT.with_(test_start=None)
    results = simulate(
        eligible, test_trades, price_lookup, cfg, resolution_ts=resolution_ts
    )

    print(f"null: {results.meta['null']}")
    print(f"preset={results.meta['preset_name']}  n_eligible={results.meta['n_eligible']}  "
          f"raw_legs={len(results.trades)}\n")
    hdr = (f"{'N(h)':>5} {'mean_strat':>11} {'mean_bench':>11} {'mean_edge':>11} "
           f"{'edge_lo':>9} {'edge_hi':>9} {'p_edge':>7} {'trades':>7} "
           f"{'wallets':>8} {'dropped':>8}")
    print(hdr)
    print("-" * len(hdr))
    for r in results.per_n:
        print(f"{r['N']:>5} {r['mean_strat']:>11.5f} {r['mean_bench']:>11.5f} "
              f"{r['mean_edge']:>11.5f} {r['edge_lo']:>9.5f} {r['edge_hi']:>9.5f} "
              f"{r['p_edge']:>7.4f} {r['n_trades']:>7} {r['n_wallets']:>8} "
              f"{r['n_dropped']:>8}")

    Ns, edges, los, his = decay_curve(results)
    print(f"\ndecay_curve Ns={Ns.tolist()}  edges={[round(e, 5) for e in edges]}")
    return results


if __name__ == "__main__":
    _demo()
