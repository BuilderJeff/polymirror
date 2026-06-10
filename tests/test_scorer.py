"""test_scorer.py — adversarial tests for the CUSTOM per-wallet scorer + luck filter.

Contract under test (README R2/R3, scorer.py docstring). AAA style, descriptive names.

Real API (verified against polymirror/scorer.py):
  score_wallets(train_trades, cfg) -> DataFrame[wallet, n_trades, brier, logloss, score]
  luck_filter(scores, train_trades, cfg) -> adds p_value, eligible_raw, eligible_fdr,
      null_mean, null_margin; .attrs carries bh_crit / survivor counts.

SKILL SEMANTICS — an important, deliberately-pinned point (R2)
-------------------------------------------------------------
Accuracy is a PROPER SCORE on ENTRY PRICES vs realized outcomes. The luck null holds
each wallet's entry prices p_i FIXED and redraws outcomes y* ~ Bernoulli(p_i). So
"skill" means a wallet's REALIZED accuracy beats what its own entry prices implied —
NOT merely that its picks won. A wallet that bought longshots at p=0.2 that all won is
actually BADLY calibrated under this score (Brier = (0.2-1)^2 = 0.64, worse than its
own Bernoulli(0.2) null), and correctly gets p_value = 1.0. The genuine-skill fixture
below therefore buys at a modest price (0.55) on picks that win far more often (~0.85),
matching scorer.py's own _build_synthetic skill model. This mirrors a real adversarial
finding: the loose task phrasing ("longshots at 0.2 that all won = skill") contradicts
the proper-scoring contract; the contract is what we test.
"""
from __future__ import annotations

import pandas as pd
import pytest

import config as cfg_mod
from polymirror.leakage import LeakageError
from polymirror.scorer import luck_filter, score_wallets, select_eligible
from polymirror.schema import WALLET

TRAIN_END_TS = 1_700_000_000  # fixed cutoff; all fixture trades sit strictly before it.


def _cfg(**overrides):
    base = cfg_mod.DEFAULT.with_(
        train_end=TRAIN_END_TS, test_start=TRAIN_END_TS, accuracy_metric="brier",
    )
    return base.with_(**overrides) if overrides else base


def _wallet_rows(wallet, *, n, price, n_won, start_ts=1):
    """n BUY rows for one wallet at fixed entry price; the first n_won won.

    Bought token is always outcome_index 0; winning_index is 0 (win) or 1 (loss),
    so `won` (bought == winner) is 1 for exactly the first n_won rows. Distinct
    condition_ids keep the rows independent. All timestamps < TRAIN_END_TS.
    """
    rows = []
    for i in range(n):
        won = i < n_won
        rows.append({
            "wallet": wallet,
            "condition_id": f"0x{(abs(hash((wallet, i))) % (16 ** 64)):064x}",
            "timestamp": start_ts + i,
            "price": price,
            "outcome_index": 0,
            "winning_index": 0 if won else 1,
        })
    return rows


# --------------------------------------------------------------------------- #
# score_wallets — proper score arithmetic on entry prices vs outcomes (R2)     #
# --------------------------------------------------------------------------- #
def test_score_wallets_reports_hand_computed_brier(make_resolved_trades):
    # Arrange: 10 BUYs at p=0.2, all won (y=1) -> Brier = (0.2-1)^2 = 0.64.
    cfg = _cfg(min_trades_per_wallet=10)
    df = make_resolved_trades(_wallet_rows("w", n=10, price=0.2, n_won=10))

    # Act
    scores = score_wallets(df, cfg)

    # Assert
    assert len(scores) == 1
    row = scores.iloc[0]
    assert row["n_trades"] == 10
    assert row["brier"] == pytest.approx(0.64)
    # accuracy_metric == 'brier' -> score column mirrors brier.
    assert row["score"] == pytest.approx(row["brier"])


def test_score_wallets_better_calibrated_wallet_has_lower_brier(make_resolved_trades):
    # Arrange: confident-and-right (p=0.9, all win) vs timid-and-right (p=0.6, all win).
    cfg = _cfg(min_trades_per_wallet=5)
    confident = make_resolved_trades(_wallet_rows("conf", n=5, price=0.9, n_won=5))
    timid = make_resolved_trades(_wallet_rows("timid", n=5, price=0.6, n_won=5))

    # Act
    s_conf = score_wallets(confident, cfg).iloc[0]["brier"]   # (0.1)^2 = 0.01
    s_timid = score_wallets(timid, cfg).iloc[0]["brier"]      # (0.4)^2 = 0.16

    # Assert
    assert s_conf < s_timid


