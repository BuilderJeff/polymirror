# Does Copying Skilled Polymarket Wallets Beat Buying the Favorite? Evidence from a Leakage-Controlled Backtest and a Live Forward Test

**A research-grade study of mirror-trading on Polymarket prediction markets**

*polymirror project · run dated 2026-06-07/08 · all code, parameters, and seeds in this repository*

---

## Abstract

We ask whether a strategy that **mirrors** the entries of wallets identified as high-quality on Polymarket can outperform a **buy-the-favorite** benchmark, after costs, over short holding horizons. We attack the question two independent ways. **Study 1** is a strictly leakage-controlled *historical* test: we score every sufficiently active wallet on a proper score (Brier) over resolved 2025–2026 binary markets and subject each to a skill-vs-luck filter (Bernoulli-price null, 10,000-draw bootstrap, Benjamini–Hochberg FDR control). **Of 151 wallets tested, zero survive** the luck filter at any activity floor — even before multiple-testing correction. **Study 2** is a *live forward* test run over a single ~6–12 hour window: we abandon the (empty) skill-filtered watchlist and instead mirror the **top-10 active wallets from the Polymarket profit leaderboard**, capturing 5,854 real trades across 202 markets in real time and reconstructing hour-by-hour mark-to-market returns from the CLOB price-history API. The forward mirror shows a **positive but fragile** gross edge over the favorite (+2.0% at 1h rising to +12.6% at 7h) that **does not survive honest inference**: clustered by market (the correct unit, n≈200–340) the 95% bootstrap CI includes zero at every horizon; the apparent "significance" clustered by wallet rests on only four wallets agreeing in sign. **Both** the mirror and the favorite *lose money* in absolute, gross-of-cost terms over these horizons. Two methods, one conclusion: **we find no robust, exploitable mirror-trading edge.** A defensible null is the result.

---

## 1. Research question and design philosophy

> To what extent can a strategy that mirrors wallets identified as high-quality outperform buy-and-hold-the-favorite over short holding horizons, *after costs*?

The unit of analysis is the on-chain wallet (`proxyWallet`); a wallet is not a person (a single operator may run many; one wallet may be a bot or a copy-trader itself). The evaluation standard is **empirical honesty, not profit**: a defensible null result is a success, and a profitable-looking result that rests on a hidden bias is a failure. The headline object is an **edge-decay curve** — edge = (mirror − benchmark) return as a function of holding horizon *N* — not a single number.

Eight design rules govern the work and, if violated, invalidate a result:

| | Rule |
|---|---|
| **R0** | Historical / read-only. No live trade, no private key, no funded wallet. Every API call is a GET. |
| **R1** | Strict temporal train/test split; selection statistics use training-window trades only. |
| **R2** | Ex-ante, machine-evaluable accuracy = a proper score (Brier/log loss), never raw PnL, leaderboard, or reputation. |
| **R3** | Skill-vs-luck filter: a wallet is eligible only if it beats a luck null, not merely ranks high. |
| **R4** | "Favorite" defined at entry (mid > 0.5 at the entry timestamp), never with hindsight. |
| **R5** | Strategy and benchmark identical except which side is bought: same trigger, exit rule, costs, spread. |
| **R6** | Spread is a swappable, labeled parameter; every headline reported under all three presets; a result surviving only the optimistic preset is *not* a result. |
| **R7** | Wallet ≠ person (stated limitation). |
| **R8** | Reproducibility: one `config.py` drives all parameters; every stochastic step is explicitly seeded. |

Study 2 **deliberately and explicitly relaxes R2** (see §5.1) and is therefore labeled *exploratory*, not confirmatory. That deviation is itself a finding: it was forced by Study 1's empty survivor set.

---

## 2. Data and the binding constraints

Three public, no-authentication Polymarket APIs:

- **Gamma** (`gamma-api.polymarket.com`) — markets and resolution (ground-truth outcomes). A binary market is *resolved* iff `closed==true` and `outcomePrices ∈ {["1","0"],["0","1"]}`; the winning index is the position of the "1"; settlement time is `closedTime`.
- **Data API** (`data-api.polymarket.com`) — per-wallet/per-market trade records (`proxyWallet`, `conditionId`, `price`, `timestamp`, `side`, `outcomeIndex`, `size`). `/trades` ignores `start`/`end` and is capped at the ~4,000 most-recent records per market; `/activity` **honors** `start`/`end` (used for the forward capture).
- **CLOB** (`clob.polymarket.com`) — `/midpoint`, `/spread`, and — critically for Study 2 — `/prices-history`.

