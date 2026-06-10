# V1/V2 Migration Investigation (CTF Exchange V2, ~2026-04-28)

**Status:** complete. **Date run:** 2026-06-02 (today). **R0 compliant:** read-only;
no keys, no wallet, no live trades. Probe code: `polymirror/v1v2_probe.py`. Raw API
responses cached under `data/cache/` (`trades_*`, `phist_*`, `gmarket_*`).

## Question

Spec §9 gotcha: around **2026-04-28** Polymarket migrated to CTF Exchange V2 (new
contracts, pUSD collateral, deprecated subgraphs). Does the **Data API `/trades`**
return trades continuously on BOTH sides of that boundary? Pull weekly trade counts
across ~2026-03 .. 2026-06 and look for a discontinuity/cliff at the boundary.

## Headline finding

**The premise of the task cannot be executed as literally stated, and that itself is
the key result.** The Data API `/trades` endpoint **cannot be windowed to a historical
week**. Empirically:

1. `/trades` **ignores every time-window parameter** tried — `from/to`, `start/end`,
   `startTs/endTs`, `start_ts/end_ts`, `after/before`, `maxTimestamp`, `endTime`,
   `before`. All return the same most-recent trades. There is no server-side time
   filter.
2. `/trades` pagination is **hard-capped at offset 3000** (`offset > 3000` → HTTP 400
   `"max historical activity offset of 3000 exceeded"`). Same cap applies platform-wide
   (offset 5000+ → 400) and per-market.
3. Consequence: for any high-volume market, `/trades` only returns the **most recent
   ~3000–3500 trades**, which for these markets spans roughly **one day to one week**.
   It is structurally impossible to walk `/trades` back to March 2026.

So a literal "weekly `/trades` count, March→June" is **not obtainable** from this
endpoint. A weekly trade-count series must come from a windowable source.

`/trades` **does** return pre-migration (V1-era) trades — it is **not** empty before the
boundary. A market that resolved **2026-03-31** ("Netanyahu out by March 31") returns
trades dated 2026-03-29 .. 2026-04-05; one that resolved **2026-04-07** returns trades
dated 2026-04-09 .. 2026-04-11. The limitation is reach (most-recent-3000), not a V1/V2
content gap. There is therefore **no evidence of a /trades content cliff at 2026-04-28**;
the apparent "cliff" people might see is just the offset cap truncating history.

## Usable continuity series: CLOB `/prices-history`

`/prices-history` **does** accept time windows (`interval=max` + `fidelity`). Each price
point exists only where there was book activity, so point density is a defensible
activity proxy. Three high-volume markets whose lifetimes straddle 2026-04-28:

| Market (start) | first daily pt | last daily pt | daily pts before mig | after mig | max gap (d) | gap starts | spans boundary |
|---|---|---|---|---|---|---|---|
| Iranian regime fall by Jun 30 (2025-12-17) | 2025-12-18 | 2026-06-02 | 37 | 35 | 11.0 | 2026-03-29 | yes |
| Uzbekistan win 2026 WC (2025-07-02) | 2025-09-18 | 2026-06-02 | 31 | 35 | 13.0 | 2026-04-09 | yes |
| Jesus return before 2027 (2025-11-25) | 2025-11-26 | 2026-06-02 | 49 | 35 | 6.0 | 2026-04-16 | yes |

### Weekly activity (daily-fidelity points per ISO week, cap 7 = one/day)

```
week    W09 W10 W11 W12 W13 W14 W15 W16 W17 W18 W19 W20 W21 W22 W23
Iran     1   7   7   7   7   -   1   1   5   7   7   7   7   7   1
Uzbek    -   7   5   5   7   -   1   -   5   7   7   7   7   7   1
Jesus    1   7   6   7   7   6   6   3   5   7   7   7   7   7   1
                            ^^^^^^^^^^^  <- thinning around migration
migration 2026-04-28 falls in W18; W14≈Mar30, W15≈Apr6, W16≈Apr13, W17≈Apr20
```

**Interpretation (daily series):** continuous across the boundary — every market has
points before AND after — but with a **soft dip W14–W16** (~2026-03-30 .. 2026-04-19),
i.e. the ~2–4 weeks leading into the migration, with max single gaps of 11–13 days
starting late March / early April. Full weekly density resumes by W17–W18 (late
April / early May). This is **thinning, not a hard break**, on the daily series.

### Hard cliff on FINE-grained data (decisive)

At **hourly fidelity (`fidelity=60`)**, history starts at **exactly `2026-05-02 05:00`
for all three markets, identically** (743 points ≈ a rolling ~31-day window ending
today):

```
hourly points/week:  W18=43  W19=168  W20=168  W21=168  W22=167  W23=24   (nothing before W18)
fine_start = 2026-05-02 05:00  (identical across every market tested)
```

