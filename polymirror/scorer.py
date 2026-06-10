"""scorer.py — CUSTOM COMPONENT #1: per-wallet accuracy scorer + luck filter.

Implements spec §6.1 (per-wallet proper scoring on ENTRY PRICES vs realized
outcomes, R2) and §7.1/§7.2 (the sign/Bernoulli-null bootstrap "luck filter"
that separates genuine forecasting skill from wallets that merely paid the
market's own price and got lucky).

Pipeline
--------
    score_wallets(train_trades)  -> per-wallet observed Brier/log score
    luck_filter(scores, train)   -> null-bootstrap p-value + BH-FDR eligibility
    select_eligible(train)       -> (eligible_wallet_list, full table)

The whole thing operates on TRAINING DATA ONLY. Selection that peeked at the
test window would be look-ahead bias, so when cfg.train_end is set we run
assert_no_leakage() FIRST (R1) — the guard is in code, not in good intentions.

Scoring uses BUY rows only (config default mirror_side='BUY'): a BUY of token k
at price p is an unambiguous opening long whose realized outcome y is `won`. A
SELL may be a close, not a fresh prediction, so it is excluded (schema.py).

The null model (§7.2)
---------------------
A wallet with NO skill beyond the market simply bets at the market-implied
probability p_i on each trade; the only thing that varies is which side of the
coin lands. We hold each wallet's entry prices p_i FIXED and redraw the
realized outcomes y* ~ Bernoulli(p_i), recomputing the SAME proper score
n_bootstrap times. That gives the score distribution of a "price-taker with no
edge". A wallet is eligible only if its observed score sits in the better
(lower) tail of its own null — i.e. it beats what its own prices would predict.

Reproducibility (R8): every wallet's null draw is seeded deterministically from
cfg.seed + a STABLE blake2b hash of the wallet string, via stats.make_rng. No
global RNG, no wall-clock. Same Config + same data => identical p-values.
"""
from __future__ import annotations

import hashlib

# Path bootstrap (R8 reproducibility / self-demo): when this module is run as a
# top-level script (`python polymirror/scorer.py`), Python puts only this file's
# directory on sys.path, so `import config` and `from polymirror ...` would fail.
# Prepend the project root in that case. No effect when imported as a package.
if __package__ in (None, ""):
    import sys
    from pathlib import Path

    _ROOT = Path(__file__).resolve().parent.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

import numpy as np
import pandas as pd

import config as _config
from polymirror import stats
from polymirror.leakage import assert_no_leakage
from polymirror.schema import (
    ENTRY_PROB,
    SIDE,
    WALLET,
    WON,
    validate_trades,
)

DEFAULT = _config.DEFAULT

# +1 Laplace smoothing on the empirical p-value: with B null draws the smallest
# attainable p-value is 1/(B+1) (never 0), which keeps BH-FDR well-defined and
# is the standard Monte-Carlo p-value estimator (Davison & Hinkley 1997).
_PVALUE_SMOOTHING = 1

_OUTPUT_COLUMNS = [
    WALLET,
    "n_trades",
    "brier",
    "logloss",
    "score",
    "p_value",
    "eligible_raw",
    "eligible_fdr",
    "null_mean",
    "null_margin",
]


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #
def _buy_rows(train_trades: pd.DataFrame) -> pd.DataFrame:
    """Resolved BUY rows only (default mirror_side). Validated at the boundary."""
    validate_trades(train_trades, resolved=True, allow_sell=True)
    buys = train_trades[train_trades[SIDE] == "BUY"]
    return buys


def _wallet_seed(wallet: str, base_seed: int) -> int:
    """Deterministic per-wallet seed: base_seed XOR stable 64-bit hash of wallet.

    blake2b (not Python's salted hash()) so the seed is identical across
    processes and runs (R8). Combined with XOR into a uint64 domain.
    """
    digest = hashlib.blake2b(str(wallet).encode("utf-8"), digest_size=8).digest()
    wallet_int = int.from_bytes(digest, byteorder="big", signed=False)
    return int((int(base_seed) ^ wallet_int) & 0xFFFFFFFFFFFFFFFF)


def _observed_score(p: np.ndarray, y: np.ndarray, cfg) -> tuple[float, float, float]:
    """(brier, logloss, score) for one wallet. score = the configured metric."""
    brier = stats.brier_score(p, y)
    logloss = stats.log_score(p, y, cfg.logloss_eps)
    score = brier if cfg.accuracy_metric == "brier" else logloss
    return brier, logloss, score