**A methodological correction.** Earlier project notes assumed the order book is destroyed on resolution, forcing historical marks to be reconstructed from trade prints. During Study 2 we found this is **only true for long-pruned markets**: the CLOB `/prices-history?market=<tokenId>&startTs=&endTs=&fidelity=<minutes>` endpoint returns a **1-minute** price series that **persists for days after resolution** (verified: a market resolved ~6 days earlier still returned its full series; a 1.5-year-old market was pruned to empty). This is what makes a low-frequency, on-demand forward design feasible — exit marks can be reconstructed retrospectively, so the collector needs network access only at capture/analysis time, not continuously at each horizon.

---

## 3. Study 1 — Historical skill-vs-luck filter

### 3.1 Method

For every wallet with at least *k* resolved BUY entries (activity floors *k* ∈ {8, 10, 15}), we collapse all fills in a given market to **one position per (wallet, market, outcome)** — a size-weighted mean entry price and a single realized outcome. This collapse is essential: a wallet that places 71 fills in one market has made **one** correlated bet, not 71, and treating them as independent inflates every naive *t*-statistic several-fold.

Each wallet receives a **Brier score** on its positions: mean (p − y)², where p is the entry-implied probability of the bought token and y∈{0,1} is whether it won. We then test each wallet against a **luck null**: holding its entry prices pᵢ fixed, redraw outcomes yᵢ* ~ Bernoulli(pᵢ) 10,000 times and recompute the score — the distribution of a wallet with *no skill beyond the market price*. The empirical p-value is Laplace-smoothed: (#{null ≤ observed} + 1)/(n_boot + 1). Each wallet's RNG is seeded deterministically from `config.seed ⊕ blake2b(wallet)`. Because many wallets are tested, we apply **Benjamini–Hochberg FDR** control and report both raw and FDR survivor counts.

The Bernoulli-price null is deliberately strict: it subsumes the favorite-buyer fallacy. Buying at 0.84 and winning 84% of the time is *exactly* what the null does, so it earns no significance.

### 3.2 Results: zero survivors

The candidate pool was built from 60 high-volume resolved binary markets (2026 settlement), yielding **85,184 positions across 67,489 wallets**. Applying the activity floors:

| Activity floor (positions) | Wallets tested | Raw survivors (p < 0.05) | FDR survivors |
|---|---|---|---|
| ≥ 8  | 151 | **0** | **0** |
| ≥ 10 | 79  | **0** | **0** |
| ≥ 15 | 27  | **0** | **0** |

The result is not marginal. Across 151 tested wallets the **minimum p-value was 0.305**; the p-value distribution piles up at the top of the unit interval (126 of 151 in [0.9, 1.0], 18 in [0.8, 0.9], and **none below 0.30**). Under pure chance one would expect ≈7–8 wallets below p=0.05; observing **exactly zero** indicates the tested wallets are not merely unskilled but, on average, score *at or slightly worse than* the price-implied null (mean null margin +0.0024; 17% score worse than their null). The strong naive signals in an early watchlist (t-statistics of 7 and 14) were artifacts of within-market fill correlation; they vanish once fills are collapsed and compared against the price null.

**Interpretation.** Under the project's own pre-registered eligibility rule (R3), the historical data admit **no wallet** worth mirroring. This is consistent with short-horizon informational efficiency of these markets and with the broader literature finding that apparent skill in large trader populations is overwhelmingly luck once multiple testing is controlled.

---

## 4. Study 2 — Live forward mirror experiment

### 4.1 Selection (an explicit R2 deviation)

Because Study 1 left an empty watchlist, a forward confirmatory test of "skilled" wallets was impossible. We therefore ran an **exploratory** forward test using a different, openly-labeled selection rule: the **top-10 wallets by 30-day profit on the Polymarket leaderboard** (`lb-api.polymarket.com/profit?window=30d`), each verified currently active via a live `/activity` check. This **violates R2** (selection by realized PnL/reputation) and is reported as hypothesis-generating, not confirmatory. The question it answers is narrower and more practical: *if a retail user naively copies whoever is winning right now, do they beat buying the favorite?*

### 4.2 Capture and mark-to-market

Over a single window (2026-06-07 20:04 UTC → 2026-06-08 08:04 UTC; nominally a 6-hour evening session, extended to 12h), an on-demand puller recorded **every** trade (BUY and SELL) the 10 wallets made in live binary markets. Each trade is normalized to an equivalent opening **long** (a SELL of token k at p is copied as a long of the complement at 1−p) — i.e. we copy the *action*, acknowledging that a SELL may be profit-taking rather than a fresh view. For each captured position we reconstruct, from `/prices-history`, the mid of both outcome tokens at entry and at each elapsed hour N; horizons past resolution settle to 0/1 (Gamma). The **strategy leg** is the copied long; the **benchmark leg** is the favorite-at-entry token; the two share entry timing and exit rule and differ only in side (R5).

Returns are computed gross and then collapsed — as in Study 1 — to **one position per (wallet, market, side)** before aggregation, then clustered by wallet. All marks were reconstructed from server-side history with **zero missing** (no horizon was silently filled).

### 4.3 Sample

| | |
|---|---|
| Window | 2026-06-07 20:04 → 2026-06-08 08:04 UTC |
| Positions captured | 5,854 (5,771 BUY / 83 SELL) |
| Distinct markets | 202 (predominantly same-day live sports + a few politics/longshots) |
| Wallets actually active | 4 of 10: HomeRunHazard (2,428), ferrariChampions2026 (1,982), Countryside (1,180), afghj2421 (264) |
| Resolved positions by close | 5,391 |

The effective wallet count is the dominant limitation: six of ten leaderboard wallets did not trade, and one of the four active ones (afghj2421) stopped after minutes. Inference rests on **3–4 wallets**.

### 4.4 Results: a positive but fragile gross edge

Edge-decay curve at window end (gross of spread; one position per wallet/market/side; clustered):

| N (h) | Mirror return | Favorite return | **Edge** | by-wallet 95% CI (n=4) | by-wallet p | by-market 95% CI (n) | by-market p |
|---|---|---|---|---|---|---|---|
| 1 | +4.6% | +2.6% | **+2.0%** | [+0.4%, +3.9%] | 0.006 | [−10.2%, +15.8%] (336) | 0.716 |
| 2 | +4.2% | +4.0% | **+0.2%** | [−0.6%, +1.1%] | 0.698 | [−13.5%, +15.7%] (325) | 0.906 |
| 3 | −0.9% | +0.8% | **−1.7%** | [−3.9%, +0.4%] | 0.158 | [−15.3%, +15.1%] (316) | 0.938 |
| 4 | −23.9% | −24.9% | **+0.9%** | [−0.6%, +3.6%] | 0.614 | [−12.4%, +19.8%] (296) | 0.708 |
| 5 | −23.3% | −25.9% | **+2.6%** | [−4.8%, +9.5%] | 0.504 | [−10.1%, +23.6%] (272) | 0.449 |
| 6 | −19.7% | −25.7% | **+6.0%** | [−0.6%, +12.6%] | 0.115 | [−6.0%, +29.5%] (248) | 0.209 |
| 7 | −15.3% | −27.9% | **+12.6%** | [+4.6%, +19.9%] | 0.006 | [−0.7%, +41.6%] (213) | 0.061 |

Three observations decide the interpretation:

1. **Clustering changes everything.** Clustered by *wallet* (n=4) the edge looks significant at 1h and 7h (p=0.006). But with only four wallets, that p-value reduces to "all four happen to share a sign" — a four-flip coin test, not evidence about a population. Clustered by *market* (the correct independent unit, n≈200–340), **every horizon's 95% CI includes zero**; the smallest by-market p is 0.061 (7h), never below 0.05. The honest reading is **no significant edge**.

2. **The edge is cohort-dependent and unstable.** The positive long-horizon edge (5–7h) is populated *only by the afternoon cohort*, the only trades aged that far. When a fresh batch of late-evening trades entered the short horizons, the 1–3h edge collapsed toward zero and 3h went **negative** (it had been +5.8% earlier in the run). A signal that flips sign as the sample composition changes is noise, not structure.

3. **Both legs lose money.** At horizons ≥4h both the mirror and the favorite return roughly −15% to −28% *gross*. The experiment never identifies a profitable strategy; at best it identifies that the mirror **loses slightly less** than mechanically buying the favorite. The deep negative returns reflect the dominance of in-game live sports markets that resolve to 0/1 within the window — buying *either* side at volatile mid-game prices was, on average, a losing proposition over these horizons.

The large negative returns are **before** the modeled bid-ask spread. Under R6 the spread (0.5–5¢ full, widening near 0/1 and on thin contracts) would subtract a further round-trip cost from *both* legs and could only erode, never create, a mirror edge.

---

## 5. Discussion

The two studies triangulate to the same answer from independent directions. Study 1 asks, with full rigor and a large sample, whether *any* wallet demonstrates calibration skill beyond the market price; the answer is an unambiguous no. Study 2 asks, with a relaxed and practical selection rule, whether naively copying current winners pays in real time; the answer is a fragile, statistically-insignificant, gross-of-cost "barely, and only relative to an also-losing benchmark."

This convergence is the substantive result. It is **evidence consistent with short-horizon efficiency** of these markets: by the time a trade is observable, its information is already in the price, so neither selecting on past calibration nor copying current winners yields a robust edge. We state the asymmetry of this inference explicitly (interpretation is one-directional): an edge ≈ 0 is *consistent with* efficiency but does **not prove** it — the copy rule may simply be weak, the window short, or the candidate pool too small. We do **not** claim to have proven the markets efficient; we claim to have **failed to reject** efficiency under two reasonable strategies, which is a meaningful negative result.

---

## 6. Limitations

- **R7 — Wallet ≠ person.** Mirrored wallets may be bots, market-makers, or copy-traders; "skill" is a property of an address, not an identity.
- **Study 2 selection (R2 deviation).** Leaderboard/PnL selection is exploratory and survivorship-prone (today's leaderboard is conditioned on having recently won).
- **Tiny effective N.** Study 2 inference rests on 3–4 active wallets over a single session; the by-wallet CIs are not trustworthy and the by-market CIs are wide.
- **Short, non-representative window.** ~12 hours, dominated by same-day live sports markets; results may not generalize to politics, crypto, or longer horizons.
- **Gross of spread.** No headline number includes transaction costs. Costs can only hurt the (long) mirror leg; the modeled-spread sweep (R6) is the appropriate next step and was not the focus of the forward run.
- **SELL ambiguity.** Copying SELLs as opening shorts conflates fresh views with profit-taking (only 83/5,854 positions, so minor here).
- **Mark granularity.** Horizon marks use the at-or-before price-history point; within-minute timing is not resolved. The favorite is determined from the entry-time mid, never the realized winner (R4).
- **Modeled, not observed, depth.** There is no free historical order-book depth; the spread is a parametric assumption throughout (R6).

---

## 7. Conclusion

Across a rigorous historical luck filter (151 wallets, **zero** survivors) and a live forward mirror experiment (5,854 trades, edge statistically indistinguishable from zero once clustered correctly, and negative in absolute gross terms), **we find no robust, exploitable mirror-trading edge over a buy-the-favorite benchmark on Polymarket at short horizons.** The most defensible reading is that these markets price observable trading information efficiently enough that neither past-calibration selection nor copy-the-winner selection beats the favorite after honest accounting. This is a clean null — and, by the standard set at the outset, a successful result.

---

## Appendix A — Reproducibility

- **Single source of truth:** `config.py` (windows, floors, `n_bootstrap=10,000`, `seed=20260602`, spread presets). Every stochastic step takes the seed explicitly; nothing reads wall-clock for results.
- **Study 1 pipeline:** candidate discovery and the position-collapsed luck filter (`notes/_discover.py`, `notes/_luckfilter.py`, `notes/_diag.py`) over the cached Gamma/Data pulls; scorer and null in `polymirror/scorer.py` + `polymirror/stats.py`.
- **Study 2 pipeline:** watchlist (`notes/_leaderboard_watchlist.py` → `notes/_watchlist_leaderboard.csv`); on-demand puller `forward_pull.py` (state in `data/forward/experiment.json`, keyed by transaction hash, idempotent); API helpers `get_activity`, `get_price_history`, `get_clob_midpoint`, `get_market_gamma(fresh=)` in `polymirror/polyapi.py`.
- **Charting outputs (`data/forward/`):** `positions.csv` (one row per trade), `marks_long.csv` (one row per trade × horizon, with strat/bench/edge returns), `edge_curve.csv` (point edge curve), `edge_curve_final.csv` (edge curve with bootstrap CIs under both clusterings).
- **Inference:** clustered bootstrap (`bootstrap_mean_ci`, `bootstrap_two_sided_p`) and Benjamini–Hochberg (`benjamini_hochberg`) in `polymirror/stats.py`, all seeded.

## Appendix B — Key parameters

```
train/test split        2025-01-01 → 2025-09-01 → 2026-01-01  (R1; Study 1 universe scoped to avoid the CTF V2 migration)
accuracy metric         Brier (log loss available)
activity floors         {8, 10, 15} positions per wallet
luck null               yᵢ* ~ Bernoulli(pᵢ), 10,000 draws, BH-FDR, α = 0.05
forward window          2026-06-07 20:04 → 2026-06-08 08:04 UTC
forward horizons        N ∈ {1, 2, …, 12} hours
spread presets (R6)     optimistic 1.0¢ / base 2.5¢ / conservative 5.0¢ full spread at mid (not applied to forward headline)
seed                    20260602
```
