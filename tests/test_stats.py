"""test_stats.py — adversarial unit tests for polymirror.stats.

Every expected value is computed BY HAND in the test (or with the stdlib `math`
module), never by re-deriving it from the function under test. Tolerances are used
only for floating point noise and Monte-Carlo bootstrap, never to paper over a sign
or magnitude error.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from polymirror.stats import (
    benjamini_hochberg,
    bootstrap_mean_ci,
    bootstrap_two_sided_p,
    brier_score,
    log_score,
    make_rng,
)


# --------------------------------------------------------------------------- #
# Brier score — mean (p - y)^2                                                 #
# --------------------------------------------------------------------------- #
def test_brier_single_miss_equals_squared_error():
    # Arrange: bought a token at p=0.2 that lost (y=0)? No — y=1 means token won.
    # p=0.2, y=1 -> (0.2 - 1)^2 = 0.64
    # Act / Assert
    assert brier_score([0.2], [1]) == pytest.approx(0.64)


def test_brier_perfect_forecast_is_zero():
    assert brier_score([1.0, 0.0], [1, 0]) == pytest.approx(0.0)


def test_brier_worst_forecast_is_one():
    # Confidently wrong on every observation -> mean squared error 1.0
    assert brier_score([0.0, 1.0], [1, 0]) == pytest.approx(1.0)


def test_brier_coinflip_forecast_is_quarter():
    # p=0.5 everywhere -> (0.5)^2 = 0.25 regardless of outcomes
    assert brier_score([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0]) == pytest.approx(0.25)


def test_brier_averages_over_observations():
    # (0.64 + 0.0) / 2 = 0.32
    assert brier_score([0.2, 0.0], [1, 0]) == pytest.approx(0.32)


def test_brier_lower_is_better_for_calibrated_longshot_winner():
    confident = brier_score([0.9], [1])     # (0.1)^2 = 0.01
    timid = brier_score([0.6], [1])         # (0.4)^2 = 0.16
    assert confident < timid


# --------------------------------------------------------------------------- #
# Log score — mean negative log-likelihood, clipped                           #
# --------------------------------------------------------------------------- #
def test_log_score_coinflip_equals_ln2():
    assert log_score([0.5], [1]) == pytest.approx(-math.log(0.5))


def test_log_score_longshot_winner_matches_hand_value():
    # y=1, p=0.2 -> -ln(0.2)
    assert log_score([0.2], [1]) == pytest.approx(-math.log(0.2))


def test_log_score_symmetric_in_outcome_coding():
    # -ln(0.7) when y=1,p=0.7  ==  value when y=0,p=0.3
    assert log_score([0.7], [1]) == pytest.approx(log_score([0.3], [0]))


def test_log_score_clips_to_avoid_infinity_on_confident_correct():
    # p=1, y=1 would be -ln(1)=0 mathematically; clipping to 1-eps keeps it finite & tiny.
    eps = 1e-6
    val = log_score([1.0], [1], eps=eps)
    assert math.isfinite(val)
    assert val == pytest.approx(-math.log(1.0 - eps), abs=1e-9)


def test_log_score_clips_to_avoid_infinity_on_confident_wrong():
    # p=1, y=0 would be -ln(0)=+inf; clipping makes it a large-but-finite penalty.
    eps = 1e-6
    val = log_score([1.0], [0], eps=eps)
    assert math.isfinite(val)
    assert val == pytest.approx(-math.log(eps), abs=1e-6)


def test_log_score_averages_over_observations():
    expected = (-math.log(0.5) - math.log(0.5)) / 2.0
    assert log_score([0.5, 0.5], [1, 0]) == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# Benjamini-Hochberg FDR                                                       #
# --------------------------------------------------------------------------- #
def test_bh_known_case_only_smallest_rejected():
    # pvals [0.001, 0.04, 0.2, 0.9], alpha 0.05.
    # Sorted thresholds i/n*alpha = [0.0125, 0.025, 0.0375, 0.05].
    # 0.001 <= 0.0125 (yes); 0.04 <= 0.025 (no); 0.2,0.9 no.
    # Largest passing rank is 1 -> reject only the 0.001 hypothesis.
    rejected, crit = benjamini_hochberg([0.001, 0.04, 0.2, 0.9], alpha=0.05)
    assert list(rejected) == [True, False, False, False]
    assert crit == pytest.approx(0.001)


def test_bh_preserves_input_order_when_unsorted():
    # Same multiset, shuffled: only the 0.001 entry (now at index 2) is rejected.
    rejected, _ = benjamini_hochberg([0.9, 0.04, 0.001, 0.2], alpha=0.05)
    assert list(rejected) == [False, False, True, False]


def test_bh_step_up_rejects_all_below_a_later_passing_rank():
    # Classic step-up property: even though p_(2)=0.02 > 1/4*0.05=0.0125,
    # because a LATER rank passes, all hypotheses up to that rank are rejected.
    # thresholds for n=4: [0.0125, 0.025, 0.0375, 0.05]
    # p sorted [0.01, 0.02, 0.03, 0.04]; passes at ranks 1,2,3,4 -> all rejected.
    rejected, crit = benjamini_hochberg([0.01, 0.02, 0.03, 0.04], alpha=0.05)
    assert list(rejected) == [True, True, True, True]
    assert crit == pytest.approx(0.04)


def test_bh_rejects_none_when_all_pvalues_large():
    rejected, crit = benjamini_hochberg([0.9, 0.8, 0.7], alpha=0.05)
    assert not rejected.any()
    assert crit == 0.0


def test_bh_empty_input_returns_empty_mask():
    rejected, crit = benjamini_hochberg([], alpha=0.05)
    assert rejected.shape == (0,)
    assert crit == 0.0


def test_bh_is_no_more_lenient_than_bonferroni_on_single_pass():
    # With only the smallest passing, BH survivor count <= raw alpha survivor count.
    pvals = [0.001, 0.04, 0.2, 0.9]
    raw_survivors = sum(p < 0.05 for p in pvals)          # 0.001 and 0.04 -> 2
    bh_rejected, _ = benjamini_hochberg(pvals, alpha=0.05)
    assert int(bh_rejected.sum()) <= raw_survivors


# --------------------------------------------------------------------------- #
# Bootstrap mean CI                                                            #
# --------------------------------------------------------------------------- #
def test_bootstrap_mean_ci_point_equals_sample_mean():
    v = [1.0, 2.0, 3.0, 4.0, 5.0]
    mean, lo, hi = bootstrap_mean_ci(v, n_boot=2000, seed=42)
    assert mean == pytest.approx(3.0)


def test_bootstrap_mean_ci_brackets_the_sample_mean():
    v = [1.0, 2.0, 3.0, 4.0, 5.0]
    _, lo, hi = bootstrap_mean_ci(v, n_boot=2000, seed=42)
    assert lo <= 3.0 <= hi
    assert lo < hi


def test_bootstrap_mean_ci_is_deterministic_for_fixed_seed():
    v = [0.1, -0.2, 0.3, 0.05, -0.4, 0.7]
    a = bootstrap_mean_ci(v, n_boot=3000, seed=7)
    b = bootstrap_mean_ci(v, n_boot=3000, seed=7)
    assert a == b


def test_bootstrap_mean_ci_differs_across_seeds():
    v = [0.1, -0.2, 0.3, 0.05, -0.4, 0.7]
    a = bootstrap_mean_ci(v, n_boot=3000, seed=7)
    b = bootstrap_mean_ci(v, n_boot=3000, seed=8)
    # Point mean identical; the CI endpoints should move with the resampling seed.
    assert (a[1], a[2]) != (b[1], b[2])


def test_bootstrap_mean_ci_empty_is_nan():
    mean, lo, hi = bootstrap_mean_ci([], n_boot=100, seed=0)
    assert math.isnan(mean) and math.isnan(lo) and math.isnan(hi)


def test_bootstrap_mean_ci_degenerate_constant_collapses_to_point():
    mean, lo, hi = bootstrap_mean_ci([2.5, 2.5, 2.5], n_boot=500, seed=3)
    assert mean == pytest.approx(2.5)
    assert lo == pytest.approx(2.5)
    assert hi == pytest.approx(2.5)


# --------------------------------------------------------------------------- #
# Bootstrap two-sided p-value for H0: mean == 0                               #
# --------------------------------------------------------------------------- #
def test_bootstrap_p_near_one_for_data_centered_on_zero():
    v = [-2.0, -1.0, 0.0, 1.0, 2.0]
    p = bootstrap_two_sided_p(v, n_boot=5000, seed=1)
    assert p == pytest.approx(1.0, abs=1e-9)


def test_bootstrap_p_small_for_data_far_from_zero():
    v = [9.0, 10.0, 11.0, 12.0, 13.0]
    p = bootstrap_two_sided_p(v, n_boot=5000, seed=1)
    assert p < 0.05


def test_bootstrap_p_in_unit_interval():
    v = [0.3, -0.1, 0.2, 0.05, 0.4, -0.2]
    p = bootstrap_two_sided_p(v, n_boot=4000, seed=11)
    assert 0.0 <= p <= 1.0


def test_bootstrap_p_is_deterministic_for_fixed_seed():
    v = [0.3, -0.1, 0.2, 0.05, 0.4, -0.2]
    assert bootstrap_two_sided_p(v, n_boot=4000, seed=11) == \
        bootstrap_two_sided_p(v, n_boot=4000, seed=11)


def test_bootstrap_p_empty_is_nan():
    assert math.isnan(bootstrap_two_sided_p([], n_boot=100, seed=0))


def test_bootstrap_p_symmetric_under_sign_flip():
    # Negating every value must not change a two-sided p-value.
    v = np.array([0.4, 0.5, -0.1, 0.3, 0.2])
    p_pos = bootstrap_two_sided_p(v, n_boot=4000, seed=5)
    p_neg = bootstrap_two_sided_p(-v, n_boot=4000, seed=5)
    assert p_pos == pytest.approx(p_neg)


# --------------------------------------------------------------------------- #
# make_rng — seeded, reproducible Generator (R8)                              #
# --------------------------------------------------------------------------- #
def test_make_rng_returns_numpy_generator():
    assert isinstance(make_rng(0), np.random.Generator)


def test_make_rng_same_seed_same_stream():
    a = make_rng(123).integers(0, 1_000_000, size=10)
    b = make_rng(123).integers(0, 1_000_000, size=10)
    assert np.array_equal(a, b)


def test_make_rng_different_seed_different_stream():
    a = make_rng(123).integers(0, 1_000_000, size=10)
    b = make_rng(124).integers(0, 1_000_000, size=10)
    assert not np.array_equal(a, b)
