"""test_leakage.py — adversarial unit tests for polymirror.leakage (R1, the guard).

R1 is the most important rule: selection may use only training-window trades, and the
cutoff instant itself belongs to the test window (the boundary is >=, NOT >). These
tests pin that boundary explicitly because an off-by-one here silently leaks the future.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from polymirror.leakage import (
    LeakageError,
    assert_no_leakage,
    split_train_test,
    to_unix,
)
from polymirror.schema import TIMESTAMP

# A fixed UTC instant: 2026-01-01T00:00:00Z == 1767225600 unix seconds.
ISO_Z = "2026-01-01T00:00:00Z"
ISO_OFFSET = "2026-01-01T00:00:00+00:00"
EPOCH = 1767225600


# --------------------------------------------------------------------------- #
# to_unix — accepts int, ISO 'Z', ISO '+00:00', datetime                      #
# --------------------------------------------------------------------------- #
def test_to_unix_passes_through_int():
    assert to_unix(1700000000) == 1700000000


def test_to_unix_truncates_float_to_int_seconds():
    assert to_unix(1700000000.9) == 1700000000


def test_to_unix_parses_iso_z_suffix():
    assert to_unix(ISO_Z) == EPOCH


def test_to_unix_parses_iso_explicit_utc_offset():
    assert to_unix(ISO_OFFSET) == EPOCH


def test_to_unix_z_and_offset_agree():
    assert to_unix(ISO_Z) == to_unix(ISO_OFFSET)


def test_to_unix_treats_naive_datetime_as_utc():
    assert to_unix(datetime(2026, 1, 1)) == EPOCH


def test_to_unix_respects_aware_datetime():
    assert to_unix(datetime(2026, 1, 1, tzinfo=timezone.utc)) == EPOCH


def test_to_unix_rejects_bool_even_though_bool_is_int():
    with pytest.raises(TypeError):
        to_unix(True)


def test_to_unix_rejects_unsupported_type():
    with pytest.raises(TypeError):
        to_unix([1, 2, 3])


# --------------------------------------------------------------------------- #
# assert_no_leakage — strictly < cutoff passes; >= cutoff raises              #
# --------------------------------------------------------------------------- #
def test_assert_no_leakage_passes_strictly_below_cutoff():
    df = pd.DataFrame({TIMESTAMP: [100, 500, 999]})
    # Must not raise.
    assert_no_leakage(df, 1000)


def test_assert_no_leakage_raises_exactly_at_cutoff_boundary():
    # ts == cutoff is LEAKAGE (the >= boundary). This is the off-by-one that matters.
    df = pd.DataFrame({TIMESTAMP: [100, 1000]})
    with pytest.raises(LeakageError):
        assert_no_leakage(df, 1000)


def test_assert_no_leakage_raises_above_cutoff():
    df = pd.DataFrame({TIMESTAMP: [2000]})
    with pytest.raises(LeakageError):
        assert_no_leakage(df, 1000)


def test_assert_no_leakage_accepts_iso_cutoff():
    # cutoff given as ISO; one row exactly at the cutoff instant must leak.
    df = pd.DataFrame({TIMESTAMP: [EPOCH - 1, EPOCH]})
    with pytest.raises(LeakageError):
        assert_no_leakage(df, ISO_Z)


def test_assert_no_leakage_empty_frame_passes():
    df = pd.DataFrame({TIMESTAMP: []})
    assert_no_leakage(df, 1000)


def test_assert_no_leakage_requires_timestamp_column():
    df = pd.DataFrame({"not_timestamp": [1, 2, 3]})
    with pytest.raises(ValueError):
        assert_no_leakage(df, 1000)


def test_leakage_error_is_assertion_error_subclass():
    # Documented contract: LeakageError(AssertionError).
    assert issubclass(LeakageError, AssertionError)


# --------------------------------------------------------------------------- #
# split_train_test — partitions correctly; rejects overlap                    #
# --------------------------------------------------------------------------- #
def test_split_partitions_on_strict_and_inclusive_boundaries():
    df = pd.DataFrame({TIMESTAMP: [100, 999, 1000, 1500, 2000]})
    train, test = split_train_test(df, train_end=1000, test_start=1000)
    # train: timestamp < train_end (strict). test: timestamp >= test_start (inclusive).
    assert sorted(train[TIMESTAMP].tolist()) == [100, 999]
    assert sorted(test[TIMESTAMP].tolist()) == [1000, 1500, 2000]


def test_split_train_excludes_the_cutoff_row():
    df = pd.DataFrame({TIMESTAMP: [999, 1000]})
    train, _ = split_train_test(df, train_end=1000, test_start=1000)
    assert 1000 not in train[TIMESTAMP].tolist()
    assert 999 in train[TIMESTAMP].tolist()


def test_split_rejects_test_start_before_train_end():
    df = pd.DataFrame({TIMESTAMP: [100, 1000, 2000]})
    with pytest.raises(ValueError):
        split_train_test(df, train_end=1000, test_start=500)


def test_split_allows_gap_between_train_end_and_test_start():
    # test_start strictly AFTER train_end is fine (an embargo gap), and the rows
    # in the gap belong to neither partition.
    df = pd.DataFrame({TIMESTAMP: [500, 1000, 1500, 2000]})
    train, test = split_train_test(df, train_end=1000, test_start=1500)
    assert train[TIMESTAMP].tolist() == [500]
    assert sorted(test[TIMESTAMP].tolist()) == [1500, 2000]
    assert 1000 not in train[TIMESTAMP].tolist()  # the gap row is dropped from train
    assert 1000 not in test[TIMESTAMP].tolist()   # and from test


def test_split_returns_copies_not_views():
    # Mutating the returned frames must not corrupt the source (immutability, R8 spirit).
    df = pd.DataFrame({TIMESTAMP: [100, 2000]})
    train, test = split_train_test(df, train_end=1000, test_start=1000)
    train.loc[train.index[0], TIMESTAMP] = -1
    assert df[TIMESTAMP].tolist() == [100, 2000]


def test_split_accepts_iso_bounds():
    df = pd.DataFrame({TIMESTAMP: [EPOCH - 10, EPOCH, EPOCH + 10]})
    train, test = split_train_test(df, train_end=ISO_Z, test_start=ISO_Z)
    assert train[TIMESTAMP].tolist() == [EPOCH - 10]
    assert sorted(test[TIMESTAMP].tolist()) == [EPOCH, EPOCH + 10]
