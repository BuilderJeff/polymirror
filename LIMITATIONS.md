# Limitations

This backtest is built to be **honest about what the data supports**. The following limitations
are load-bearing — read them before citing any number. Several are structural (imposed by the free
data) and cannot be engineered away without paid data or live collection.

> Markers like `«FILL: …»` are completed from the actual run in the final write-up.

## 1. Wallet ≠ person (R7)
The unit of analysis is the on-chain proxy wallet (`proxyWallet`). One person may operate many
wallets, and one wallet could in principle be shared. We do **not** cluster wallets into entities.
"A skilled wallet" therefore means a skilled *address*, not a skilled *trader*, and persistence or
profitability at the person level may differ.

## 2. The spread is a modelled assumption, not an observation (R6)
There is **no free source of historical order-book depth**: the CLOB `/book`, `/spread`, `/midpoint`
endpoints return only the *current* book (and 404 on resolved markets, whose books are deleted), and
`/prices-history` is unreliable for old markets. We therefore **model** the round-trip spread
parametrically (`polymirror/costs.py`), calibrated against a sample of *current* live quotes walked at
\$100/\$1k/\$5k order sizes (`notes/spread_calibration.md`). Every headline number is reported under
three presets (`optimistic` / `base` / `conservative`); **a result that survives only `optimistic` is
reported as not a result.** The true historical spread is unknown and could differ from all three.

## 3. The dominant constraint: the ~4,000-trade-per-market API cap
The public Data API exposes at most the **most-recent ~4,000 trades per market** (page size ≤ 1000,
offset hard-capped at 3000) and silently ignores `start`/`end` on `/trades`. Consequences:
- For high-volume markets we observe only the **final window of trading near resolution**, biasing the
  sample toward late, near-certain prices and away from early price discovery.
- **Cross-window wallet overlap is thin.** Identifying a wallet as skilled in training *and* observing
  it trade again in the test window is exactly what mirror-trading requires, and the cap suppresses it.
  After widening the universe to `«FILL: N markets»` resolved 2025 markets, the backtest evaluates
  `«FILL: K»` candidate wallets (≥ `min_trades` scoring BUYs **and** ≥ 1 test BUY). This is a **small
  sample**; the bootstrap CIs reflect it, and low statistical power is a real possibility.
- A fuller design would pull each candidate wallet's complete history via per-wallet `/activity`
  time-windows (which *are* honored), at the cost of many more requests; we use market-level pulls as
  the primary source and note this as the first upgrade path.

## 4. Universe scope
The universe is resolved, **binary** (exactly 2 outcomes), order-book-enabled 2025 markets with
≥ \$50k volume. Scoping to 2025 deliberately **sidesteps the CTF Exchange V2 migration** (~2026-04-28,
new contracts/pUSD collateral) so no cross-migration continuity check is needed — but it is **not a
random sample** of Polymarket, and skews toward liquid sports/politics/crypto markets.

## 5. Price series are reconstructed from trade prints
Because the CLOB gives no historical book, the per-token mid-price series used to mark fixed-horizon
exits is built from the **trade prints themselves** (a print of token *k* at price *p* implies mid
*k* ≈ *p*, mid *(1−k)* ≈ 1−*p*). Exit marks use the last print **at or before** the horizon; between
prints the price is carried forward (no interpolation), and an exit after resolution settles at 0/1.
This is a reasonable proxy but is **not** the true executable mid, and thinly-printed intervals add
noise. The benchmark's favorite is likewise chosen from the entry print's implied complement.

## 6. Selection: skill vs. luck (the core intellectual risk)
Selecting wallets by raw PnL or the public leaderboard mostly captures **luck**, not skill — on this
exact platform, the top ~1% of traders capture ~76.5% of profits and apparent persistence may be
sample selection (Akey, Grégoire, Harvie & Martineau, SSRN 6443103); only ~3% of accounts appear
genuinely skilled and ~60% of "lucky winners" revert out-of-sample (Gómez-Cram, Guo, Jensen & Kung,
SSRN 6617059). We instead select by a **proper score** (Brier/log) on entry prices and a
**sign-permutation luck filter** with Benjamini–Hochberg FDR control. A **small survivor set is
expected and is itself a finding**, not a bug: `«FILL: raw vs FDR survivor counts»`.

## 7. The efficiency interpretation is one-directional (§7.6)
- An edge > 0 after costs whose CI excludes zero under the **base and conservative** presets is
  *evidence against* full short-horizon efficiency — **not a proof**, and never a live strategy (R0).
- An edge ≈ 0 is *consistent with* efficiency but does **not prove** it: our specific copy rule may
  simply be weak, and the small candidate pool may leave us underpowered. We do not claim efficiency
  from a null.

## 8. Other modelling choices (documented, not hidden)
- **BUY-only.** Only BUY entries are scored and mirrored; a SELL may be a position *close* (not a fresh
  directional view) and we cannot distinguish opens from closes without full position tracking.
- **Entry price = fill price.** We treat each trade's executed price as the mid at entry (it is the
  actual fill); the benchmark's other-token mid uses the 1−price complement when no print exists.
- **Fees.** Polymarket charges no per-trade CLOB taker fee at time of writing; the spread dominates.
  A `taker_fee_bps` knob exists for auditability.
- **No live execution, ever (R0).** This is a historical backtest; it never trades, signs, or holds a key.

## 9. Reproducibility caveats
All randomness is seeded and config-driven (R8), so the numbers reproduce bit-for-bit from the cached
data + `config.py`. The *upstream APIs are live and drift*; a fresh pull on a later date may return a
different (more recent) 4,000-trade window per market and thus slightly different inputs. The cached
parquet under `data/cache/` is the frozen snapshot the reported numbers come from.
