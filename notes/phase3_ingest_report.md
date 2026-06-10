# Phase 3 — Ingest Viability Report
_Read-only data-viability gate for the mirror-trading backtest (`verify_viability.py`)._
- Source frame: `data/cache/master_trades.parquet`
## Schema
- `validate_trades(df, resolved=True, allow_sell=True)`: PASS
- Columns: `['wallet', 'condition_id', 'timestamp', 'price', 'side', 'outcome_index', 'size', 'usdc_size', 'title', 'slug', 'winning_index', 'won', 'entry_prob', 'resolution_ts']`
- `resolution_ts` column present: True

## Global shape
- Markets (distinct condition_id): **46**
- Trades: **183633**
- Distinct wallets: **81249**
- Timestamp span: 1735279443 .. 1766536107 (2024-12-27T06:04:03+00:00 .. 2025-12-24T00:28:27+00:00)

## Temporal split (config windows)
- train_end = 1756684800 (2025-09-01T00:00:00+00:00)
- test_start = 1756684800 (2025-09-01T00:00:00+00:00)
- Train trades (TIMESTAMP < train_end): **133518**
- Test trades (TIMESTAMP >= test_start): **50115**
- Markets resolved pre-cutoff (resolution_ts < train_end): **33** (the only ones usable for SCORING under strict-R1)

## Mirror-trading candidate wallets
A candidate wallet has >= `min_trades` BUY trades in the SCORING set (TIMESTAMP < train_end AND resolution_ts < train_end) AND >= 1 BUY trade in the TEST window (TIMESTAMP >= test_start).

- At configured `min_trades_per_wallet=30`: **17** candidate wallets

| min_trades | candidate wallets |
|-----------:|------------------:|
| 10 | 49 |
| 20 | 21 |
| 30 | 17 |
| 50 | 10 |

### Binding-constraint funnel
- Scoring-set BUY rows: 89181; test-window BUY rows: 34837 (across 16687 distinct test-BUY wallets)

| min_trades | scoring-qualified | also-test-BUY (candidate) | lost to test gate |
|-----------:|------------------:|--------------------------:|------------------:|
| 10 | 259 | 49 | 210 |
| 20 | 77 | 21 | 56 |
| 30 | 41 | 17 | 24 |
| 50 | 26 | 10 | 16 |

The binding constraint is the **test-window overlap**, not the scoring threshold: many wallets clear the BUY-count bar in the scoring set but never place a BUY in the test window, so they cannot be mirrored. Loosening `min_trades` alone does not fix this — at min=10, 259 wallets qualify on scoring but only 49 also trade in the test window.

## Verdict
- **viable = False** (rule: n_candidate_wallets >= 30)

### Warnings
- NOT VIABLE: only 17 candidate wallets (need >= 30) at min_trades_per_wallet=30. Threshold sweep: {10: 49, 20: 21, 30: 17, 50: 10}. BINDING CONSTRAINT is the test-window overlap, not the scoring threshold: 41 wallets clear >=30 BUYs in the scoring set but only 17 of them place any BUY in the test window (24 lost to the test gate). Remedies: (a) INGEST MORE RESOLVED MARKETS — only 46 markets / 33 pre-cutoff are in the frame, so the wallet overlap across the split is thin; widening the universe is the highest-leverage fix. (b) Loosen min_trades_per_wallet (sweep: 49 at 10, 21 at 20) — helps only modestly because the test gate dominates. (c) Adjust the split (e.g. a shorter scoring window / later cutoff) so more of the same wallets are active on both sides.
