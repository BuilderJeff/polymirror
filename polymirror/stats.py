"""stats.py — shared, seeded statistical primitives (DRY core for scorer + simulator).

Pure numpy. Nothing here reads wall-clock or global RNG state; every stochastic
function takes an explicit seed so results are reproducible (R8).
"""
from __future__ import annotations

import numpy as np


# --- proper scores (spec §7.1) ----------------------------------------------
def brier_score(p, y) -> float:
    """Mean (p - y)^2. Lower is better. p = implied prob of the realized-coded outcome."""
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    return float(np.mean((p - y) ** 2))


def log_score(p, y, eps: float = 1e-6) -> float:
    """Mean negative log-likelihood. Lower is better. p clipped to [eps, 1-eps]."""
    p = np.clip(np.asarray(p, dtype=float), eps, 1.0 - eps)
    y = np.asarray(y, dtype=float)
    return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))


# --- multiple testing (spec §7.2) -------------------------------------------
def benjamini_hochberg(pvals, alpha: float = 0.05):
    """Benjamini-Hochberg FDR control.

    Returns (rejected_mask, crit_value) where rejected_mask[i] is True iff
    hypothesis i is rejected at FDR <= alpha. Order of the input is preserved.
    """
    p = np.asarray(pvals, dtype=float)
    n = p.size
    if n == 0:
        return np.zeros(0, dtype=bool), 0.0
    order = np.argsort(p, kind="mergesort")  # stable
    ranked = p[order]
    thresh = (np.arange(1, n + 1) / n) * alpha
    below = ranked <= thresh
    rejected = np.zeros(n, dtype=bool)
    if not below.any():
        return rejected, 0.0
    k = int(np.max(np.nonzero(below)[0])) + 1   # largest rank i with p_(i) <= i/n*alpha
    crit = float(ranked[k - 1])
    rejected[order[:k]] = True
    return rejected, crit


# --- bootstrap inference (spec §7.5) ----------------------------------------
def make_rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(int(seed))


def bootstrap_mean_ci(values, n_boot: int = 10_000, seed: int = 0, ci: float = 0.95):
    """(point_mean, lo, hi) percentile CI of the mean by resampling UNITS with replacement.

    Pass per-unit values already aggregated to the clustering level (e.g. per-wallet
    mean returns) so a single hyperactive wallet cannot dominate (spec §7.5).
    """
    v = np.asarray(values, dtype=float)
    if v.size == 0:
        nan = float("nan")
        return nan, nan, nan
    rng = make_rng(seed)
    idx = rng.integers(0, v.size, size=(n_boot, v.size))
    means = v[idx].mean(axis=1)
    lo = float(np.percentile(means, (1.0 - ci) / 2.0 * 100.0))
    hi = float(np.percentile(means, (1.0 + ci) / 2.0 * 100.0))
    return float(v.mean()), lo, hi


def bootstrap_two_sided_p(values, n_boot: int = 10_000, seed: int = 0) -> float:
    """Bootstrap two-sided p-value for H0: mean(values) == 0.

    p = 2 * min( P(boot_mean <= 0), P(boot_mean >= 0) ), clipped to [0,1].
    Used for the edge = strategy - benchmark test (paired per wallet).
    """
    v = np.asarray(values, dtype=float)
    if v.size == 0:
        return float("nan")
    rng = make_rng(seed)
    idx = rng.integers(0, v.size, size=(n_boot, v.size))
    means = v[idx].mean(axis=1)
    p_le = float(np.mean(means <= 0.0))
    p_ge = float(np.mean(means >= 0.0))
    return float(min(1.0, 2.0 * min(p_le, p_ge)))
