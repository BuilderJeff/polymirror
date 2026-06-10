# Chosen Market — Vertical Slice

> Stage: market finder (feeds the slice stage). Read-only data collection only (R0).
> Selected on 2026-06-02 from a Gamma universe of the 1,500 highest-volume **closed** markets.

## Selection

**Question:** Will the Toronto Raptors win the 2025 NBA Finals?
**Slug:** `will-the-toronto-raptors-win-the-2025-nba-finals`
**conditionId:** `0x37963e2b3194455fe768cd470571640c758c049b7cda6967449a43a8bd5c835f`

| Field | Value |
|---|---|
| outcomes | `["Yes", "No"]` |
| outcomePrices | `["0", "1"]` (settled: 0.00 / 1.00) |
| **winning_index** | **1** → outcome `"No"` (Raptors did not win) |
| enableOrderBook | `true` |
| binary (2 outcomes) | yes |
| umaResolutionStatus | `resolved` |
| endDate (nominal) | `2025-06-23T12:00:00Z` |
| closedTime (actual) | `2025-04-02 06:13:03+00` (early resolution — team eliminated) |
| negRisk | `true` (per-team slice of the "2025 NBA Finals winner" event) |
| volumeNum (USDC) | ~154,220,300 |
| clobTokenIds | `["86152833...214927", "23538341...222978"]` |

## Why this market

All five finalist candidates were settled binary order-book markets with thousands of
distinct wallets, so every hard requirement was trivially met. The Raptors market wins on
the qualities the downstream backtest actually needs:

1. **Most distinct wallets** of any candidate examined: **3,101 distinct `proxyWallet`s**
   in the observable trade set (requirement was "hundreds+"; this is ~10x over).
2. **Wide temporal span in the observable window:** the trade records span **22.2 days**
   (2025-03-09 → 2025-03-31). This matters for R1 (a clean, non-degenerate temporal
   train/test split). By contrast, the Fed/Iran candidates of similar dollar volume had
   their last 4,000 trades compressed into **under 1–4 days** of last-minute volume — a
   poor basis for a training window.
3. **Clean early settlement well before the CTF migration boundary** (~2026-04-28, spec §9).
   The entire observable trade window (Mar 2025) and resolution (Apr 2025) sit far from that
   boundary, so no continuity check is needed for the slice.
4. **Unambiguous resolution:** outcomePrices `["0","1"]` → winning_index = 1 ("No").

## Data API trade coverage (IMPORTANT constraint discovered)

The Data API `https://data-api.polymarket.com/trades?market={conditionId}&type=TRADE`
**caps offset at 3000** (`{"error":"max historical activity offset of 3000 exceeded"}`).
With `limit=1000` you can fetch offset 0/1000/2000/3000 → a **maximum of 4,000 trade
records per market**. Verified that `ascending=true` and `ascending=false` return the **same
4,000 records** (identical span and union), so this endpoint exposes a single 4,000-record
window, not a full history, for high-volume markets.

For this market that window is the complete observable set and is well-behaved:

| Metric | Value |
|---|---|
| Observed TRADE records (`type=TRADE`) | **4,000** |
| Distinct `proxyWallet`s | **3,101** |
| Time span | 22.2 days (2025-03-09 12:39 → 2025-03-31 18:31 UTC) |
| BUY-side share of sample | ~78% (3,102 / 4,000) |
| Trades on winning side ("No") | ~90.7% |

**Implication for the slice stage:** treat 4,000 as the per-market trade ceiling from this
endpoint. The wallet universe for this market is the 3,101 distinct traders in this window;
a train/test cut inside 2025-03-09 .. 2025-03-31 is feasible and non-degenerate. If fuller
per-wallet history is later required, it must come from per-wallet `/activity` or CLOB
`/prices-history`, not from deeper `/trades` offset paging (which is hard-capped at 3000).

## Candidate comparison (top recent settled binary order-book markets)

Distinct wallets / span measured over the (capped) 4,000-trade Data API window:

| slug | trades obs. | distinct wallets | obs. span | endDate |
|---|---|---|---|---|
| **will-the-toronto-raptors-win-the-2025-nba-finals** | 4,000 | **3,101** | **22.2 d** | 2025-06-23 |
| us-forces-enter-iran-by-april-30-899 | 4,000 | 2,595 | 0.9 d | 2026-04-30 |
| fed-decreases-...-50-bps-after-december-2025 | 4,000 | 2,542 | 0.5 d | 2025-12-10 |
| will-the-sacramento-kings-win-the-2025-nba-finals | 4,000 | 2,496 | 22.8 d | 2025-06-23 |
| fed-decreases-...-50-bps-after-january-2026 | 4,000 | 2,467 | 3.8 d | 2026-01-28 |

## Provenance / reproducibility

- Gamma universe: `GET /markets?closed=true&order=volumeNum&ascending=false`, paged
  (limit=100, offset 0..1400), cached at `data/cache/gamma_closed_markets.json`.
- Filtered to settled (one price 1.00, other 0.00) + exactly 2 outcomes + `enableOrderBook`,
  cached at `data/cache/settled_binary_candidates.json` (1,494 markets).
- Trade counts via Data API `/trades?...&type=TRADE`, cached at
  `data/cache/chosen_trades_newest.json` and `data/cache/chosen_metrics.json`.
- Authoritative chosen-market record: `data/cache/chosen_market_raw.json`.
