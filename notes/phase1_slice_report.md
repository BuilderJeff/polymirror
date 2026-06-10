# Phase-1 Vertical Slice — Report

> Stage: end-to-end slice (client → pull → resolve → join → verify → parquet).
> Read-only historical backtest only (R0). Ran clean on 2026-06-02 with
> `./.venv/Scripts/python.exe phase1_slice.py`.

## Market

| Field | Value |
|---|---|
| question | Will the Toronto Raptors win the 2025 NBA Finals? |
| conditionId | `0x37963e2b3194455fe768cd470571640c758c049b7cda6967449a43a8bd5c835f` |
| outcomes | `["Yes", "No"]` |
| outcomePrices (settled) | `[0.0, 1.0]` |
| **winning_index (DERIVED)** | **1** → label `"No"` (cross-checks the market-finder's 1) |

The winning index is **derived** from the Gamma settled `outcomePrices` (index of the
`1.0`), not hard-coded. The market-finder's value (1) is used only as a cross-check assert.

## Verification results

| Metric | Value |
|---|---|
| total TRADE records | **4000** |
| distinct proxyWallets | **3101** |
| null counts (price/timestamp/side/outcome_index) | `{price:0, timestamp:0, side:0, outcome_index:0}` |
| all rows have price/ts/side/idx | **True** |
| resolution-join coverage | **100.0000%** (4000/4000) — ASSERTED |
| winning outcomeIndex | **1** (label `"No"`) |
| side breakdown | BUY 3102 / SELL 898 |
| won breakdown | won=1: 3626 / won=0: 374 |

The 4000/3101 figures match `notes/chosen_market.md` exactly.

### Pagination note (why page size = 1000 matters)

The Data API offset is hard-capped at 3000 **regardless of page size**, so a larger page
reaches strictly more records. Paging at `limit=1000` reaches the full ~4000-record
observable window (offsets 0/1000/2000/3000); paging at the smaller `config.http_page_limit`
(500) would stop at ~3500. `get_trades` therefore defaults the page size to the API max
(1000), not `config.http_page_limit`. Verified: the 4 offset pages return 4000 trades with
**4000 distinct transactionHashes and 4000 distinct full records (zero duplicates)**.

## 5 sample joined rows

| wallet | price | timestamp | side | outcomeIndex | label | won |
|---|---|---|---|---|---|---|
| `0x0e4f6c8e6e1b72b2eb1b56b39513379e81f8a805` | 0.9990 | 1743445893 | SELL | 1 | No | 1 |
| `0x6a4eadde31147fa851ab9d52f0bfac669e18d91b` | 0.0010 | 1743439739 | BUY | 0 | Yes | 0 |
| `0x01e35ddbcf375c5a6a6d61e8bcad25b19d9c5212` | 0.9990 | 1743431131 | SELL | 1 | No | 1 |
| `0x091e03f42af56624dffbcaf4c49acef9c4e13048` | 0.0010 | 1743429103 | BUY | 0 | Yes | 0 |
| `0x5c89a4e4d95ac2c841caa7c1d5f869ac856b6220` | 0.9990 | 1743414187 | SELL | 1 | No | 1 |

`won = 1` iff the trade's bought `outcomeIndex == winning_index (1)`. (Verified globally:
`won == (outcome_index == 1)` for all 4000 rows.)

## Mean entry price: winning vs losing side (reported, NOT asserted)

| Side (by outcomeIndex) | mean entry price | n |
|---|---|---|
| WINNING side (idx=1, "No") | **0.9988** | 3626 |
| LOSING side (idx=0, "Yes") | **0.0011** | 374 |

The winning side's mean entry price (0.9988) is far higher than the losing side's (0.0011),
as expected when prices are informative: by the observable window (Mar 2025) the Raptors'
elimination was near-certain, so "No" traded near 1.00 and "Yes" near 0.00. This is reported
for sanity, not asserted (a defensible result does not depend on it).

> Caveat: this slice attaches the realized winner to **every** row regardless of side. The
> downstream mirror strategy restricts to BUY entries (see `polymirror/schema.py` outcome
> semantics); SELL rows are kept here only so the slice exercises the full join.

## Artifacts

- `data/cache/phase1_trades.parquet` — 4000 rows × 15 cols (resolved schema + labels).
- `data/cache/phase1_summary.json` — machine-readable summary of the above.
- `data/cache/polyapi/*.json` — on-disk request cache (re-runs hit cache, no re-pull;
  verified 4000-record re-pull in 0.018 s with zero network calls).

## Files written this stage

- `polymirror/polyapi.py` — Gamma+Data+CLOB read-only client (pagination, backoff, cache).
- `phase1_slice.py` — the end-to-end slice runner.
- `notes/phase1_slice_report.md` — this report.
