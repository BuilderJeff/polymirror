# Polymarket API Schema — Empirical Contract (probed 2026-06-02)

> All findings below are **empirical**, obtained by firing real requests today and inspecting
> live responses. APIs drift; re-run the probe scripts to re-verify.
>
> Probe scripts (read-only, cache under `data/cache/`):
> - `data/probe_data_api.py` — Data API `/trades` + `/activity`
> - `data/probe_clob_rate.py` — CLOB `/book` `/spread` `/midpoint` `/prices-history` + rate-limit burst
>
> Hosts:
> - Gamma `https://gamma-api.polymarket.com`
> - Data  `https://data-api.polymarket.com`
> - CLOB  `https://clob.polymarket.com`
>
> Reference IDs used:
> - Resolved binary market (Trump 2024): conditionId `0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917`, gamma id `253591`, YES token `21742633143463906290569050155826241533067272736897614950488156847949938836455`
> - Live market (MicroStrategy/BTC): conditionId `0x3733a1b647e7364095736ab0966465d896a84cf3b6bc1695ca1f26c3239b3868`, YES token `25714007960293389110960044475283546872601238755063051359394740854408462452120`
> - Busy wallet (4000+ trades): `0xd23f8c8aab13cfb2a35da40b67f8471faf9894a1`

---

## 1. Data API `/trades`

`GET https://data-api.polymarket.com/trades`

Returns a **flat JSON array** of trade records (NOT an envelope). `user` is **optional** — with no
`user` it returns the global recent-trades firehose.

### 1.1 Fields (every field in a real record, with type + meaning)

Example record (`data/cache/example_trade.json`):

```json
{
  "proxyWallet": "0x614f8c216086a1b7eead36b89b456938406d3b8a",
  "side": "BUY",
  "asset": "21742633143463906290569050155826241533067272736897614950488156847949938836455",
  "conditionId": "0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917",
  "size": 15718.806263,
  "price": 0.6205051347924995,
  "timestamp": 1730844726,
  "title": "Will Donald Trump win the 2024 US Presidential Election?",
  "slug": "will-donald-trump-win-the-2024-us-presidential-election",
  "icon": "https://polymarket-upload.s3...png",
  "eventSlug": "presidential-election-winner-2024",
  "outcome": "Yes",
  "outcomeIndex": 0,
  "name": "NewDarkShark",
  "pseudonym": "Ambitious-Dynamics",
  "bio": "",
  "profileImage": "",
  "profileImageOptimized": "",
  "transactionHash": "0xc59af936390df287c490e51d45bddedd4c4d6b0a553364643b55394fdd564450"
}
```