Fine-grained book/price data **does not exist before 2026-05-02** for these long-lived
markets — a uniform ~31-day window ending today.

**Disambiguation test (decisive):** a market that *started after* the migration —
"MicroStrategy sells any Bitcoin" (start **2026-05-05**) — has hourly data from
**2026-05-06 01:00**, i.e. from *its own creation*, **not** pinned to 2026-05-02.
Therefore the hourly wall is a **rolling ~30-day fine-fidelity retention window**, NOT a
migration artifact. The 2026-05-02 start on the spanning markets is just "today minus
~30 days." **This is NOT a V1/V2 cliff.** The honest conclusion: there is **no observed
discontinuity attributable to the migration** in either `/trades` content or price
history — the only real cliff is the generic offset-3000 / 30-day-fine-retention limits,
which apply equally on both sides of 2026-04-28. The operational consequence still holds
for the backtest: **sub-daily execution prices are only available for roughly the last 30
days regardless of the migration**, so any intraday-price logic is time-limited by
retention, independent of V1/V2.

## What this means for the backtest window

The backtest needs, per trade: entry price, side, size, timestamp (from `/trades` or a
windowable source) and a resolution outcome (Gamma). The constraints:

- **`/trades` cannot reconstruct a deep historical tape** (offset-3000 cap, no time
  filter). Any window relying on walking `/trades` back in time is infeasible regardless
  of which side of the migration it is on. Per-trade history must instead be assembled
  from per-wallet `/activity` (also offset-capped) or accepted as "most-recent-N only".
- **Daily price history is continuous** across 2026-04-28 and usable for both sides.
- **Fine (hourly) price history exists only for ~the last 30 days** (today: from
  2026-05-02), but this is a **rolling retention window, not a migration cliff** (a
  market created 2026-05-05 has hourly data from 2026-05-06, i.e. from its own birth).
  Anything needing sub-daily execution prices (e.g. the §5 spread/midpoint
  reconstruction at entry time) is therefore time-limited by retention to roughly the
  last 30 days, **independent of which side of 2026-04-28 it falls on**.

## Recommendation: **(c) span the boundary WITH a continuity check** — with a strong lean toward keeping the *test* window post-2026-05-02

Reasoning:

- **Not (a) "entirely before":** the *richest* and only fine-grained data is post-
  migration; restricting to V1-only throws away the cleanest data and leaves you on the
  data-thinned, fine-resolution-absent side (nothing hourly before 2026-05-02).
- **Not (b) "entirely after" as a hard rule:** the daily series is genuinely continuous
  across the boundary, and pre-migration markets *do* resolve and *do* return trades, so
  there is real, usable signal before 2026-04-28. Discarding it wholesale is unnecessary
  and costs sample size for a high-school-scale study.
- **(c) span it, but instrument the seam:** allow the window to cross 2026-04-28 while
  running an explicit continuity check at selection/simulation time. Concretely:
  1. Per market, assert `/prices-history` daily points exist on both sides with no gap
     `> 14 days` inside the window (these markets pass; flag any that don't).
  2. Treat **2026-03-30 .. 2026-04-19 (W14–W16)** as a **data-thinning caution zone** —
     down-weight or annotate results that depend heavily on entries in that span.
  3. For any logic needing **sub-daily** prices (spread model anchored to intraday
     midpoint at entry), keep that portion of the analysis to **`test_start >=
     2026-05-02`**, since fine data does not exist earlier.

**Concrete config suggestion** (to be set in Phase 3 once the universe date-range is
fixed): a defensible split is `train` across the continuous daily history up to
`train_end ≈ 2026-04-21` (before the thinning zone bites) and `test_start ≈ 2026-05-02`
(first fully fine-grained, unambiguously-V2 day). That single choice (a) keeps R1 strict
temporal separation, (b) puts the migration seam inside the *gap* between train and test
rather than inside either set, and (c) guarantees the test side has full daily AND hourly
data. If more test sample is needed, extend `test_start` only downward to 2026-04-29
(post-migration, daily-only) and explicitly label it as daily-resolution.

## Limitations / honesty notes (R6-adjacent rigor)

- `/prices-history` point density is a **proxy** for trade activity, not a trade count.
  The true "weekly `/trades` count" the task asked for is **not retrievable** from the
  API; this is a finding, not a gap in effort.
- The hourly 2026-05-02 wall was initially a migration suspect but is **resolved**: a
  post-migration market (created 2026-05-05) has hourly data from its own birth
  (2026-05-06), proving the wall is a **rolling ~30-day retention window**, not a V1/V2
  effect. Net: **no migration-attributable discontinuity was found** in any endpoint.
- Markets sampled are high-volume political/sports/novelty contracts; thinner markets may
  show larger gaps. The continuity check in (c) must run **per market**, not once
  globally.
