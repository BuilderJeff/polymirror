# Spread Calibration (Phase 1, R6)

**Date:** 2026-06-02  **Sample size:** 24 outcome tokens across 18 markets
**Sampler:** `polymirror/spread_sampler.py`  **Raw data:** `data/raw/spread_samples.csv`
**Cache:** `data/cache/*.json` (Gamma markets + CLOB `/midpoint`, `/spread`, `/book`)

> **R6 — spread is a MODELLED assumption, never a historical observation.** No free
> source gives historical order-book depth. What we *can* observe is the **current**
> live book. This note calibrates the model's preset numbers against that current
> reality so the chosen numbers are defensible, not invented. The model is still an
> assumption applied to past trades; these reads only anchor its magnitude.

---

## 1. What was sampled

Pulled active, `enableOrderBook=true`, `closed=false` **binary** markets from Gamma,
stratified into four cells to span the (price level × liquidity) plane:

| stratum | definition |
|---|---|
| `liquid_mid` | market volume ≥ \$50k, Yes-mid in [0.20, 0.80] |
| `liquid_boundary` | market volume ≥ \$50k, mid near 0/1 |
| `thin_mid` | market volume < \$50k, mid in [0.20, 0.80] |
| `thin_boundary` | market volume < \$50k, mid near 0/1 |

For each selected token we read CLOB `/midpoint` and `/spread` (top-of-book), plus
`/book` for depth. Degenerate placeholder quotes (e.g. a 1¢/99¢ book on a dead,
zero-liquidity market that reports a 98¢ spread) were dropped — they are modelling
artefacts, not real two-sided markets, and would poison calibration.

---

## 2. Raw sampled table (top-of-book `/spread`)

| stratum | outcome | mid | spread¢ | mkt vol $ | liq $ | question |
|---|---|---:|---:|---:|---:|---|
| liquid_mid | Yes | 0.643 | 0.10 | 33.8M | 335k | Spurs win 2026 NBA Finals? |
| liquid_mid | Yes | 0.252 | 0.10 | 25.6M | 218k | Newsom 2028 Dem nominee? |
| liquid_mid | Yes | 0.358 | 0.10 | 19.5M | 444k | Knicks win 2026 NBA Finals? |
| liquid_mid | Yes | 0.275 | 1.00 | 17.3M | 437k | US×Iran peace deal by Jun 30? |
| liquid_mid | Yes | 0.245 | 0.20 | 13.9M | 481k | Sánchez Palomino Peru pres? |
| liquid_mid | Yes | 0.215 | 1.00 | 12.7M | 305k | Hormuz traffic normal by Jun? |
| liquid_boundary | Yes | 0.003 | 0.10 | 91.1M | 16.3M | MicroStrategy sells BTC? |
| liquid_boundary | No | 0.998 | 0.10 | 91.1M | 16.3M | MicroStrategy sells BTC? (No) |
| liquid_boundary | Yes | 0.020 | 0.10 | 63.4M | 839k | Jesus returns before 2027? |
| liquid_boundary | No | 0.981 | 0.10 | 63.4M | 839k | Jesus returns (No) |
| liquid_boundary | Yes | 0.001 | 0.10 | 55.1M | 8.5M | Uzbekistan win 2026 WC? |
| liquid_boundary | No | 1.000 | 0.10 | 55.1M | 8.5M | Uzbekistan win WC (No) |
| thin_mid | Yes | 0.495 | 1.00 | 48.2k | 68k | Netherlands win 2026-06-14? |
| thin_mid | Golden Knights | 0.415 | 1.00 | 46.8k | 252k | Golden Knights vs Hurricanes |
| thin_mid | Yes | 0.717 | 0.40 | 46.7k | 24k | BTC > \$70k on Jun 3? |
| thin_mid | Yes | 0.445 | 1.00 | 45.3k | 26k | WTI hits \$80 in June? |
| thin_mid | Yes | 0.310 | 2.00 | 42.5k | 19k | BTC > \$72k on Jun 4? |
| thin_mid | Jakub Mensik | 0.345 | 1.00 | 40.8k | 114k | Mensik vs Fonseca (RG) |
| thin_boundary | Minnesota Lynx | 1.000 | 0.10 | 48.7k | 205k | Spread: Lynx (-2.5) |
| thin_boundary | Phoenix Mercury | 0.001 | 0.10 | 48.7k | 205k | Spread: Lynx (-2.5) (other) |
| thin_boundary | Yes | 0.999 | 0.20 | 48.6k | 65k | Trump insults Tucker by Jun 30? |
| thin_boundary | No | 0.001 | 0.20 | 48.6k | 65k | Trump insults Tucker (No) |
| thin_boundary | Yes | 0.001 | 0.10 | 48.1k | 65k | ETH > \$2,500 on Jun 2? |
| thin_boundary | No | 1.000 | 0.10 | 48.1k | 65k | ETH > \$2,500 (No) |

