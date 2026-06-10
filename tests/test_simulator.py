"""test_simulator.py — adversarial tests for the CUSTOM mirror/benchmark simulator.

Contract under test (README R4/R5/R6/R7/R8, simulator.py docstring).

Real API (verified against polymirror/simulator.py):
  simulate(eligible_wallets, test_trades, price_lookup, cfg=DEFAULT, *,
           resolution_ts=None, preset=None) -> Results(per_n, trades, meta)
  - price_lookup(condition_id, outcome_index, ts) -> mid | None
  - per-trade rows in Results.trades carry strat_ret/bench_ret/edge, where
        strat_ret = (strat_exit - entry_exec) / entry_exec   (fractional return)
  - Results.per_n[i] aggregates horizon cfg.N_hours[i] with keys
        mean_strat / mean_bench / mean_edge / n_trades / n_wallets / n_dropped ...
  - settlement before the horizon (resolution_ts) exits at 0/1 with NO spread;
    otherwise the exit marks to market via price_lookup as a SELL.

Tests pin one fully hand-computed trade plus the contract SIGNS/ORDERING/CLUSTERING/
DETERMINISM. Hand values were cross-checked against the implementation arithmetic.
"""
from __future__ import annotations

import pandas as pd
import pytest

import config as cfg_mod
from config import SPREAD_PRESETS
from polymirror.costs import apply_spread
from polymirror.simulator import SECONDS_PER_HOUR, Results, decay_curve, simulate
from polymirror.schema import compute_won, USDC_SIZE as USDC, WALLET as W, \
    CONDITION_ID as C, TIMESTAMP as T, PRICE as P, SIDE as S, \
    OUTCOME_INDEX as OI, SIZE as SZ, WINNING_INDEX as WI, WON as WON_C, \
    ENTRY_PROB as EP

ENTRY_TS = 1_000_000
LIQUID_VOL = 1e9  # large USDC volume -> no thinness multiplier, clean hand math.

OPT = SPREAD_PRESETS["optimistic"]


def _cfg(**overrides):
    # test_start=None so bare-epoch synthetic timestamps are not gated by R1's
    # test-window check (that gate is exercised explicitly in its own test).
    base = cfg_mod.DEFAULT.with_(test_start=None)
    return base.with_(**overrides) if overrides else base


def _trades(rows):
    """Build a resolved trades DataFrame from compact row dicts (all BUY by default)."""
    recs = []
    for r in rows:
        oi = int(r["outcome_index"])
        wi = int(r["winning_index"])
        price = float(r["price"])
        recs.append({
            W: r["wallet"], C: r["condition_id"], T: int(r.get("timestamp", ENTRY_TS)),
            P: price, S: r.get("side", "BUY"), OI: oi, SZ: 100.0,
            USDC: float(r.get("usdc_size", LIQUID_VOL)),
            WI: wi, WON_C: int(compute_won(oi, wi)), EP: price,
        })
    return pd.DataFrame.from_records(recs)


def _lookup_none(cond, oi, ts):
    """No mid available -> favorite falls back to the complement; mark exits drop."""
    return None


def _lookup_winner_high(winners):
    """Oracle pricing the winning index near 1.0 and the loser near 0.0."""
    def _f(cond, oi, ts):
        win = winners.get(cond)
        if win is None:
            return 0.5
        return 0.99 if int(oi) == int(win) else 0.01
    return _f


def _settled_at_entry(conds):
    """resolution_ts mapping that makes every condition settle at the entry instant,
    so exits are SETTLED (0/1, no exit spread) at every positive horizon."""
    return {c: ENTRY_TS for c in conds}