def _null_scores(p: np.ndarray, cfg, seed: int) -> np.ndarray:
    """Vectorized null score distribution for a single wallet (§7.2).

    Hold entry prices p FIXED; draw y* ~ Bernoulli(p) n_bootstrap times and
    recompute the SAME proper score per draw. Returns an array of length
    n_bootstrap. Lower = better, same orientation as the observed score.
    """
    rng = stats.make_rng(seed)
    n = p.shape[0]
    n_boot = int(cfg.n_bootstrap)
    # Shape (n_boot, n): independent Bernoulli(p_i) per column, per draw.
    u = rng.random((n_boot, n))
    y_star = (u < p[np.newaxis, :]).astype(np.float64)

    if cfg.accuracy_metric == "brier":
        return np.mean((p[np.newaxis, :] - y_star) ** 2, axis=1)

    # Log score: clip p exactly as stats.log_score does, then the per-draw
    # negative log-likelihood collapses to a per-column constant selected by y*.
    eps = cfg.logloss_eps
    p_clipped = np.clip(p, eps, 1.0 - eps)
    log_p = np.log(p_clipped)            # contribution when y* == 1
    log_1mp = np.log(1.0 - p_clipped)    # contribution when y* == 0
    ll = y_star * log_p[np.newaxis, :] + (1.0 - y_star) * log_1mp[np.newaxis, :]
    return -np.mean(ll, axis=1)


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #
def score_wallets(train_trades: pd.DataFrame, cfg=DEFAULT) -> pd.DataFrame:
    """Per-wallet proper score on entry prices vs realized outcomes (R2, §6.1).

    Returns one row per wallet with n_trades >= cfg.min_trades_per_wallet:
    columns [wallet, n_trades, brier, logloss, score]. `score` is brier or
    logloss per cfg.accuracy_metric; lower is better.

    When cfg.train_end is set, assert_no_leakage() runs FIRST (R1).
    """
    if cfg.train_end is not None:
        assert_no_leakage(train_trades, cfg.train_end)

    buys = _buy_rows(train_trades)
    min_trades = int(cfg.min_trades_per_wallet)

    rows: list[dict] = []
    for wallet, group in buys.groupby(WALLET, sort=True):
        n_trades = len(group)
        if n_trades < min_trades:
            continue
        p = group[ENTRY_PROB].to_numpy(dtype=float)
        y = group[WON].to_numpy(dtype=float)
        brier, logloss, score = _observed_score(p, y, cfg)
        rows.append(
            {
                WALLET: wallet,
                "n_trades": int(n_trades),
                "brier": brier,
                "logloss": logloss,
                "score": score,
            }
        )

    out = pd.DataFrame(rows, columns=[WALLET, "n_trades", "brier", "logloss", "score"])
    return out


