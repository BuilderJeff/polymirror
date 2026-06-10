# Methods

A reproducible, leakage-controlled backtest of **Polymarket mirror-trading** against a
**buy-the-favorite** benchmark. Every parameter lives in `config.py` (the single source of truth);
every stochastic step is explicitly seeded; the same config + cached data reproduce every number
bit-for-bit. `«FILL: …»` markers are completed from the final run.

## 1. Research question and design

> To what extent can a strategy that mirrors wallets identified as high-accuracy in a **training**
> period outperform buy-and-hold-the-favorite in a **strictly later test** period, *after costs*?

The unit of analysis is the on-chain wallet (`proxyWallet`). The headline output is the
**edge-decay curve** — edge = (mirror − benchmark) net return — across holding horizons
N ∈ {0, 1, 6, 24, 48} hours, under three spread assumptions. A defensible null is a valid result.

## 2. Data and the binding API constraint

Three public Polymarket APIs (read-only; no key, no live trade — **R0**):
- **Gamma** — markets and resolution (ground-truth outcomes).
- **Data API** — per-wallet/per-market trade records (`proxyWallet`, `conditionId`, `price`,
  `timestamp`, `side`, `outcomeIndex`, …).
- **CLOB** — *current* book only; used solely to calibrate the spread model.

**Binding constraint (Phase 1):** the Data API returns at most the **most-recent ~4,000 trades per
market** (page ≤ 1000, offset ≤ 3000) and ignores `start`/`end` on `/trades`. We therefore observe each
market's final ~4,000 trades. This is the dominant limitation (see `LIMITATIONS.md §3`) and motivates
both the universe construction and the price-series reconstruction below.

## 3. Universe construction

Resolved, **binary** (exactly two outcomes), order-book-enabled markets that settled in **2025**, with
volume ≥ \$50k. Restricting to 2025 deliberately avoids the CTF Exchange V2 migration (~2026-04-28), so
no cross-migration continuity check is needed. The Gamma lister paginates closed markets by descending
volume (page size 100; offset ceiling ~20k handled gracefully). The final universe is `«FILL: N»`
markets, `«FILL»` resolving before the cutoff and `«FILL»` in the test window.

For each market we pull its observable trades, parse the Gamma resolution (a market is resolved iff
`closed==true` and `outcomePrices` = {"1","0"}; the **winning index** is where the price is "1"; the
**settlement time** is `closedTime`), and **assert 100% resolution-join coverage per market** — a wrong
join silently inverts returns, so it must hard-fail. Trades are normalised to one canonical schema
(`polymirror/schema.py`) and concatenated into a cached master frame (`data/cache/master_trades.parquet`).

## 4. Temporal split and leakage control (R1)

Trades are split by a single calendar cutoff (`train_end = test_start`, 2025-09-01). `assert_no_leakage`
hard-fails if any selection row has `timestamp ≥ cutoff` and is called inside the scorer.

**Strict-R1 strengthening.** A wallet is scored only on training trades whose **market also resolved
before the cutoff** (`resolution_ts < train_end`). Otherwise the score would depend on an outcome not
yet realised at the selection date — a subtle look-ahead. This is enforced in `run.py` via the
`resolution_ts` column carried on every trade.

## 5. Wallet accuracy (R2; `scorer.py`)

For each wallet with ≥ `min_trades_per_wallet` (default 30; swept over {10,20,30,50}) **BUY** entries in
the scoring set, we compute proper scores on the entry price `p` (the implied probability of the bought
token) versus the realised outcome `y` (1 if the bought token won):
- **Brier** = mean (p − y)² ; **Log loss** = −mean[y·ln p + (1−y)·ln(1−p)], p clipped to [1e-6, 1−1e-6].

Only BUY entries are used: a BUY unambiguously opens a long directional bet, whereas a SELL may be a
position *close* (not a fresh view) and cannot be disambiguated without full position tracking.

## 6. Skill-vs-luck filter (R3; §7.2)