Full machine-readable rows incl. best_bid/best_ask in `data/raw/spread_samples.csv`.

---

## 3. Top-of-book spread by (liquidity × price band)

| tier | price band | n | min¢ | median¢ | mean¢ | max¢ |
|---|---|---:|---:|---:|---:|---:|
| liquid | boundary (<0.20 or >0.80) | 6 | 0.10 | **0.10** | 0.10 | 0.10 |
| liquid | mid (0.20–0.80) | 6 | 0.10 | **0.15** | 0.42 | 1.00 |
| thin | boundary | 6 | 0.10 | **0.10** | 0.13 | 0.20 |
| thin | mid (0.20–0.80) | 6 | 0.40 | **1.00** | 1.07 | 2.00 |

**Overall:** n=24, median = **0.10¢**, mean = 0.43¢, max = 2.0¢.
Percentiles: p50 0.10, p75 1.0, p90 1.0, p95 1.0, p100 2.0.

### Two findings that drive the model

1. **Top-of-book spreads are astonishingly tight.** Polymarket's CLOB tick size is
   small and market-making is dense; the *quoted* (1-tick) spread is 0.1¢ on most
   liquid contracts and rarely exceeds 1–2¢ even on thin ones.

2. **Boundary does NOT widen the top-of-book spread — it tightens it.** Tick size
   shrinks as price → 0/1, so near-boundary contracts show 0.1¢ spreads on *both*
   liquid and thin markets. This is the **opposite** of the model's `boundary_k`
   widening assumption, when measured at top of book. See §5 for why we keep a
   modest boundary term anyway (it captures size/adverse-selection risk the
   1-tick quote hides).

### Thinness effect (real and large)
`thin_mid` median (1.00¢) ÷ `liquid_mid` median (0.15¢) ≈ **6.7×** at top of book.
Low volume genuinely widens spreads in the contested mid-price region.

---

## 4. Effective spread under realistic order sizes (book-walking)

The CLOB `/spread` is the **1-tick top-of-book** spread — a *lower bound* on realized
cost. A real mirror trade fills a notional order and walks the book. We computed the
effective round-trip spread (VWAP ask − VWAP bid) from cached `/book` depth:

| order notional | overall median¢ | overall mean¢ | overall max¢ | liquid_mid med¢ | thin_mid med¢ | thin_mid max¢ |
|---|---:|---:|---:|---:|---:|---:|
| top of book | 0.10 | 0.43 | 2.0 | 0.15 | 1.00 | 2.00 |
| \$100 | 0.46 | 0.73 | 3.2 | 0.28 | 1.00 | 3.22 |
| \$1,000 | 0.88 | 1.20 | 7.1 | 0.63 | 1.01 | 7.08 |
| \$5,000 | 1.71 | 3.15 | 19.0 | 1.71 | 2.11 | 18.98 |

This is the **single most important calibration fact**: realized cost is driven by
depth, not the headline quote. A \$1k order pays ~0.9¢ median; a \$5k order pays
~1.7¢ median and *18.98¢* in the worst thin-mid case. Liquid boundary stays tight
even at size (deep books). The model's preset cents must represent **effective**
round-trip cost, so they sit above the raw 0.1¢ quote.