# --------------------------------------------------------------------------- #
# One fully hand-computed trade                                               #
# --------------------------------------------------------------------------- #
def test_single_settled_winner_matches_hand_computed_return():
    # Arrange: wallet buys the FAVORITE (outcome 0 @ mid 0.70) which wins; the market
    # settles at entry so the exit is at 1.0 with no exit spread. Liquid contract,
    # optimistic preset. Because the mirrored side IS the favorite, the two legs are
    # identical and edge == 0.
    cfg = _cfg(spread_preset="optimistic", N_hours=(1,))
    df = _trades([{"wallet": "w", "condition_id": "0xc1", "price": 0.70,
                   "outcome_index": 0, "winning_index": 0}])

    # Act
    res = simulate(["w"], df, _lookup_none, cfg, resolution_ts=_settled_at_entry(["0xc1"]))
    trade = res.trades.iloc[0]

    # Assert — hand math (optimistic: base_cents=1.0, boundary_k=0.5):
    #   full_spread = 1.0 * (1 + 0.5*(1 - 4*0.7*0.3)) = 1.0 * 1.08 = 1.08 cents
    #   half_spread = 1.08 / 100 / 2 = 0.0054
    #   entry_exec  = apply_spread(0.70, BUY, optimistic, liquid) = 0.70 + 0.0054 = 0.7054
    #   strat_exit  = 1.0 (settled winner, no spread)
    #   strat_ret   = (1.0 - 0.7054) / 0.7054 = 0.4176353841...
    entry_exec = apply_spread(0.70, "BUY", OPT, LIQUID_VOL)
    assert trade["entry_exec"] == pytest.approx(entry_exec)
    assert trade["strat_exit"] == pytest.approx(1.0)
    assert trade["strat_ret"] == pytest.approx((1.0 - entry_exec) / entry_exec)
    assert trade["strat_ret"] == pytest.approx(0.4176353841791893, rel=1e-9)
    assert trade["edge"] == pytest.approx(0.0, abs=1e-12)
    assert res.per_n[0]["mean_edge"] == pytest.approx(0.0, abs=1e-12)


def test_edge_equals_strat_ret_minus_bench_ret_by_definition():
    # The experiment's definition (R5): edge is strategy minus benchmark, per trade.
    cfg = _cfg(spread_preset="optimistic", N_hours=(1,))
    df = _trades([{"wallet": "w", "condition_id": "0xc1", "price": 0.55,
                   "outcome_index": 0, "winning_index": 0}])
    res = simulate(["w"], df, _lookup_winner_high({"0xc1": 0}), cfg,
                   resolution_ts=_settled_at_entry(["0xc1"]))
    t = res.trades.iloc[0]
    assert t["edge"] == pytest.approx(t["strat_ret"] - t["bench_ret"], abs=1e-12)


# --------------------------------------------------------------------------- #
# Resolution BEFORE the horizon settles at 0/1 (and ignores the oracle)        #
# --------------------------------------------------------------------------- #
def test_settlement_before_horizon_exits_at_one_ignoring_misleading_oracle():
    # Arrange: market settles +1h after entry; horizon is 48h, so the leg is SETTLED.
    # The oracle is deliberately misleading (0.10) — if the sim (wrongly) marked to
    # market the return would be negative; settlement to 1.0 makes it positive.
    cfg = _cfg(spread_preset="optimistic", N_hours=(48,))
    df = _trades([{"wallet": "w", "condition_id": "0xc1", "price": 0.40,
                   "outcome_index": 0, "winning_index": 0}])

    def misleading(cond, oi, ts):
        return 0.10

    # Act
    res = simulate(["w"], df, misleading, cfg,
                   resolution_ts={"0xc1": ENTRY_TS + 1 * SECONDS_PER_HOUR})
    t = res.trades.iloc[0]

    # Assert
    assert t["exit_mode"] == "settled"
    assert t["strat_exit"] == pytest.approx(1.0)
    assert t["strat_ret"] > 0.0


def test_settled_loser_exits_at_zero_giving_minus_one_return():
    # A bought loser that settles before the horizon exits at 0.0 -> strat_ret == -1.
    cfg = _cfg(spread_preset="optimistic", N_hours=(1,))
    df = _trades([{"wallet": "w", "condition_id": "0xc1", "price": 0.40,
                   "outcome_index": 0, "winning_index": 1}])  # bought 0, winner is 1
    res = simulate(["w"], df, _lookup_none, cfg, resolution_ts=_settled_at_entry(["0xc1"]))
    t = res.trades.iloc[0]
    assert t["strat_exit"] == pytest.approx(0.0)
    assert t["strat_ret"] == pytest.approx(-1.0)


# --------------------------------------------------------------------------- #
# R4/R5: mirrored UNDERDOG that loses to the favorite -> NEGATIVE edge         #
# --------------------------------------------------------------------------- #
def test_underdog_mirror_when_favorite_wins_has_negative_edge():
    # Arrange: wallet bought the UNDERDOG (outcome 1 @ 0.30), but the FAVORITE
    # (outcome 0, complement mid 0.70) is the winner. Strategy follows into the loser
    # (settles to 0); benchmark buys the favorite winner (settles to 1).
    cfg = _cfg(spread_preset="optimistic", N_hours=(1,), favorite_threshold=0.5)
    df = _trades([{"wallet": "w", "condition_id": "0xc1", "price": 0.30,
                   "outcome_index": 1, "winning_index": 0}])

    # Act: lookup returns None so the favorite is the complement (0.70 > 0.30).
    res = simulate(["w"], df, _lookup_none, cfg, resolution_ts=_settled_at_entry(["0xc1"]))
    t = res.trades.iloc[0]

    # Assert
    assert t["favorite_index"] == 0
    assert t["strat_ret"] < 0.0
    assert t["strat_ret"] < t["bench_ret"]
    assert t["edge"] < 0.0
    assert res.per_n[0]["mean_edge"] < 0.0


