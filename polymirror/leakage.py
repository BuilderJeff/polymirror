"""leakage.py — the look-ahead-bias guard (rule R1, the most important rule).

assert_no_leakage() HARD-FAILS if any row used for wallet selection carries a
timestamp at or after the training cutoff. It is called at every selection step in
scorer.py. The guard is in code, not in good intentions (spec R1).

Boundary convention: a row with timestamp == cutoff is LEAKAGE (>=), because the
training window is strictly [.., cutoff) and the cutoff instant belongs to test.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Union

import pandas as pd

from polymirror.schema import TIMESTAMP

TimeLike = Union[int, float, str, datetime]


class LeakageError(AssertionError):
    """Raised when selection data peeks at or past the training cutoff (R1 violation)."""


def to_unix(ts: TimeLike) -> int:
    """Coerce int/float epoch seconds, ISO-8601 string, or datetime to unix seconds (UTC)."""
    if isinstance(ts, bool):  # guard: bool is an int subclass
        raise TypeError("timestamp must not be a bool")
    if isinstance(ts, (int, float)):
        return int(ts)
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    if isinstance(ts, str):
        s = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    raise TypeError(f"unsupported timestamp type: {type(ts)!r}")


def assert_no_leakage(df: pd.DataFrame, cutoff_ts: TimeLike) -> None:
    """Hard-fail if ANY row has timestamp >= cutoff_ts. Call at every selection step."""
    if TIMESTAMP not in df.columns:
        raise ValueError(f"DataFrame has no {TIMESTAMP!r} column to check for leakage.")
    cutoff = to_unix(cutoff_ts)
    leaked = df[df[TIMESTAMP] >= cutoff]
    if len(leaked) > 0:
        first = int(leaked[TIMESTAMP].min())
        raise LeakageError(
            f"R1 LEAKAGE: {len(leaked)} of {len(df)} selection rows have "
            f"timestamp >= cutoff {cutoff} (earliest offending ts={first})."
        )


def split_train_test(df: pd.DataFrame, train_end: TimeLike, test_start: TimeLike):
    """Return (train, test): train timestamp < train_end; test timestamp >= test_start.

    Enforces the strict temporal split (R1): test_start must be >= train_end.
    Returns copies so callers cannot mutate the source frame (immutability).
    """
    te = to_unix(train_end)
    ts = to_unix(test_start)
    if ts < te:
        raise ValueError(f"R1: test_start ({ts}) < train_end ({te}) — windows overlap.")
    train = df[df[TIMESTAMP] < te].copy()
    test = df[df[TIMESTAMP] >= ts].copy()
    return train, test
