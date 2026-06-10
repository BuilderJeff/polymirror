# Existing-Repo Scout — polymirror

Scope: assess open-source code to clone/adapt so we build NEW code for only two
components (the wallet-selection statistics and the mirror/benchmark simulation).
Assessment date: 2026-06-02. Verification via WebFetch (no `gh` CLI on PATH) + WebSearch.

**SECURITY CAVEAT (carried into every recommendation):** the spec warns that generic
"Polymarket copy-trading bot" repos have historically contained MALWARE (key-stealers,
malicious post-install scripts). None of the repos below is such a bot, but the lesson
holds: we never `pip install` and run an unvetted trading bot, never feed it a key, and
treat any copy-trading code as **read-only-for-design** only. This is a historical,
read-only backtest (R0) — no repo here is run with a funded wallet or a private key.

---

## Recommendation table

| # | Repo | Status (2026-06-02) | What it provides | Fit for polymirror | Call |
|---|------|---------------------|------------------|--------------------|------|
| a | `evan-kolberg/prediction-market-backtesting` | Alive. Python 93.9%, ~899 stars, mixed GPL-3.0/LGPL/MIT, `v4.1-alpha` active | NautilusTrader extension for prediction markets: binary 0/1 settlement, fee modelling w/ maker rebates, slippage/latency/queue execution, **copy-trading account-ledger replay**, multi-market charting (equity/P&L/drawdown). Modules: `prediction_market_extensions/`, `strategies/`, `backtests/`, `live/` | Matches the intended ENGINE anchor on paper. BUT: heavyweight NautilusTrader dependency + alpha branch + mixed/copyleft (GPL) license is a poor fit for a small, auditable, reproducible HS project where R8 demands "same Config => identical numbers." The valuable parts are the **settlement/fee/slippage logic and the ledger-replay design**, not the framework. The `live/` trading plumbing must never be run (R0). | **read-only** (design reference for settlement, fee/rebate, slippage, and ledger-replay; do NOT adopt the Nautilus runtime or run `live/`) |
| b | `warproxxx/poly_data` | Alive. Python 100%, ~2.0k stars, 400 forks, GPL-3.0 | On-chain ingestion pipeline: reads `OrderFilled` events from **CTF Exchange V2** via Polygon JSON-RPC, joins Gamma market metadata, emits `processed/trades.csv` (price, USD, BUY/SELL). Modules: `update_chain.py`, `update_markets.py`, `process_live.py`, `poly_utils/`. **Market-wide order events, not per-wallet follow.** | Useful *design reference* for the on-chain ingestion path and the CTF-V2 event schema (directly relevant to the ~2026-04-28 V2 migration gotcha noted in config.py §9). But our plan sources trades from the Polymarket **Data API** (`/trades`, `/activity` per wallet) + Gamma resolutions, which is simpler and needs no RPC node. GPL-3.0 means we port ideas, not copy code, into our MIT/none project. | **read-only** (reference the CTF-V2 event decode + Gamma join; prefer Data-API ingestion over forking the RPC pipeline) |
| c | `SII-WANGZJ/Polymarket_data` (HuggingFace) | Alive. Parquet, ~4.01B rows, 60 likes, updated 2026-04-16 | **CORRECTION to the spec premise:** this is **NOT ~1.1B trades**. The 21-column schema is **market/event METADATA** (id, question, slug, condition_id, token1/2, outcome_prices, volume, created_at/end_date/updated_at, closed/active/archived, neg_risk). There is **no per-trade, no per-wallet, no price-time-series** column. The 4B row count is metadata snapshots (incl. high-frequency 5m/15m crypto markets), not executed trades. | Does NOT satisfy the "bulk per-trade backtest" need. It cannot drive a mirror/benchmark replay (no trades, no wallets, no fills). It IS a fast offline **market-universe filter** (volume >= 50k, binary-only, resolution outcome via outcome_prices) — i.e. it can seed `universe_*` selection in config.py without hammering Gamma. License unspecified on the card => treat as unknown, verify before redistribution. | **read-only** (use ONLY as a market-universe/metadata cache for selection; do NOT treat as a trade source — premise mismatch) |
| d | `Polymarket/py-clob-client` | **ARCHIVED 2026-05-25.** "no longer functional, do not use." MIT, ~1.2k stars, last release v0.34.6 (2026-02-19). Successor: `Polymarket/py-sdk` | Official CLOB client. Read-only ops (book, midpoint, **spread**, prices-history) needed NO auth; only trading needed a key. | The archived client is a dead end — but we don't need the SDK at all: config.py and the spec already hit `clob.polymarket.com` (`/book`, `/spread`, `/midpoint`, `/prices-history`) directly with `requests`. The successor **`Polymarket/py-sdk`** (MIT, beta, `PublicClient()` works key-free) is the live reference for exact endpoint shapes / param names IF a raw call is ambiguous. R6: any CLOB `/spread` read is for **Phase-1 calibration of the modelled spread only**, never an observation backfilled into history. | **skip** (archived/non-functional). Use plain `requests` to the documented CLOB endpoints; consult `py-sdk` read-only as a reference if endpoint shapes are unclear |
| e | Dune `polymarket_polygon` curated tables | Alive. Curated tables confirmed: `polymarket_polygon.market_trades` + `polymarket_polygon.market_details`, updated daily | `market_trades` = one row per fill (price, amount, shares, maker/taker addresses); `market_details` = market metadata (question, tags, resolution times). Clean, normalized on-chain trades **by address** — ideal for independent cross-checks of our Data-API ingestion. | Strong fit as a **read-only cross-check oracle** (does our per-wallet trade set match Dune's?). Access needs a Dune account; **free tier = 2,500 API credits/month + API access by default**, enough for spot-check queries by address/condition_id. Not a primary pipeline (rate/credit limited, daily lag), but an excellent honesty check (empirical-rigor standard). Requires a `DUNE_API_KEY` env var — keep out of source (security.md). | **read-only** (manual/low-volume cross-check via free-tier API; not the primary ingestion path; never commit the API key) |

---

## Net plan (what to actually pull in)

- **Clone:** none. No repo is a clean drop-in foundation for a small, reproducible (R8),
  permissively-licensed HS project. The two NEW components (wallet-selection stats,
  mirror/benchmark simulation) are written fresh against `config.py`.
- **Read-only design anchors:**
  - settlement / fee+rebate / slippage / ledger-replay semantics  -> repo (a)
  - CTF Exchange V2 event decode + Gamma join (V2 migration gotcha) -> repo (b)
  - exact key-free CLOB read endpoint shapes                        -> `py-sdk` (successor to (d))
- **Data sources:**
  - primary trades/wallets/resolutions -> Polymarket Data API + Gamma (direct `requests`)
  - market-universe metadata cache     -> HF dataset (c) [metadata only, NOT trades]
  - independent cross-check            -> Dune `market_trades` (e) [free tier, keyed]

## Key corrections vs. the task brief

1. **(c) is mislabelled in the brief.** It is ~4B rows of **market/event metadata**, not
   ~1.1B *trades*. It cannot drive the bulk backtest; it is a universe/metadata cache only.
2. **(d) is now ARCHIVED & non-functional** (2026-05-25). Don't depend on it; the engine
   talks to CLOB endpoints directly. `Polymarket/py-sdk` is the live key-free reference.

## Hard-rule compliance notes

- **R0** (read-only backtest): every repo is reference/data only. No bot is run; no key is
  ever required or stored. The `live/` dir of (a) and any trading path is explicitly off-limits.
- **R6** (spread is modelled, never observed): CLOB `/spread` (via direct calls / `py-sdk`)
  is used solely to **calibrate** the modelled `SpreadPreset` in Phase 1, with chosen numbers
  reported alongside results. No order-book read is backfilled as historical truth.
- **Malware caveat:** acknowledged and enforced — no copy-trading bot is executed; copy-trading
  material from (a) is consumed as design only.
