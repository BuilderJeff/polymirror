"""test_costs.py — adversarial unit tests for polymirror.costs (the modelled spread, R6).

The spread is a labelled assumption, not an observation. These tests pin the SHAPE
of that assumption (BUY pays up, SELL receives less; wider at the boundaries; thinness
multiplier; clamps; preset ordering) using the preset arithmetic, not the function
under test, to derive expectations.
"""
from __future__ import annotations

import pytest

from config import SPREAD_PRESETS
from polymirror.costs import (
    apply_spread,
    full_spread_cents,
    half_spread_frac,
    round_trip_cost_frac,
)

OPT = SPREAD_PRESETS["optimistic"]
BASE = SPREAD_PRESETS["base"]
CONS = SPREAD_PRESETS["conservative"]


# --------------------------------------------------------------------------- #
# Direction: BUY pays up, SELL receives less, mid in between                   #
# --------------------------------------------------------------------------- #
def test_buy_executes_above_mid_and_sell_below():
    mid = 0.5
    buy = apply_spread(mid, "BUY", BASE)
    sell = apply_spread(mid, "SELL", BASE)
    assert buy > mid > sell


def test_buy_and_sell_are_symmetric_around_mid():
    mid = 0.4
    buy = apply_spread(mid, "BUY", BASE)
    sell = apply_spread(mid, "SELL", BASE)
    # Both moved by the same half-spread in opposite directions.
    assert (buy - mid) == pytest.approx(mid - sell)


def test_apply_spread_move_equals_half_spread_frac():
    mid = 0.5
    hs = half_spread_frac(mid, BASE)
    assert apply_spread(mid, "BUY", BASE) - mid == pytest.approx(hs)


def test_apply_spread_rejects_unknown_side():
    with pytest.raises(ValueError):
        apply_spread(0.5, "HOLD", BASE)


# --------------------------------------------------------------------------- #
# Boundary widening: spread wider near 0/1 than at mid                         #
# --------------------------------------------------------------------------- #
def test_spread_wider_near_zero_than_at_mid():
    assert full_spread_cents(0.05, BASE) > full_spread_cents(0.50, BASE)


def test_spread_wider_near_one_than_at_mid():
    assert full_spread_cents(0.95, BASE) > full_spread_cents(0.50, BASE)


def test_spread_symmetric_about_mid_in_price():
    # boundary_mult depends on p only through 4*p*(1-p), symmetric about 0.5.
    assert full_spread_cents(0.2, BASE) == pytest.approx(full_spread_cents(0.8, BASE))


def test_spread_at_mid_equals_base_cents_for_liquid_contract():
    # boundary_mult = 1 at p=0.5; no thinness (vol=None); base_cents within clamp.
    assert full_spread_cents(0.5, BASE) == pytest.approx(BASE.base_cents)


# --------------------------------------------------------------------------- #
# Thinness multiplier: applies strictly BELOW the threshold                   #
# --------------------------------------------------------------------------- #
def test_thin_multiplier_applies_below_threshold():
    thin = full_spread_cents(0.5, BASE, volume_usd=BASE.thin_volume_usd - 1)
    liquid = full_spread_cents(0.5, BASE, volume_usd=BASE.thin_volume_usd + 1)
    assert thin > liquid
    # At mid with full clamp headroom the ratio is exactly thin_mult.
    assert thin == pytest.approx(liquid * BASE.thin_mult)


def test_thin_multiplier_not_applied_at_threshold_boundary():
    # Contract says vol < thin_volume_usd; equality must NOT trigger thinness.
    at_thresh = full_spread_cents(0.5, BASE, volume_usd=BASE.thin_volume_usd)
    liquid = full_spread_cents(0.5, BASE, volume_usd=BASE.thin_volume_usd + 1)
    assert at_thresh == pytest.approx(liquid)


def test_none_volume_means_no_thinness_penalty():
    assert full_spread_cents(0.5, BASE, volume_usd=None) == \
        pytest.approx(full_spread_cents(0.5, BASE, volume_usd=1e12))


# --------------------------------------------------------------------------- #
# Clamps to [min_cents, max_cents]                                            #
# --------------------------------------------------------------------------- #
def test_full_spread_never_below_min_cents():
    # Sweep prices and volumes; the modelled spread must respect the floor.
    for p in (0.01, 0.25, 0.5, 0.75, 0.99):
        for vol in (None, 1.0, 1e9):
            assert full_spread_cents(p, OPT, volume_usd=vol) >= OPT.min_cents - 1e-9


def test_full_spread_never_above_max_cents():
    # Conservative near the boundary with a thin contract maxes out the multipliers.
    val = full_spread_cents(0.01, CONS, volume_usd=1.0)
    assert val <= CONS.max_cents + 1e-9
    assert val == pytest.approx(CONS.max_cents)  # this combo saturates the ceiling


# --------------------------------------------------------------------------- #
# Executable price clipped to [0, 1]                                          #
# --------------------------------------------------------------------------- #
def test_buy_executable_clipped_at_one():
    # Near p=1 with a wide thin conservative spread, BUY would exceed 1 -> clip.
    px = apply_spread(0.99, "BUY", CONS, volume_usd=1.0)
    assert px == pytest.approx(1.0)
    assert px <= 1.0


def test_sell_executable_clipped_at_zero():
    px = apply_spread(0.01, "SELL", CONS, volume_usd=1.0)
    assert px == pytest.approx(0.0)
    assert px >= 0.0


def test_executable_price_always_in_unit_interval():
    for p in (0.0, 0.01, 0.5, 0.99, 1.0):
        for side in ("BUY", "SELL"):
            px = apply_spread(p, side, CONS, volume_usd=1.0)
            assert 0.0 <= px <= 1.0


# --------------------------------------------------------------------------- #
# Preset ordering: conservative > base > optimistic at the same price         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("price", [0.2, 0.5, 0.8])
def test_preset_cost_ordering_conservative_gt_base_gt_optimistic(price):
    o = full_spread_cents(price, OPT)
    b = full_spread_cents(price, BASE)
    c = full_spread_cents(price, CONS)
    assert o < b < c


def test_preset_ordering_holds_for_thin_contracts_too():
    o = full_spread_cents(0.5, OPT, volume_usd=1.0)
    b = full_spread_cents(0.5, BASE, volume_usd=1.0)
    c = full_spread_cents(0.5, CONS, volume_usd=1.0)
    assert o < b < c


# --------------------------------------------------------------------------- #
# Internal consistency of the helper family                                   #
# --------------------------------------------------------------------------- #
def test_half_spread_is_full_over_two_hundred():
    # half_spread_frac = full_spread_cents / 100 / 2 = full / 200
    assert half_spread_frac(0.5, BASE) == pytest.approx(full_spread_cents(0.5, BASE) / 200.0)


def test_round_trip_cost_is_full_spread_as_fraction():
    assert round_trip_cost_frac(0.5, BASE) == pytest.approx(full_spread_cents(0.5, BASE) / 100.0)


def test_round_trip_is_twice_the_half_spread():
    assert round_trip_cost_frac(0.3, CONS) == pytest.approx(2.0 * half_spread_frac(0.3, CONS))
