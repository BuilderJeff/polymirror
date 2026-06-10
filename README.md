# polymirror

A **research-grade historical backtest** asking one question:

> To what extent can a Polymarket mirror-trading strategy that copies wallets identified as
> high-accuracy in a prior (training) period outperform a buy-and-hold-the-favorite benchmark
> in a later (test) period, **after transaction costs**?

The evaluation standard is **empirical rigor and honesty about what the data supports** — not
trading profit, speed, or cleverness. **A defensible null result is a success.** A profitable
result that rests on a hidden bias is a failure. The headline output is an **edge-decay curve**
over holding horizon *N*, not a single number.


---

## §0 — Hard rules (override everything)

1. **Historical backtest only.** This code never places a live trade, never requires or stores a
   private key, and never connects a funded wallet. Every API call is a read.
2. **No look-ahead bias, ever.** Enforced in code by `assert_no_leakage(df, cutoff_ts)`, called at
   every wallet-selection step — not by good intentions.
3. **Honesty about the spread.** There is no free historical order-book depth, so the spread is a
   **modeled parametric assumption**, labeled as such on every number it touches, and swept over
   `optimistic` / `base` / `conservative` presets.

## Design rules (each one, if violated, invalidates the result)

| | Rule |
|---|---|
| **R1** | Strict temporal train/test split. Selection stats use **only** training-window trades. `assert_no_leakage` hard-fails on any selection row with `timestamp >= cutoff_ts`. |
| **R2** | Ex-ante, machine-evaluable accuracy = a **proper score** (Brier / log loss) on entry prices vs realized outcomes, training window only. Never select by raw PnL, leaderboard, or reputation. |
| **R3** | Skill-vs-luck filter. A wallet is eligible only if it beats a luck null (sign-permutation bootstrap), not merely ranks high. Survivor count is a required reported output. |
| **R4** | Favorite defined **at entry** (`price > favorite_threshold`, default 0.50) at the mirrored entry timestamp — never with hindsight. |
| **R5** | Strategy and benchmark are **identical except for which side is bought**: same trigger timestamp, same exit rule, same cost model, same spread. That isolation *is* the experiment. |
| **R6** | Spread is a swappable, labeled parameter. Every headline result is reported under all three presets. A result that survives only `optimistic` is reported as not a result. |
| **R7** | Wallet ≠ person. The unit of analysis is the **wallet** (`proxyWallet`). Stated as a limitation. |
| **R8** | Reproducibility. A single `config.py` drives all parameters; every random process takes an explicit `seed`. Same config + same cached data ⇒ identical numbers. |

---

## The two components we build ourselves

Everything else adapts existing open-source code (see `notes/existing_repos.md`). These two are
original, and are where the rigor lives:

1. **`scorer.py`** — per-wallet accuracy scorer (Brier + log loss on entry prices vs outcomes) and
   the **luck filter** (sign-permutation bootstrap + Benjamini–Hochberg FDR control).
2. **`simulator.py`** — out-of-sample mirror simulator + buy-the-favorite benchmark, producing the
   **edge = strategy − benchmark** decay curve with bootstrapped CIs, under each spread preset.

## Data sources (public, no auth)

| API | Base URL | Used for |
|---|---|---|
| Gamma | `https://gamma-api.polymarket.com` | markets, events, **resolution outcomes** (ground truth) |
| Data | `https://data-api.polymarket.com` | per-wallet `/trades`, `/activity`, `/positions`, `/holders` |
| CLOB | `https://clob.polymarket.com` | **current** `/book`, `/spread`, `/midpoint` (spread calibration only) |

There is **no free historical order-book depth** — see `notes/spread_calibration.md`.

---

## Repository layout

```
polymirror/
├── config.py              # SINGLE source of truth for every parameter (spec §7.7) + spread presets
├── requirements.txt       # dependencies   (requirements.lock = frozen versions)
├── polymirror/            # package
│   ├── polyapi.py         # cached, paginated, backoff HTTP client for the three APIs
│   ├── scorer.py          # [Phase 4] accuracy scorer + luck filter   (CUSTOM)
│   └── simulator.py       # [Phase 4] mirror sim + benchmark + decay curve   (CUSTOM)
├── phase1_slice.py        # one-market end-to-end vertical slice (Phase 1)
├── data/cache/            # parquet/JSON caches — regenerable, git-ignored
├── results/              # decay curve, return tables, scatter — regenerable
├── notes/                # recon findings: api_schema, existing_repos, spread_calibration, v1v2
└── tests/                # unit tests (scorer toy set, assert_no_leakage)
```

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe config.py        # self-test the config
```

## How to run

> Filled in as phases land. Phase 1 (vertical slice):
> `.\.venv\Scripts\python.exe phase1_slice.py`

---

## Which parts are cloned vs custom

_Filled in after Phase 1 repo scouting — see `notes/existing_repos.md`._

## Limitations

See [`LIMITATIONS.md`](LIMITATIONS.md) (written in Phase 6): wallet ≠ person; the parametric-spread
assumption; V1/V2 data-span continuity; the luck-vs-skill caveat with citations; and any
data-availability compromises surfaced during Phase 1.

## Build status

- [x] **Phase 0** — environment, venv, `config.py` single source of truth
- [ ] **Phase 1** — connectivity + one-market vertical slice _(in progress)_
- [ ] **Phase 2** — stand up the cloned backtest engine
- [ ] **Phase 3** — ingestion at scale + parquet caching
- [ ] **Phase 4** — `scorer.py` + `simulator.py` (the two custom components)
- [ ] **Phase 5** — full run + sensitivities (spread presets, `min_trades_per_wallet` sweep)
- [ ] **Phase 6** — write-up + LIMITATIONS