# --------------------------------------------------------------------------- #
# Luck filter — genuine skill (accuracy beats entry prices) is flagged         #
# --------------------------------------------------------------------------- #
def test_genuinely_skilled_wallet_beats_luck_null(make_resolved_trades):
    # Arrange: buys at 0.55 on picks that win ~85% (34/40) — realized accuracy far
    # exceeds the implied 0.55, so the wallet beats its own Bernoulli(0.55) null.
    cfg = _cfg(min_trades_per_wallet=20)
    df = make_resolved_trades(_wallet_rows("skill_w", n=40, price=0.55, n_won=34))

    # Act
    table = luck_filter(score_wallets(df, cfg), df, cfg)
    row = table[table[WALLET] == "skill_w"].iloc[0]

    # Assert: significant p-value, better-than-null margin, raw-eligible.
    assert row["p_value"] < cfg.alpha
    assert row["null_margin"] < 0.0           # observed score better (lower) than null mean
    assert bool(row["eligible_raw"]) is True


def test_longshot_winner_is_not_skill_under_proper_score(make_resolved_trades):
    # Arrange: the loosely-described "12 longshots at 0.2 that all won" case. Under the
    # proper-scoring contract this is BAD calibration, not skill: p_value must NOT clear
    # alpha. This test pins the contract against the misleading intuition.
    cfg = _cfg(min_trades_per_wallet=10)
    df = make_resolved_trades(_wallet_rows("longshot_w", n=12, price=0.2, n_won=12))

    # Act
    row = luck_filter(score_wallets(df, cfg), df, cfg).iloc[0]

    # Assert: a price-taker who underpaid does not beat its OWN-price null on score.
    assert row["p_value"] >= cfg.alpha
    assert bool(row["eligible_raw"]) is False


# --------------------------------------------------------------------------- #
# Luck filter — a coin-flipper with ~half wins is NOT significant              #
# --------------------------------------------------------------------------- #
def test_coinflip_wallet_is_not_flagged_as_skilled(make_resolved_trades):
    # Arrange: ~40 bets at p=0.5, exactly half winning — pure noise, no edge.
    cfg = _cfg(min_trades_per_wallet=30)
    df = make_resolved_trades(_wallet_rows("noise_w", n=40, price=0.5, n_won=20))

    # Act
    row = luck_filter(score_wallets(df, cfg), df, cfg).iloc[0]

    # Assert
    assert row["p_value"] >= cfg.alpha
    assert bool(row["eligible_raw"]) is False
    assert bool(row["eligible_fdr"]) is False


# --------------------------------------------------------------------------- #
# min_trades_per_wallet: thin wallets are dropped before scoring               #
# --------------------------------------------------------------------------- #
def test_wallets_below_min_trades_are_dropped(make_resolved_trades):
    # Arrange: 30-trade wallet (eligible) + 3-trade wallet (too thin).
    cfg = _cfg(min_trades_per_wallet=30)
    big = _wallet_rows("big_w", n=30, price=0.4, n_won=15)
    small = _wallet_rows("small_w", n=3, price=0.4, n_won=2)
    df = make_resolved_trades(big + small)

    # Act
    scored = set(score_wallets(df, cfg)[WALLET])

    # Assert
    assert "small_w" not in scored
    assert "big_w" in scored


def test_min_trades_boundary_is_inclusive(make_resolved_trades):
    # A wallet with EXACTLY min_trades_per_wallet trades is kept (>=, not >).
    cfg = _cfg(min_trades_per_wallet=30)
    df = make_resolved_trades(_wallet_rows("exact_w", n=30, price=0.4, n_won=15))
    assert "exact_w" in set(score_wallets(df, cfg)[WALLET])


# --------------------------------------------------------------------------- #
# FDR: Benjamini-Hochberg survivors never exceed raw-alpha survivors           #
# --------------------------------------------------------------------------- #
def test_fdr_survivors_do_not_exceed_raw_survivors(make_resolved_trades):
    # Arrange: a population of skilled + noise wallets large enough that multiple
    # testing can bite. Two clear-skill wallets and eight coin-flippers.
    cfg = _cfg(min_trades_per_wallet=20)
    rows = []
    rows += _wallet_rows("skill_a", n=30, price=0.55, n_won=26)
    rows += _wallet_rows("skill_b", n=30, price=0.5, n_won=24)
    for j in range(8):
        rows += _wallet_rows(f"noise_{j}", n=30, price=0.5, n_won=15)
    df = make_resolved_trades(rows)

    # Act
    table = luck_filter(score_wallets(df, cfg), df, cfg.with_(fdr=True))
    n_raw = int(table["eligible_raw"].sum())
    n_fdr = int(table["eligible_fdr"].sum())

    # Assert: FDR is the more conservative gate.
    assert n_fdr <= n_raw
    # And eligible_fdr is a subset of eligible_raw, wallet-by-wallet.
    assert (~table["eligible_raw"] & table["eligible_fdr"]).sum() == 0


