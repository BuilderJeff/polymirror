"""schema.py — the canonical in-memory trade schema (the contract every layer agrees on).

The ingestion layer (Phase 3) PRODUCES DataFrames in this shape; the scorer and
simulator (Phase 4) CONSUME them. Centralising the column names and the outcome
semantics here keeps the two custom components (scorer.py, simulator.py) honest and
prevents the single most dangerous bug in this project: a mis-joined outcome that
silently inverts returns.

OUTCOME SEMANTICS (read before touching scorer/simulator)
---------------------------------------------------------
Polymarket binary markets have two outcome tokens, outcomeIndex 0 and 1, with
parallel `outcomes`/`outcomePrices` arrays. After resolution the winner settles to
1.00 and the loser to 0.00, so the *winning outcomeIndex* is ground truth.

A trade's executed `price` is the market-implied probability of the token it touches.

We default to BUY-only for BOTH scoring and simulation (config.mirror_side):
  * A BUY of outcomeIndex k at price p is unambiguously OPENING a long directional
    bet on k. entry_prob = p; realized y (`won`) = 1 if winning_index == k else 0.
  * A SELL is ambiguous — it may OPEN a short on k, or it may CLOSE a prior BUY
    (profit-taking), which is not a fresh directional prediction. We cannot tell
    opens from closes without full position tracking, so SELLs are EXCLUDED by
    default (spec §9: "the strategy mirrors BUY entries ... handle side explicitly,
    document the choice"). `to_long_entry` exists for the optional, clearly-labelled
    case where a SELL is treated as an opening short; it is OFF by default.
"""
from __future__ import annotations

from typing import Iterable
import pandas as pd


# --- Column names (single source of truth for every DataFrame in the project) ---
WALLET        = "wallet"          # proxyWallet (the UNIT OF ANALYSIS, R7)
CONDITION_ID  = "condition_id"    # market id, 0x + 64 hex
TIMESTAMP     = "timestamp"       # unix seconds (int) — the leakage clock (R1)
PRICE         = "price"           # executed price of the touched token, in [0,1]
SIDE          = "side"            # "BUY" | "SELL"
OUTCOME_INDEX = "outcome_index"   # 0 | 1 — which token the trade touched
SIZE          = "size"            # shares
USDC_SIZE     = "usdc_size"       # notional in USDC
TITLE         = "title"           # human label (optional, for readability)
SLUG          = "slug"            # market slug (optional)

# Added by the resolution join (ingestion):
WINNING_INDEX = "winning_index"   # 0 | 1 — outcomeIndex that settled to 1.00
WON           = "won"             # 1 if the trade's bought token won, else 0 (BUY semantics)
ENTRY_PROB    = "entry_prob"      # implied prob of the bought token = price (BUY) ; 1-price (SELL-as-long)

RAW_COLUMNS = [WALLET, CONDITION_ID, TIMESTAMP, PRICE, SIDE, OUTCOME_INDEX, SIZE, USDC_SIZE]
RESOLVED_COLUMNS = RAW_COLUMNS + [WINNING_INDEX, WON, ENTRY_PROB]


def to_long_entry(outcome_index: int, price: float, side: str) -> tuple[int, float]:
    """Normalise a trade to an equivalent OPENING LONG: (bought_index, entry_prob).

    BUY of k @ p           -> (k,   p)
    SELL of k @ p (≡ short)-> (1-k, 1-p)    # only valid if the SELL opens a short

    SELL normalisation is OFF by default in the pipeline (see module docstring);
    this helper is provided for explicit, documented opt-in only.
    """
    if side == "BUY":
        return int(outcome_index), float(price)
    if side == "SELL":
        return 1 - int(outcome_index), 1.0 - float(price)
    raise ValueError(f"unknown side {side!r} (expected BUY or SELL)")


def compute_won(bought_index: int, winning_index: int) -> int:
    """1 if the bought token is the one that settled to 1.00, else 0."""
    return int(int(bought_index) == int(winning_index))


def validate_trades(df: pd.DataFrame, *, resolved: bool, allow_sell: bool = False) -> None:
    """Fail fast if a DataFrame violates the contract (input validation at boundary).

    Checks presence of required columns, value ranges (price∈[0,1], index∈{0,1},
    side∈{BUY[,SELL]}), and — when resolved — that `won`∈{0,1} and winning_index∈{0,1}
    with NO nulls in any critical column. A wrong/partial join must explode here.
    """
    required = RESOLVED_COLUMNS if resolved else RAW_COLUMNS
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"trades DataFrame missing columns: {missing}")

    if len(df) == 0:
        return  # empty is structurally valid

    crit = [WALLET, CONDITION_ID, TIMESTAMP, PRICE, SIDE, OUTCOME_INDEX]
    if resolved:
        crit += [WINNING_INDEX, WON]
    nulls = {c: int(df[c].isna().sum()) for c in crit if df[c].isna().any()}
    if nulls:
        raise ValueError(f"nulls in critical columns (silent-corruption risk): {nulls}")

    p = df[PRICE]
    if not ((p >= 0.0) & (p <= 1.0)).all():
        raise ValueError("PRICE outside [0,1] — not a valid implied probability.")

    allowed_sides = {"BUY", "SELL"} if allow_sell else {"BUY"}
    bad_sides = set(df[SIDE].unique()) - allowed_sides
    if bad_sides:
        raise ValueError(f"unexpected side(s) {bad_sides}; allow_sell={allow_sell}.")

    if not df[OUTCOME_INDEX].isin([0, 1]).all():
        raise ValueError("OUTCOME_INDEX must be 0 or 1 (binary markets only).")

    if resolved:
        if not df[WINNING_INDEX].isin([0, 1]).all():
            raise ValueError("WINNING_INDEX must be 0 or 1.")
        if not df[WON].isin([0, 1]).all():
            raise ValueError("WON must be 0 or 1.")


def require_columns(df: pd.DataFrame, cols: Iterable[str]) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"missing required columns: {missing}")