Ranking high is not enough; a wallet must beat a luck null. For each wallet we hold its entry prices
`pᵢ` fixed and redraw outcomes `yᵢ* ~ Bernoulli(pᵢ)`, recompute the score, and repeat
`n_bootstrap = 10,000` times — the score distribution of a wallet with **no skill beyond the market
price**. The empirical p-value is `(#{null ≤ observed} + 1)/(n_bootstrap + 1)` (Laplace-smoothed). Each
wallet's RNG is seeded deterministically from `config.seed ⊕ blake2b(wallet)` (R8). Because many wallets
are tested, we apply **Benjamini–Hochberg FDR** control and report **both raw and FDR survivor counts**.
A small survivor set is expected (Akey et al.; Gómez-Cram et al.) and is itself a finding.

## 7. Out-of-sample simulation (R4, R5; `simulator.py`)

For each eligible wallet's **test-window BUY** entries:
- **Entry.** Open the same long the wallet opened (mirror) at the executable entry price; the benchmark
  opens a long on the **favorite at entry** — the side whose mid > `favorite_threshold` (0.50),
  determined only from prices at the entry timestamp (never the realised winner, **R4**).
- **Exit.** After N hours, exit at the executable price then — or, if the market settled within N hours,
  at the settled 0/1. Strategy and benchmark share entry timing, exit rule, cost model, and spread; they
  differ **only in which side is bought (R5)**. This isolation *is* the experiment.

**Price series.** Because the CLOB book is deleted on resolved markets, the historical mid used to mark
fixed-horizon exits is reconstructed from the **trade prints** themselves (a print of token *k* at *p*
implies mid *k* ≈ *p*, mid *(1−k)* ≈ 1−*p*); the exit mark is the last print at or before the horizon
(`polymirror/pricing.py`). Legs with no available quote are dropped and counted, never silently filled.

## 8. Cost model (R6; `costs.py`)

No free source gives historical book depth, so the round-trip spread is **modelled** and treated as an
explicit assumption. `half_spread = ½ · base_cents/100 · boundary_mult(p) · thinness_mult(vol)`, where
`boundary_mult = 1 + boundary_k·(1 − 4p(1−p))` widens the spread toward 0/1 and `thinness_mult` widens it
on thin contracts; the result is clamped. Presets (`optimistic`/`base`/`conservative`) were calibrated by
walking the *current* CLOB book at \$100/\$1k/\$5k order sizes (`notes/spread_calibration.md`). **Every
headline number is reported under all three presets; a result that survives only `optimistic` is reported
as not a result.** A BUY pays half the spread up on entry and the other half on exit.

## 9. Inference (§7.5)

Returns are **clustered by wallet** — we average within each wallet first, then bootstrap across wallets
(`n_bootstrap = 10,000`, seeded) — so one hyperactive wallet cannot dominate. We report, per N and per
preset, mean strategy and benchmark returns, the **edge** with a 95% bootstrap CI, and a two-sided
bootstrap p-value for H₀: mean per-wallet edge = 0 ("mirroring does not beat buy-the-favorite after
costs"). Accuracy and profitability are reported **separately** (§7.4): a wallet can be well-calibrated
yet unprofitable to copy once the market has priced it in (`results/accuracy_vs_profit.png`).

## 10. Interpretation is one-directional (§7.6)

An edge > 0 after costs whose CI excludes zero under the **base and conservative** presets is *evidence
against* full short-horizon efficiency — not a proof, and not a tradable strategy. An edge ≈ 0 is
*consistent with* efficiency but does **not** prove it; the copy rule may simply be weak, or the
candidate pool too small. We state this asymmetry explicitly and never claim efficiency from a null.

## 11. Reproducibility (R8)

`config.py` drives every parameter and window; all randomness is seeded; nothing reads wall-clock. The
pipeline is `ingest → score → luck-filter → simulate → results`, runnable end-to-end from the cached
parquet. The two original components (`scorer.py`, `simulator.py`) carry 106 unit tests on hand-checked
toy data; the data layer is validated by live smoke tests and the per-market join assertion.