# --------------------------------------------------------------------------- #
# R6: a wider spread (conservative) reduces strategy return vs optimistic       #
# --------------------------------------------------------------------------- #
def test_conservative_spread_reduces_strategy_return_settled_exit():
    # Settled exit pays spread on ENTRY only; conservative entry costs more than
    # optimistic, so the realised return is strictly lower.
    df = _trades([{"wallet": "w", "condition_id": "0xc1", "price": 0.55,
                   "outcome_index": 0, "winning_index": 0}])

    def run(preset):
        cfg = _cfg(spread_preset=preset, N_hours=(1,))
        res = simulate(["w"], df, _lookup_winner_high({"0xc1": 0}), cfg,
                       resolution_ts=_settled_at_entry(["0xc1"]))
        return res.trades.iloc[0]["strat_ret"]

    assert run("conservative") < run("optimistic")


def test_conservative_spread_reduces_strategy_return_mark_to_market_exit():
    # Mark-to-market exit pays spread on BOTH entry and exit, so the cost gap is even
    # larger; ordering must still hold.
    df = _trades([{"wallet": "w", "condition_id": "0xc1", "price": 0.55,
                   "outcome_index": 0, "winning_index": 0}])

    def run(preset):
        cfg = _cfg(spread_preset=preset, N_hours=(1,))
        res = simulate(["w"], df, _lookup_winner_high({"0xc1": 0}), cfg, resolution_ts=None)
        assert res.trades.iloc[0]["exit_mode"] == "mark"
        return res.trades.iloc[0]["strat_ret"]

    assert run("conservative") < run("optimistic")


# --------------------------------------------------------------------------- #
# R7: per-wallet clustering — one hyperactive wallet == one unit               #
# --------------------------------------------------------------------------- #
def test_per_wallet_clustering_gives_each_wallet_one_unit():
    # Arrange: 'heavy' makes 100 identical WINNING trades; 'light' makes 1 LOSING
    # trade. Pooled per-trade the mean would be ~+0.96 (heavy dominates). Clustered
    # PER WALLET (R7) the across-wallet mean is (heavy_mean + light_mean)/2.
    cfg = _cfg(spread_preset="optimistic", N_hours=(1,))
    rows, conds = [], []
    for i in range(100):
        c = f"0xh{i}"
        conds.append(c)
        rows.append({"wallet": "heavy", "condition_id": c, "price": 0.50,
                     "outcome_index": 0, "winning_index": 0})  # wins
    conds.append("0xl0")
    rows.append({"wallet": "light", "condition_id": "0xl0", "price": 0.50,
                 "outcome_index": 0, "winning_index": 1})       # loses
    df = _trades(rows)

    # Act
    res = simulate(["heavy", "light"], df, _lookup_none, cfg,
                   resolution_ts=_settled_at_entry(conds))
    pn = res.per_n[0]

    # Hand math: entry_exec at 0.50 optimistic liquid; heavy_ret=(1-ee)/ee,
    # light_ret=(0-ee)/ee=-1. Across-wallet mean = (heavy_ret + light_ret)/2.
    ee = apply_spread(0.50, "BUY", OPT, LIQUID_VOL)
    heavy_ret = (1.0 - ee) / ee
    light_ret = -1.0
    two_unit_mean = (heavy_ret + light_ret) / 2.0
    pooled_mean = (100 * heavy_ret + 1 * light_ret) / 101.0

    # Assert: clustered by wallet, NOT pooled by trade.
    assert pn["n_trades"] == 101
    assert pn["n_wallets"] == 2
    assert pn["mean_strat"] == pytest.approx(two_unit_mean, abs=1e-9)
    assert abs(pn["mean_strat"] - two_unit_mean) < abs(pn["mean_strat"] - pooled_mean)


