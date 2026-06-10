"""polymirror — research-grade backtest of Polymarket mirror-trading vs buy-the-favorite.

HARD RULES (see README §0):
  R0  Historical backtest ONLY. Never place a live trade. Never store a private key.
  R1  Strict temporal train/test split. assert_no_leakage() guards every selection step.
  R6  Spread is a labeled assumption, swept over optimistic/base/conservative presets.
"""

__version__ = "0.0.1"
