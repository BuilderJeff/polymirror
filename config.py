"""config.py — SINGLE SOURCE OF TRUTH for every backtest parameter (spec §7.7).

Reproducibility (R8): same Config + same cached data => identical numbers.
Every stochastic step takes `seed` explicitly; nothing reads wall-clock.

Nothing in here touches the network or the filesystem. It is pure parameters.
Spread preset cent-values are FIRST-PASS defaults; they are calibrated against a
sample of *current* CLOB /spread reads in Phase 1 (see notes/spread_calibration.md)
and the chosen numbers are reported alongside every result (R6).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal, Optional


# --------------------------------------------------------------------------- #
# Spread model (spec §5.2)                                                     #
# --------------------------------------------------------------------------- #
# There is NO free historical order-book depth (spec §5.1), so the spread is a
# MODELLED ASSUMPTION, never an observation. A round trip pays a full spread:
# half on entry, half on exit. Spread widens near the 0/1 boundaries and on thin
# contracts. We parametrise it as:
#
#     half_spread(price, vol) = 0.5 * base_cents/100
#                                   * boundary_mult(price)
#                                   * thinness_mult(vol)
#
# where boundary_mult inflates the spread as price -> 0 or 1, and thinness_mult
# inflates it when the contract's in-window traded volume is below a threshold.
# The executable price is the mid moved AGAINST the trader by half_spread.

@dataclass(frozen=True)
class SpreadPreset:
    name: Literal["optimistic", "base", "conservative"]
    # Full spread in cents at mid-price (0.50) for a liquid contract.
    base_cents: float
    # Boundary widening: spread is multiplied by (1 + boundary_k * (1 - 4*p*(1-p))).
    # 4*p*(1-p) is 1 at p=0.5 and 0 at p∈{0,1}; so the factor is 1 at mid and
    # (1 + boundary_k) at the extremes. boundary_k=0 disables boundary widening.
    boundary_k: float
    # Thinness widening: if a contract's in-window USDC volume < thin_volume_usd,
    # multiply spread by thin_mult. Set thin_mult=1.0 to disable.
    thin_volume_usd: float
    thin_mult: float
    # Hard floor/ceiling on full spread (cents) after all multipliers.
    min_cents: float
    max_cents: float


# CALIBRATED in Phase 1 (notes/spread_calibration.md) against 24 outcome tokens across
# 18 live markets, walking the CLOB book at $100/$1k/$5k order sizes. Each preset maps to
# an execution-quality scenario: optimistic = small order on a liquid book; base = a
# realistic $1-5k order; conservative ≈ 3x base stress. base_cents is the FULL spread at
# mid (p=0.5) for a liquid contract; presets are reported alongside every result (R6).
SPREAD_PRESETS: dict[str, SpreadPreset] = {
    # Small order on a liquid book. A result that survives ONLY here is not a result (R6).
    "optimistic": SpreadPreset(
        name="optimistic", base_cents=1.0, boundary_k=0.5,
        thin_volume_usd=2_000, thin_mult=1.5, min_cents=0.2, max_cents=6.0,
    ),
    # Realistic $1-5k order on a typical contract.
    "base": SpreadPreset(
        name="base", base_cents=2.5, boundary_k=1.0,
        thin_volume_usd=25_000, thin_mult=2.5, min_cents=0.5, max_cents=12.0,
    ),
    # Stress test: ~3x base; ceiling exceeds the worst observed effective spread (~19¢).
    "conservative": SpreadPreset(
        name="conservative", base_cents=5.0, boundary_k=1.5,
        thin_volume_usd=50_000, thin_mult=3.0, min_cents=1.0, max_cents=25.0,
    ),
}


# --------------------------------------------------------------------------- #
# Fees (spec: cloned engine handles settlement/fees; Polymarket spot fee = 0). #
# --------------------------------------------------------------------------- #
# Polymarket charges no per-trade taker fee on the CLOB at time of writing;
# the dominant cost is the spread. Kept as a parameter so it is auditable and
# swappable if the fee schedule changes.
TAKER_FEE_BPS: float = 0.0


# --------------------------------------------------------------------------- #
# Master config (spec §7.7)                                                    #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Config:
    # --- Temporal split (R1). STRICT: test_start must be >= train_end. -------
    # ISO-8601 UTC. Scoped to 2025 (Phase 1 confirmed 2025 resolved markets pull
    # cleanly), which sidesteps the CTF Exchange V2 migration boundary (~2026-04-28,
    # §9) entirely — no continuity check needed. Clean contiguous calendar split.
    # NOTE (R1 strengthening, enforced in run.py): a wallet is scored ONLY on
    # training trades whose MARKET ALSO resolved before train_end, so selection never
    # uses an outcome that was not yet known at the cutoff. The master frame carries a
    # `resolution_ts` column for this filter.
    train_start: Optional[str] = "2025-01-01T00:00:00Z"
    train_end:   Optional[str] = "2025-09-01T00:00:00Z"   # cutoff_ts for assert_no_leakage()
    test_start:  Optional[str] = "2025-09-01T00:00:00Z"   # MUST be >= train_end
    test_end:    Optional[str] = "2026-01-01T00:00:00Z"

    # --- Wallet selection (R2, R3) ------------------------------------------
    min_trades_per_wallet: int = 30          # swept in Phase 5
    accuracy_metric: Literal["brier", "log"] = "brier"
    n_bootstrap: int = 10_000
    alpha: float = 0.05
    fdr: bool = True                          # Benjamini-Hochberg across wallets
    logloss_eps: float = 1e-6                 # clip p to [eps, 1-eps]

    # --- Mirror / benchmark simulation (R4, R5) -----------------------------
    # Holding horizons (hours) for the edge-decay curve. N=0 is the entry mark
    # (instantaneous round-trip = the pure cost floor). Capped at 48h: the live
    # forward collector records each captured entry for at most 48h, so no horizon
    # may exceed the maximum recorded window.
    N_hours: tuple[int, ...] = (0, 1, 6, 24, 48)
    favorite_threshold: float = 0.50          # benchmark buys side with p > this AT ENTRY
    mirror_side: Literal["BUY"] = "BUY"        # strategy mirrors BUY entries only (§9)

    # --- Cost model (R6) -----------------------------------------------------
    spread_preset: Literal["optimistic", "base", "conservative"] = "base"
    taker_fee_bps: float = TAKER_FEE_BPS

    # --- Market & wallet universe (§7.7) ------------------------------------
    # How resolved markets are selected for the backtest.
    universe_min_volume_usd: float = 50_000
    universe_require_order_book: bool = True   # enableOrderBook == True only
    universe_binary_only: bool = True          # exactly 2 outcomes (Yes/No-style)
    # Universe scan depth. The Gamma lister walks markets by volume DESC; the first
    # ingest at page_budget=60 reached only mega-markets ($57M-$400M) — too few (46)
    # and too retail-heavy for cross-window wallet overlap. Scanning deeper pulls in
    # the many mid-size ($50k-$50M) 2025 markets with concentrated repeat traders.
    universe_max_markets: int = 600            # ~13x the first run; ~2000+ qualify, plenty for overlap
    universe_page_budget: int = 200            # Gamma pages are 100 each; 200 pages covers the offset cap
    # How the candidate wallet set is seeded: every wallet that TRADEd a universe
    # market in the training window (default), optionally narrowed to /holders.
    wallet_universe_seed: Literal["all_traders", "holders"] = "all_traders"

    # --- Reproducibility (R8) -----------------------------------------------
    seed: int = 20260602

    # --- HTTP client behaviour (caching/backoff; §9 rate limits) ------------
    http_max_retries: int = 5
    http_backoff_base_s: float = 0.75
    http_page_limit: int = 500                # API max page size for pagination

    def preset(self) -> SpreadPreset:
        return SPREAD_PRESETS[self.spread_preset]

    def with_(self, **overrides) -> "Config":
        """Return a new Config with overrides applied (immutable update, R8)."""
        return replace(self, **overrides)

    def validate(self) -> None:
        """Fail fast on incoherent parameters (input validation at boundary)."""
        if self.test_start is not None and self.train_end is not None:
            if self.test_start < self.train_end:
                raise ValueError(
                    f"Temporal leakage risk: test_start {self.test_start} < "
                    f"train_end {self.train_end} (violates R1)."
                )
        if not (0.0 < self.favorite_threshold < 1.0):
            raise ValueError("favorite_threshold must be in (0,1).")
        if self.min_trades_per_wallet < 1:
            raise ValueError("min_trades_per_wallet must be >= 1.")
        if self.spread_preset not in SPREAD_PRESETS:
            raise ValueError(f"unknown spread_preset {self.spread_preset!r}.")
        if self.alpha <= 0 or self.alpha >= 1:
            raise ValueError("alpha must be in (0,1).")


# The default config used unless a caller overrides it.
DEFAULT = Config()


if __name__ == "__main__":
    import json
    from dataclasses import asdict
    DEFAULT.validate()
    print("Config OK. Default parameters:")
    print(json.dumps(asdict(DEFAULT), indent=2, default=str))
    print("\nActive spread preset:", DEFAULT.preset())