def test_fdr_disabled_makes_fdr_flag_equal_raw_flag(make_resolved_trades):
    # With cfg.fdr=False the eligible_fdr column must equal eligible_raw exactly.
    cfg = _cfg(min_trades_per_wallet=20, fdr=False)
    rows = _wallet_rows("skill_a", n=30, price=0.55, n_won=26)
    rows += _wallet_rows("noise_0", n=30, price=0.5, n_won=15)
    df = make_resolved_trades(rows)

    table = luck_filter(score_wallets(df, cfg), df, cfg)
    assert (table["eligible_fdr"] == table["eligible_raw"]).all()


# --------------------------------------------------------------------------- #
# Determinism (R8): same seed -> identical p-values                            #
# --------------------------------------------------------------------------- #
def test_luck_filter_is_deterministic_for_a_fixed_seed(make_resolved_trades):
    # Arrange
    cfg = _cfg(min_trades_per_wallet=15, seed=12345)
    rows = _wallet_rows("w1", n=18, price=0.3, n_won=12)
    rows += _wallet_rows("w2", n=22, price=0.45, n_won=11)
    df = make_resolved_trades(rows)

    # Act: identical inputs + seed, run twice.
    a = luck_filter(score_wallets(df, cfg), df, cfg).sort_values(WALLET)
    b = luck_filter(score_wallets(df, cfg), df, cfg).sort_values(WALLET)

    # Assert: every per-wallet p-value matches to the bit (no wall-clock, no global RNG).
    assert a[WALLET].tolist() == b[WALLET].tolist()
    for pa, pb in zip(a["p_value"], b["p_value"]):
        assert pa == pytest.approx(pb, abs=0.0, rel=0.0)


def test_luck_filter_pvalues_change_with_seed(make_resolved_trades):
    # A different seed should perturb the Monte-Carlo p-values (proves they are
    # actually seeded by cfg.seed, not a constant). We deliberately choose a
    # MODERATE-skill wallet whose observed score lands in the SENSITIVE tail of its
    # null (p ~ 0.03), so resampling noise moves the empirical p-value. (Wallets whose
    # score sits on top of the null mass pin to p=1.0 under every seed, which would
    # not exercise the seed dependence.)
    cfg_a = _cfg(min_trades_per_wallet=15, seed=1)
    cfg_b = _cfg(min_trades_per_wallet=15, seed=2)
    df = make_resolved_trades(_wallet_rows("w1", n=30, price=0.55, n_won=22))
    pa = luck_filter(score_wallets(df, cfg_a), df, cfg_a).iloc[0]["p_value"]
    pb = luck_filter(score_wallets(df, cfg_b), df, cfg_b).iloc[0]["p_value"]
    assert pa != pb


# --------------------------------------------------------------------------- #
# R1: selection must hard-fail on a leaking row at cfg.train_end               #
# --------------------------------------------------------------------------- #
def test_score_wallets_raises_on_leakage_at_train_end(make_resolved_trades):
    # Arrange: last trade lands EXACTLY at the cutoff (>= boundary is leakage, R1).
    cfg = _cfg(min_trades_per_wallet=2)
    rows = _wallet_rows("leaky_w", n=4, price=0.4, n_won=2)
    rows[-1]["timestamp"] = TRAIN_END_TS
    df = make_resolved_trades(rows)

    # Act / Assert
    with pytest.raises(LeakageError):
        score_wallets(df, cfg)


def test_select_eligible_also_guards_leakage(make_resolved_trades):
    # The convenience wrapper must inherit the R1 guard.
    cfg = _cfg(min_trades_per_wallet=2)
    rows = _wallet_rows("leaky_w", n=4, price=0.4, n_won=2)
    rows[-1]["timestamp"] = TRAIN_END_TS + 100  # strictly past cutoff
    df = make_resolved_trades(rows)
    with pytest.raises(LeakageError):
        select_eligible(df, cfg)


def test_select_eligible_returns_wallets_and_table(make_resolved_trades):
    # Happy path: skilled wallet survives and appears in the eligible list.
    cfg = _cfg(min_trades_per_wallet=20)
    df = make_resolved_trades(_wallet_rows("skill_w", n=30, price=0.55, n_won=26))
    eligible, table = select_eligible(df, cfg)
    assert "skill_w" in eligible
    assert "skill_w" in set(table[WALLET])