def luck_filter(scores: pd.DataFrame, train_trades: pd.DataFrame, cfg=DEFAULT) -> pd.DataFrame:
    """Sign/Bernoulli-null bootstrap luck filter (§7.2).

    For each scored wallet, build its no-skill null score distribution
    (entry prices fixed, outcomes redrawn ~Bernoulli(p)) and compute the
    empirical one-sided p-value that the observed score is at least this good:

        p_value = (#{null_score <= observed_score} + 1) / (n_bootstrap + 1)

    The +1 numerator/denominator is Laplace smoothing (see _PVALUE_SMOOTHING):
    it bounds the p-value away from exactly 0 and is the standard Monte-Carlo
    estimator. Small p_value => the wallet beats its own prices more than luck.

    Augments `scores` with: p_value, eligible_raw (p_value < alpha),
    eligible_fdr (Benjamini-Hochberg across ALL tested wallets when cfg.fdr,
    else == eligible_raw), null_mean, null_margin (observed - null_mean;
    negative => better than the no-skill average). Sorted ascending by score.

    The BH critical value is recorded in the returned frame's .attrs.
    """
    if len(scores) == 0:
        empty = scores.copy()
        for col in ("p_value", "null_mean", "null_margin"):
            empty[col] = pd.Series(dtype=float)
        for col in ("eligible_raw", "eligible_fdr"):
            empty[col] = pd.Series(dtype=bool)
        empty.attrs["bh_crit"] = 0.0
        empty.attrs["n_tested"] = 0
        empty.attrs["n_eligible_raw"] = 0
        empty.attrs["n_eligible_fdr"] = 0
        return empty

    buys = _buy_rows(train_trades)
    # Pre-group once so each wallet's entry prices are fetched in O(1).
    prices_by_wallet = {
        wallet: group[ENTRY_PROB].to_numpy(dtype=float)
        for wallet, group in buys.groupby(WALLET, sort=False)
    }

    n_boot = int(cfg.n_bootstrap)
    base_seed = int(cfg.seed)

    p_values: list[float] = []
    null_means: list[float] = []
    null_margins: list[float] = []

    for _, row in scores.iterrows():
        wallet = row[WALLET]
        observed = float(row["score"])
        p = prices_by_wallet[wallet]

        seed = _wallet_seed(wallet, base_seed)
        null = _null_scores(p, cfg, seed)

        # One-sided: how often does no-skill match-or-beat the observed score?
        n_at_least_as_good = int(np.count_nonzero(null <= observed))
        p_value = (n_at_least_as_good + _PVALUE_SMOOTHING) / (n_boot + _PVALUE_SMOOTHING)

        null_mean = float(np.mean(null))
        p_values.append(float(p_value))
        null_means.append(null_mean)
        null_margins.append(observed - null_mean)

    out = scores.copy()
    out["p_value"] = p_values
    out["null_mean"] = null_means
    out["null_margin"] = null_margins
    out["eligible_raw"] = out["p_value"] < cfg.alpha

    if cfg.fdr:
        rejected, bh_crit = stats.benjamini_hochberg(out["p_value"].to_numpy(), cfg.alpha)
        out["eligible_fdr"] = rejected
    else:
        bh_crit = float("nan")
        out["eligible_fdr"] = out["eligible_raw"].to_numpy()

    out = out.sort_values("score", ascending=True, kind="mergesort").reset_index(drop=True)
    out = out[_OUTPUT_COLUMNS]

    out.attrs["bh_crit"] = float(bh_crit)
    out.attrs["fdr"] = bool(cfg.fdr)
    out.attrs["alpha"] = float(cfg.alpha)
    out.attrs["n_tested"] = int(len(out))
    out.attrs["n_eligible_raw"] = int(out["eligible_raw"].sum())
    out.attrs["n_eligible_fdr"] = int(out["eligible_fdr"].sum())
    return out


def select_eligible(train_trades: pd.DataFrame, cfg=DEFAULT) -> tuple[list[str], pd.DataFrame]:
    """score_wallets then luck_filter; return (eligible_wallets, full table).

    eligible_wallets = wallets with eligible_fdr when cfg.fdr else eligible_raw.
    The table carries raw vs FDR survivor counts in both columns and .attrs.
    """
    scores = score_wallets(train_trades, cfg)
    table = luck_filter(scores, train_trades, cfg)

    flag = "eligible_fdr" if cfg.fdr else "eligible_raw"
    if len(table) == 0:
        eligible_wallets: list[str] = []
    else:
        eligible_wallets = table.loc[table[flag], WALLET].tolist()
    return eligible_wallets, table