| field | type | meaning |
|---|---|---|
| `proxyWallet` | str | trader's Polymarket proxy wallet (0x...). This is the join key for mirror selection. |
| `side` | str | `BUY` or `SELL` (the wallet's direction on `asset`/`outcome`). |
| `asset` | str | CLOB ERC-1155 token id (decimal string) of the specific outcome traded. |
| `conditionId` | str | the market's CTF conditionId (`0x...`). Join key to Gamma market resolution. |
| `size` | float | number of outcome shares traded. |
| `price` | float | execution price in USDC per share, range (0,1). **Probability-implied entry price.** |
| `timestamp` | int | **UNIX seconds** (e.g. 1730844726 = 2024-11). |
| `title` | str | market question text. |
| `slug` | str | market slug. |
| `icon` | str | image URL. |
| `eventSlug` | str | parent event slug (groups multi-market events). |
| `outcome` | str | human label of the side traded, e.g. `Yes`/`No`. |
| `outcomeIndex` | int | index into the market's `outcomes[]` array (0 or 1 for binary). |
| `name` / `pseudonym` / `bio` | str | trader profile (often empty). |
| `profileImage` / `profileImageOptimized` | str | avatar URLs (often empty). |
| `transactionHash` | str | on-chain tx hash. **Best per-record dedup key** (timestamp alone collides — many trades share a second). |

### 1.2 Query parameter behaviour (empirically tested)

| param | works? | observed behaviour |
|---|---|---|
| `user` | YES | filters to one proxyWallet. Optional (omit → global firehose). |
| `market` | YES | accepts a **single** conditionId **OR** a single CLOB token id. |
| `market` CSV (2 ids) | **NO** | comma-joined value → **HTTP 408** `{"error":"Request timed out..."}`. Do NOT pass CSV; loop per market. |
| `type` | accepted | `type=TRADE` accepted (no effect on `/trades`, which is already trades-only). |
| `side` | YES | `BUY`/`SELL` filter. Also accepts lowercase `buy`. |
| `start` | **IGNORED** | `start=2000000000` (future) still returns full results → **filter has no effect on /trades**. |
| `end` | **IGNORED** | `end=1000000000` (past) still returns full results → **no effect on /trades**. |
| `sortBy` | accepted | `TIMESTAMP`, `PRICE` accepted without error. |
| `sortDirection` | accepted | `ASC`/`DESC` accepted. |
| `limit` | YES | page size; **capped at 1000** (see pagination). |
| `offset` | YES | pagination offset; **hard ceiling 3000** (see pagination). |
| `filterType` / `filterAmount` | accepted | `CASH`/`TOKENS` + numeric accepted (200). |
| bogus param (`this_is_not_real`) | ignored | unknown params silently ignored (200). |

> **CRITICAL for this project:** `start`/`end` are **silently ignored on `/trades`**. Time-windowing
> for the train/test split (R1) must be done **client-side** by filtering on `timestamp`,
> **OR** use `/activity` instead (which *does* honour `start`/`end` — see §2).

### 1.3 Pagination (the real mechanism)

- **Default page size (no `limit`) = 100.**
- **Max page size = 1000.** `limit=1001`, `limit=5000` all clamp to **1000**.
- **Default sort = `timestamp` DESCENDING** (newest first).
- `offset` paginates correctly: consecutive pages do **not** overlap (verified by transactionHash sets).
- **Hard offset ceiling: `offset > 3000` → HTTP 400** `{"error":"max historical activity offset of 3000 exceeded"}`.
  - Practical consequence: via offset you can retrieve at most **offset 0..3000 + limit 1000 ≈ the most-recent ~4000 records** of a wallet. Older history is **unreachable by offset alone**.
  - **Termination rule:** stop when `len(page) < limit`. Do **NOT** rely on an empty page — at/near the data boundary the API can return a clamped trailing record rather than `[]`, and past offset 3000 it 400s.

> **For wallets with > ~4000 trades** you cannot page the full history with offset. Options:
> (a) use `/activity` with `start`/`end` windows (which are honoured), or
> (b) accept the most-recent ~4000 cap. Document whichever you choose.

---

## 2. Data API `/activity`

`GET https://data-api.polymarket.com/activity`

Same record shape as `/trades` **plus two fields**, and `user` is **REQUIRED** (`/activity` with no
user → **HTTP 400**).

### 2.1 Extra fields vs /trades

| field | type | meaning |
|---|---|---|
| `type` | str | activity kind. Observed values: **`TRADE`, `MERGE`, `CONVERSION`, `SPLIT`**. Only `TRADE` rows have a real `side`/`price`; `MERGE`/`CONVERSION` have `side=""`, `price=0`. |
| `usdcSize` | float | USDC notional of the activity (= `size * price` for trades). Use this for $-volume. |

Example `TRADE` activity row (`data/cache/example_activity.json`): same keys as a trade record + `"type":"TRADE"`, `"usdcSize":5.92`.

### 2.2 Query parameter behaviour

| param | works? | observed behaviour |
|---|---|---|
| `user` | **REQUIRED** | missing → HTTP 400. |
| `type` | YES | filter by activity type; accepts CSV (`TRADE,SPLIT,MERGE`). |
| `side` | YES | `BUY`/`SELL`. |
| `market` | YES | single conditionId. |
| `start` | **HONOURED** | `start=2000000000` (future) → **0 results**. Real filter. UNIX seconds. |
| `end` | **HONOURED** | `end=1000000000` (past) → **0 results**. Real filter. UNIX seconds. |
| `sortBy` / `sortDirection` | accepted | `TIMESTAMP` + `ASC`/`DESC`. |
| `limit` | YES | max **1000**, default 100. |
| `offset` | YES | same **3000 ceiling** as /trades. |

- **Default sort = timestamp DESCENDING.**
- **`start`/`end` ARE respected on `/activity`** (unlike `/trades`). This is the recommended endpoint
  for time-windowed wallet history — combine `type=TRADE` + `start`/`end` to walk a wallet's trade
  history within the training/test window without the client-side filtering /trades requires.

> **Endpoint choice for this project:** prefer **`/activity?user=&type=TRADE&start=&end=`** for
> selection windows (honours time bounds, exposes `usdcSize`), and `/trades` for the global firehose
> or simple single-market pulls. Both share the same 1000-page / 3000-offset limits.

---

## 3. Gamma `/markets` and `/markets/{id}` — resolution detection

### 3.1 Listing: `GET /markets`
Returns a flat array. Default scope **excludes closed markets**. Useful query params (verified):
`limit`, `offset`, `closed` (`true`/`false`), `active`, `order` (e.g. `volumeNum`, `volume24hr`),
`ascending`, `condition_ids`.

- **Pagination:** `offset` works (consecutive pages disjoint). Default order is unstable across calls —
  pass `order=` + `ascending=` for deterministic paging.
- **`condition_ids` filter quirk:** filtering a **resolved** market by `condition_ids` returns **n=0
  unless you also pass `closed=true`** (because the default scope hides closed markets). With
  `condition_ids=<cond>&closed=true` → exactly 1 match. Note the param is `condition_ids`; misspellings
  (`conditionId`, `conditionIds`) are **silently ignored** and return the default 20-market list — a
  dangerous false positive. `slug` is **not** a working list filter (returns 0).

### 3.2 Single: `GET /markets/{numericId}`
`{numericId}` is the gamma `id` (e.g. `253591`), **not** the conditionId. Returns a single object.

### 3.3 The outcomes / outcomePrices parallel arrays (RESOLUTION)
Both are **JSON-encoded strings** that parse to parallel arrays:

```json
"outcomes":      "[\"Yes\", \"No\"]"
"outcomePrices": "[\"1\", \"0\"]"      // RESOLVED: index 0 (Yes) won
```

- `outcomes[i]` is the label; `outcomePrices[i]` is that outcome's settled/last price.
- **`clobTokenIds`** (also a JSON string array) is parallel too: `clobTokenIds[i]` is the CLOB token id for `outcomes[i]`.

### 3.4 How to detect RESOLVED + which index WON (definitive)

```
resolved  ⟺  closed == True  AND  set(outcomePrices) == {"1","0"} (i.e. one is "1", other "0")
winningOutcomeIndex = index i where outcomePrices[i] == "1"
```

Empirical evidence:
- **Open** market → `closed=false`, `outcomePrices` are live fractional probs e.g. `["0.0025","0.9975"]` (NOT 0/1).
- **Resolved** market → `closed=true`, `outcomePrices` exactly `["1","0"]` or `["0","1"]`. Trump-2024: `["1","0"]` → Yes (index 0) won.

**Flag caveats (do NOT use these for resolution):**
- `active` stays **`true`** even after resolution (Trump market: `closed=true, active=true`). `active` ≠ "still trading".
- `archived` was `false` on resolved markets too — not a reliable resolution signal.
- `umaResolutionStatuses` is **unreliable**: empty `[]` on the fully-settled Trump market, yet `["proposed"]` on freshly-closed sports markets. **Use `closed` + `outcomePrices`, not UMA status.**
- `resolvedBy` is an address (the resolver), present but not a boolean resolved flag.

### 3.5 Other useful market fields
`enableOrderBook` (bool — required by `universe_require_order_book`), `acceptingOrders` (live trading open),
`volume`/`volumeNum`/`volume24hr`/`volumeClob` (USDC), `liquidity`/`liquidityNum`, `spread`/`bestBid`/`bestAsk`
(live CLOB snapshot embedded in Gamma), `negRisk`, `endDate`/`endDateIso`, `createdAt`/`updatedAt`.

### 3.6 Multi-market event caveat
Events (`GET /events`) can bundle **many** sub-markets. Example: "World Cup Winner" event →
`negRisk=true`, **60 sub-markets**, each with its **own distinct conditionId** (Spain, England, ...).
Each sub-market is an independent binary (Yes/No) market and must be treated separately — do **not**
assume one conditionId per event. For binary-only universe (`universe_binary_only`), select on the
**sub-market** level (`len(outcomes)==2`), not the event level.

---

## 4. CLOB endpoints (current order-book state — live markets only)

> All four require a **live, order-book-enabled** market. On a **resolved** market the book is gone:
> `/book`, `/spread`, `/midpoint` all return **HTTP 404** `{"error":"No orderbook exists for the
> requested token id"}`, and `/prices-history interval=max` returns `{"history":[]}`. Use a market with
> `enableOrderBook=true` and `acceptingOrders=true`.

### 4.1 `GET /book?token_id=<tokenId>`
```json
{
  "market": "0x3733...3868",          // conditionId
  "asset_id": "25714007960293389...",  // CLOB token id
  "timestamp": "1780374295357",        // STRING, UNIX MILLISECONDS
  "hash": "784444092b19cafe...",
  "bids": [ {"price":"0.001","size":"34194392.32"}, ... ],  // ascending price
  "asks": [ {"price":"0.999","size":"1199873.2"}, ... ],    // descending price
  "min_order_size": 5,
  "tick_size": 0.001,
  "neg_risk": false,
  "last_trade_price": <float>
}
```
- `price`/`size` are **strings**. Best bid = max bid price; best ask = min ask price.
- `timestamp` here is **milliseconds** (string), unlike Data API timestamps (seconds, int).

### 4.2 `GET /spread?token_id=<tokenId>`
```json
{"spread": "0.001"}
```
String. This is the **current** best-ask − best-bid. **R6 reminder:** this is a *current* snapshot only —
there is **no historical** spread feed, so the backtest spread remains a MODELLED assumption (see `config.py`).
Use these reads only to *calibrate* the spread preset, never as historical observations.

### 4.3 `GET /midpoint?token_id=<tokenId>`
```json
{"mid": "0.0025"}
```
String. (best-bid + best-ask)/2.

### 4.4 `GET /prices-history`
```json
{"history": [ {"t": 1779771605, "p": 0.095}, ... {"t": 1780374184, "p": 0.0025} ]}
```
- `t` = **UNIX seconds**, `p` = float price. Envelope key is `history`.
- Param `market=<tokenId>` (it is the **token id**, despite the name).
- Time selection two ways:
  - `interval` preset: `max` (coarse, ~28 pts on a young market), `1w`, `1d`, `1h`, `1m`.
  - `startTs` + `endTs` (UNIX seconds) for an explicit window.
- `fidelity` = candle resolution in **minutes** (e.g. 60 = hourly; 10 = 10-min).
- **Gotcha:** an explicit `startTs/endTs` range that is too wide for the chosen `fidelity` → **HTTP 400**
  `{"error":"invalid filters: 'startTs' and 'endTs' interval is too long"}`. Use a preset `interval`
  for long look-backs, or coarsen `fidelity`.

---

## 5. Rate limits (Data API burst test)

Fired **30 rapid sequential** `GET /trades?limit=1` from one IP:
- All **30 → HTTP 200**, completed in ~1.07 s (**~28 req/s**). **No 429s.**
- **No rate-limit headers** of any kind in responses (no `X-RateLimit-*`, no `Retry-After`, no `RateLimit-Remaining`).
- Conclusion: at ~28 req/s sequential there is no visible throttling and no header-based budget to read.
  Still follow project policy: small pages, exponential backoff on 429/5xx (per `config.http_*`), and
  cache under `data/cache/`. Treat the absence of headers as "back off blindly on 429/5xx" since the
  server gives no budget hints.

---

## 6. Cross-cutting gotchas (quick reference)

1. `/trades` **ignores** `start`/`end`; `/activity` **honours** them → use `/activity` for windowed pulls.
2. Page size caps at **1000**; offset ceiling is **3000** on both Data endpoints → ~4000 most-recent records max via offset; older history needs `/activity` time windows.
3. Terminate paging on `len(page) < limit`, **never** on empty page (boundary returns clamped row, then 400 past offset 3000).
4. `market` CSV (multiple conditionIds) → **408**; query one market at a time.
5. Data API timestamps = **UNIX seconds (int)**; CLOB `/book.timestamp` = **UNIX milliseconds (string)**; `/prices-history.t` = **UNIX seconds**.
6. Resolution = `closed==true` AND `outcomePrices ∈ {["1","0"],["0","1"]}`; winner = index of `"1"`. `active`/`archived`/`umaResolutionStatuses` are NOT reliable resolution signals.
7. Gamma default `/markets` scope **hides closed markets**; pass `closed=true` to fetch/filter resolved ones (incl. `condition_ids` lookups). Misspelled filter params are silently ignored (return default list).
8. Multi-market negRisk events bundle many distinct-conditionId binary sub-markets; select at sub-market level.
9. CLOB book/spread/midpoint 404 on resolved markets (book deleted); spread is current-only (R6: modelled, never historical).
10. CLOB outcome prices/sizes are **strings**; parse before math.
