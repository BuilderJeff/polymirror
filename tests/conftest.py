"""conftest.py — make `import config` and `from polymirror...` resolve from the
project root regardless of where pytest is invoked.

Adversarial-test policy: this file does the MINIMUM plumbing (sys.path) and a few
shared fixtures. It contains no assertions about behavior — each test file owns its
own hand-computed expectations so a buggy fixture cannot mask a buggy implementation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# Project root = parent of this tests/ directory. Prepend so the in-repo
# `config.py` and `polymirror/` package win over anything else on the path.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(scope="session")
def root_path() -> Path:
    return _ROOT


@pytest.fixture
def make_resolved_trades():
    """Factory building a *resolved* trades DataFrame in the canonical schema.

    Each row is one BUY. Caller passes a list of dicts with at least
    wallet, condition_id, timestamp, price, outcome_index, winning_index.
    Missing optional columns (size, usdc_size, side, won, entry_prob) are filled
    with contract-consistent defaults so validate_trades(resolved=True) passes.
    """
    from polymirror.schema import (
        WALLET, CONDITION_ID, TIMESTAMP, PRICE, SIDE, OUTCOME_INDEX,
        SIZE, USDC_SIZE, WINNING_INDEX, WON, ENTRY_PROB, compute_won,
    )

    def _build(rows):
        records = []
        for i, r in enumerate(rows):
            oi = int(r["outcome_index"])
            wi = int(r["winning_index"])
            price = float(r["price"])
            side = r.get("side", "BUY")
            records.append({
                WALLET: r["wallet"],
                CONDITION_ID: r.get("condition_id", f"0x{i:064x}"),
                TIMESTAMP: int(r["timestamp"]),
                PRICE: price,
                SIDE: side,
                OUTCOME_INDEX: oi,
                SIZE: float(r.get("size", 100.0)),
                USDC_SIZE: float(r.get("usdc_size", price * 100.0)),
                WINNING_INDEX: wi,
                WON: int(r.get("won", compute_won(oi, wi))),
                ENTRY_PROB: float(r.get("entry_prob", price)),
            })
        return pd.DataFrame.from_records(records)

    return _build