# --------------------------------------------------------------------------- #
# Self-demo on synthetic data (run with the venv python)                       #
# --------------------------------------------------------------------------- #
def _build_synthetic(cfg=DEFAULT) -> pd.DataFrame:
    """Synthetic resolved BUY trades: ~50 no-skill (noise) + a few skilled wallets.

    Noise wallets bet AT the true outcome probability (calibrated price-takers
    with zero edge): entry price == true win prob, outcome ~ Bernoulli(that
    prob). By construction their observed score matches the null, so they
    SHOULD mostly fail the luck filter.

    Skilled wallets consistently beat the price: they buy at a market price that
    is too pessimistic about their pick (price ~0.55) while the pick actually
    wins ~0.85 of the time, so their realized accuracy beats their entry prices.

    Everything is seeded off cfg.seed (R8): no global RNG, no wall-clock.
    """
    from polymirror.schema import (
        CONDITION_ID,
        OUTCOME_INDEX,
        PRICE,
        SIZE,
        TIMESTAMP,
        USDC_SIZE,
        WINNING_INDEX,
    )

    rng = stats.make_rng(cfg.seed)
    n_noise = 50
    n_skilled = 5
    n_trades = max(cfg.min_trades_per_wallet, 40)
    base_ts = 1_700_000_000  # fixed epoch, well before any plausible train_end

    records: list[dict] = []

    def emit(wallet: str, entry_prob: float, won: int, idx: int) -> None:
        # Encode as a BUY of OUTCOME_INDEX 0 at price=entry_prob; winning_index
        # is 0 iff the bought token won. (Binary market, BUY semantics.)
        outcome_index = 0
        winning_index = 0 if won == 1 else 1
        records.append(
            {
                WALLET: wallet,
                CONDITION_ID: f"0x{idx:064x}",
                TIMESTAMP: base_ts + idx,
                PRICE: float(entry_prob),
                SIDE: "BUY",
                OUTCOME_INDEX: outcome_index,
                SIZE: 100.0,
                USDC_SIZE: 100.0 * float(entry_prob),
                WINNING_INDEX: winning_index,
                WON: int(won),
                ENTRY_PROB: float(entry_prob),
            }
        )

    idx = 0
    # No-skill wallets: well-calibrated price-takers (price == true win prob).
    for w in range(n_noise):
        wallet = f"0xnoise{w:04d}"
        true_p = float(rng.uniform(0.30, 0.70))
        for _ in range(n_trades):
            won = int(rng.random() < true_p)
            emit(wallet, true_p, won, idx)
            idx += 1

    # Skilled wallets: buy cheap (~0.55) on picks that really win ~0.85.
    for w in range(n_skilled):
        wallet = f"0xskill{w:04d}"
        entry = 0.55
        true_win = 0.85
        for _ in range(n_trades):
            won = int(rng.random() < true_win)
            emit(wallet, entry, won, idx)
            idx += 1

    return pd.DataFrame.from_records(records)


def _self_demo() -> int:
    """Run select_eligible on synthetic data; print survivor counts; sanity-check."""
    cfg = DEFAULT  # train_end is None by default -> leakage guard is a no-op here
    trades = _build_synthetic(cfg)

    eligible, table = select_eligible(trades, cfg)

    n_tested = int(table.attrs.get("n_tested", len(table)))
    n_raw = int(table.attrs.get("n_eligible_raw", 0))
    n_fdr = int(table.attrs.get("n_eligible_fdr", 0))
    bh_crit = table.attrs.get("bh_crit", float("nan"))

    skilled_total = int(table[WALLET].str.startswith("0xskill").sum())
    skilled_survive = int(
        table.loc[table["eligible_fdr"], WALLET].str.startswith("0xskill").sum()
    )
    noise_total = int(table[WALLET].str.startswith("0xnoise").sum())
    noise_survive = int(
        table.loc[table["eligible_fdr"], WALLET].str.startswith("0xnoise").sum()
    )

    print("=" * 68)
    print("scorer.py self-demo (synthetic, seeded — R8)")
    print("=" * 68)
    print(f"metric                : {cfg.accuracy_metric}")
    print(f"min_trades_per_wallet : {cfg.min_trades_per_wallet}")
    print(f"n_bootstrap           : {cfg.n_bootstrap}")
    print(f"alpha                 : {cfg.alpha}  (fdr={cfg.fdr})")
    print(f"wallets tested        : {n_tested}")
    print(f"raw survivors         : {n_raw} / {n_tested}")
    print(f"FDR survivors         : {n_fdr} / {n_tested}   (BH crit p={bh_crit:.5g})")
    print(f"  skilled surviving   : {skilled_survive} / {skilled_total}")
    print(f"  noise   surviving   : {noise_survive} / {noise_total}")
    print(f"eligible wallets (FDR): {len(eligible)}")

    print("\ntop 8 by score (lower = better):")
    cols = [WALLET, "n_trades", "score", "null_mean", "null_margin", "p_value",
            "eligible_raw", "eligible_fdr"]
    with pd.option_context("display.width", 120, "display.max_columns", None):
        print(table[cols].head(8).to_string(index=False))

    # Sanity checks: skilled should mostly survive; MOST noise should NOT.
    ok = True
    if skilled_survive < skilled_total:
        print(f"\n[WARN] only {skilled_survive}/{skilled_total} skilled wallets survived FDR.")
    if noise_survive > noise_total // 2:
        print(f"\n[BROKEN] {noise_survive}/{noise_total} noise wallets survived FDR — "
              "filter is NOT separating luck from skill.")
        ok = False
    else:
        print(f"\n[OK] noise mostly rejected ({noise_survive}/{noise_total} survived); "
              f"skilled mostly retained ({skilled_survive}/{skilled_total}).")

    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(_self_demo())
