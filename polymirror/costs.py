"""costs.py — the modelled spread cost (spec §5.2, rule R6).

THERE IS NO FREE HISTORICAL ORDER-BOOK DEPTH (§5.1). The spread is therefore a
MODELLED ASSUMPTION, never an observation, and is swept over three presets
(optimistic / base / conservative). A round trip pays a full spread: half on entry,
half on exit. apply_spread() is the single interface every cost in the project goes
through, so a future real-L2 dataset can be swapped in behind the same signature.

Spread model:
    full_spread_cents(p, vol) = clamp(
        base_cents * boundary_mult(p) * thinness_mult(vol),
        min_cents, max_cents )
    boundary_mult(p) = 1 + boundary_k * (1 - 4*p*(1-p))   # 1 at p=.5, 1+k at p∈{0,1}
    thinness_mult(vol) = thin_mult if (vol is not None and vol < thin_volume_usd) else 1
    half_spread = full_spread_cents / 2
"""
from __future__ import annotations

from typing import Optional

from config import SpreadPreset


def full_spread_cents(price: float, preset: SpreadPreset,
                      volume_usd: Optional[float] = None) -> float:
    """Modelled FULL round-trip spread in cents at this price level / liquidity."""
    p = float(price)
    boundary_mult = 1.0 + preset.boundary_k * (1.0 - 4.0 * p * (1.0 - p))
    thin_mult = 1.0
    if volume_usd is not None and volume_usd < preset.thin_volume_usd:
        thin_mult = preset.thin_mult
    cents = preset.base_cents * boundary_mult * thin_mult
    return float(min(max(cents, preset.min_cents), preset.max_cents))


def half_spread_frac(price: float, preset: SpreadPreset,
                     volume_usd: Optional[float] = None) -> float:
    """Half-spread as a price fraction (paid once on entry, once on exit)."""
    return full_spread_cents(price, preset, volume_usd) / 100.0 / 2.0


def apply_spread(mid_price: float, side: str, preset: SpreadPreset,
                 volume_usd: Optional[float] = None) -> float:
    """Executable taker price = mid moved AGAINST the trader by half the spread.

    BUY  -> pays UP   (executable = mid + half_spread)
    SELL -> receives LESS (executable = mid - half_spread)
    Result is clipped to [0, 1] (a probability can't leave the unit interval).
    """
    hs = half_spread_frac(mid_price, preset, volume_usd)
    if side == "BUY":
        px = mid_price + hs
    elif side == "SELL":
        px = mid_price - hs
    else:
        raise ValueError(f"unknown side {side!r} (expected BUY or SELL)")
    return float(min(max(px, 0.0), 1.0))


def round_trip_cost_frac(price: float, preset: SpreadPreset,
                         volume_usd: Optional[float] = None) -> float:
    """Total spread cost of a round trip as a fraction of price (entry half + exit half)."""
    return full_spread_cents(price, preset, volume_usd) / 100.0