---

## 5. Recommended SpreadPreset numbers

The model is `half_spread = 0.5 · base_cents/100 · boundary_mult(p) · thinness_mult(vol)`,
where `boundary_mult = 1 + boundary_k·(1 − 4p(1−p))` and `thinness_mult = thin_mult`
when in-window volume < `thin_volume_usd`. `base_cents` is the **full** spread at
mid (p=0.5) for a *liquid* contract; floors/ceilings clamp the result.

We map presets to **order-size / execution-quality scenarios** observed in §4:

| field | optimistic | base | conservative | grounded in |
|---|---:|---:|---:|---|
| `base_cents` | **1.0** | **2.5** | **5.0** | liquid_mid: top-of-book 0.15¢ → \$100 0.28¢ → \$1k 0.63¢ → \$5k 1.71¢. Optimistic = small order on liquid book; base = realistic \$1–5k order; conservative ≈ 3× base stress. |
| `boundary_k` | **0.5** | **1.0** | **1.5** | Top-of-book spread does *not* widen at boundary, so the old 1.0/1.5/2.0 over-stated it. Kept positive but smaller: near 0/1 the *price* is small so a fixed cent cost is a large %, and adverse selection on near-certain outcomes is real. Lowered to reflect the observed tightening. |
| `thin_volume_usd` | **2,000** | **25,000** | **50,000** | Our `thin` cut was \$50k and those contracts already showed 6.7× wider mid spreads and severe \$5k-order blow-out (18.98¢). Raised base/conservative thresholds so the thinness penalty actually triggers on the sub-\$50k tail we measured. |
| `thin_mult` | **1.5** | **2.5** | **3.0** | thin_mid/liquid_mid ≈ 6.7× at top of book but ~1.0–1.3× once both walk a \$1k book; at \$5k thin blows out far more. 2.5–3.0× captures the depth-driven penalty without double-counting the quote ratio. |
| `min_cents` | **0.2** | **0.5** | **1.0** | Observed top-of-book min is 0.1¢; effective \$100-order min ≈ 0.1–0.3¢. Floor sits just below typical small-order cost so liquid contracts aren't penalised, but never free. |
| `max_cents` | **6.0** | **12.0** | **25.0** | Worst observed effective spread: \$5k thin-mid order = 18.98¢. Conservative ceiling 25¢ exceeds the worst real case; base 12¢ ≈ the p~95 of \$5k orders; optimistic 6¢ ≈ worst \$1k order. |

### Changes vs the first-pass defaults
- `base.base_cents` **3.0 → 2.5**: top-of-book and small-order reality is tighter
  than 3¢; 2.5¢ still covers a realistic \$1–5k order.
- `conservative.base_cents` **6.0 → 5.0**: 6¢ was above even the \$5k liquid-mid
  median (1.7¢); 5¢ is already a hard stress vs observed costs.
- `optimistic.base_cents` kept at **1.0** — matches \$100–\$1k liquid-mid execution.
- `boundary_k` lowered across the board: the data shows top-of-book *tightens*, not
  widens, near 0/1, so the prior widening was unjustified at the magnitude set.
- `thin_volume_usd` raised for base/conservative so the penalty fires on the
  genuinely-thin (<\$50k) tail we sampled.
- Floors/ceilings retuned to bracket the measured effective-spread distribution.

### Honesty caveats (do not omit from the writeup)
- These are **current** reads (2026-06-02); historical books may have been wider,
  especially pre-V2-migration (~2026-04-28, §9). The presets remain an *assumption*.
- `/spread` is top-of-book; we bridge to effective cost via current `/book` depth,
  which is itself a snapshot. Past depth is unobservable (R6).
- A result that survives only under `optimistic` is **not** a result. Report all
  three presets alongside every backtest number.

---

## 6. Reproduce

```
./.venv/Scripts/python.exe -m polymirror.spread_sampler
```
Writes `data/raw/spread_samples.csv`; reuses `data/cache/*.json` (R8 reproducible).