# --------------------------------------------------------------------------- #
# BUY-only policy and eligibility filtering                                   #
# --------------------------------------------------------------------------- #
def test_sell_rows_are_ignored_buy_only_policy():
    # A SELL entry is not a fresh opening long (schema docstring) -> contributes nothing.
    cfg = _cfg(spread_preset="optimistic", N_hours=(1,))
    df = _trades([{"wallet": "w", "condition_id": "0xc1", "price": 0.60,
                   "outcome_index": 0, "winning_index": 0, "side": "SELL"}])
    res = simulate(["w"], df, _lookup_winner_high({"0xc1": 0}), cfg,
                   resolution_ts=_settled_at_entry(["0xc1"]))
    assert res.per_n[0]["n_trades"] == 0


def test_non_eligible_wallets_are_excluded():
    cfg = _cfg(spread_preset="optimistic", N_hours=(1,))
    df = _trades([{"wallet": "w", "condition_id": "0xc1", "price": 0.60,
                   "outcome_index": 0, "winning_index": 0}])
    # Empty eligible set -> no legs simulated.
    res = simulate([], df, _lookup_winner_high({"0xc1": 0}), cfg,
                   resolution_ts=_settled_at_entry(["0xc1"]))
    assert res.per_n[0]["n_trades"] == 0


def test_missing_mark_to_market_quote_drops_the_leg():
    # No settlement + a None mid at exit means the leg cannot be priced -> dropped.
    cfg = _cfg(spread_preset="optimistic", N_hours=(1,))
    df = _trades([{"wallet": "w", "condition_id": "0xc1", "price": 0.60,
                   "outcome_index": 0, "winning_index": 0}])
    res = simulate(["w"], df, _lookup_none, cfg, resolution_ts=None)
    assert res.per_n[0]["n_trades"] == 0
    assert res.per_n[0]["n_dropped"] == 1


# --------------------------------------------------------------------------- #
# R1: simulate guards the test-window boundary                                #
# --------------------------------------------------------------------------- #
def test_simulate_rejects_trades_before_test_start():
    # A test frame whose earliest timestamp precedes cfg.test_start overlaps the
    # selection window -> must explode (R1).
    cfg = cfg_mod.DEFAULT.with_(test_start=ENTRY_TS + 10, N_hours=(1,),
                                spread_preset="optimistic")
    df = _trades([{"wallet": "w", "condition_id": "0xc1", "timestamp": ENTRY_TS,
                   "price": 0.60, "outcome_index": 0, "winning_index": 0}])
    with pytest.raises(ValueError):
        simulate(["w"], df, _lookup_none, cfg, resolution_ts=_settled_at_entry(["0xc1"]))


# --------------------------------------------------------------------------- #
# R8: determinism for a fixed seed                                            #
# --------------------------------------------------------------------------- #
def test_simulator_is_deterministic_for_a_fixed_seed():
    # Arrange: a mixed population marked to market (so bootstrap CIs are exercised).
    cfg = _cfg(spread_preset="base", N_hours=(1,), seed=999)
    df = _trades([
        {"wallet": "a", "condition_id": "0xa", "price": 0.60, "outcome_index": 0, "winning_index": 0},
        {"wallet": "b", "condition_id": "0xb", "price": 0.35, "outcome_index": 1, "winning_index": 0},
        {"wallet": "c", "condition_id": "0xc", "price": 0.50, "outcome_index": 0, "winning_index": 1},
    ])
    lookup = _lookup_winner_high({"0xa": 0, "0xb": 0, "0xc": 1})

    # Act: identical inputs + seed, twice.
    r1 = simulate(["a", "b", "c"], df, lookup, cfg, resolution_ts=None)
    r2 = simulate(["a", "b", "c"], df, lookup, cfg, resolution_ts=None)

    # Assert: every aggregate (means AND bootstrap CIs/p-values) is bit-identical.
    assert r1.per_n == r2.per_n


# --------------------------------------------------------------------------- #
# decay_curve helper shape                                                     #
# --------------------------------------------------------------------------- #
def test_decay_curve_returns_one_point_per_horizon():
    cfg = _cfg(spread_preset="optimistic", N_hours=(1, 6, 24))
    df = _trades([{"wallet": "w", "condition_id": "0xc1", "price": 0.60,
                   "outcome_index": 0, "winning_index": 0}])
    res = simulate(["w"], df, _lookup_winner_high({"0xc1": 0}), cfg,
                   resolution_ts=_settled_at_entry(["0xc1"]))
    Ns, edges, los, his = decay_curve(res)
    assert Ns.tolist() == [1.0, 6.0, 24.0]
    assert len(edges) == len(los) == len(his) == 3
