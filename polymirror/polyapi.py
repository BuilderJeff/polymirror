"""polyapi.py — a small, robust read-only client for Polymarket's Gamma + Data + CLOB APIs.

R0: HISTORICAL / READ-ONLY ONLY. This client issues GET requests against public,
no-auth endpoints. It never signs anything, never needs a private key, never posts.

Design goals (kept deliberately small, ~300 lines):
  * pagination over limit/offset that pulls ALL pages to completion (subject to the
    Data API's documented hard ceilings — see below);
  * exponential backoff with jitter on 429/5xx (config.http_*);
  * an on-disk JSON cache under data/cache/ keyed by endpoint + sorted params, so a
    given pull is fetched from the network exactly once (never re-pull);
  * typed helpers used by the rest of the pipeline:
      - get_trades(market, type='TRADE', **q)  -> list[dict]
      - get_market_gamma(condition_id)         -> dict (Gamma resolution record)
      - resolution_from_gamma(market_record)   -> ResolutionInfo (winning index, etc.)

Empirical API contract that shapes this client (see notes/api_schema.md, probed 2026-06-02):
  * Data API /trades returns a FLAT JSON ARRAY (no envelope).
  * /trades page size caps at 1000; offset is hard-capped at 3000 → at most ~4000
    most-recent records per market. We page to completion within that ceiling and stop
    on the first short page (len(page) < limit), NEVER on an empty page (the boundary
    can return a clamped trailing record, and offset>3000 returns HTTP 400).
  * /trades silently ignores start/end; time-windowing must be done client-side.
  * Gamma /markets hides closed markets by default; resolved lookups need closed=true.
    outcomes / outcomePrices / clobTokenIds are JSON-ENCODED STRINGS (parse before use).
  * Resolution: closed==true AND outcomePrices ∈ {["1","0"],["0","1"]}; winner = index of "1".
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from config import Config, DEFAULT

# --- Hosts (no auth) --------------------------------------------------------- #
GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Data API pagination ceilings (empirical — notes/api_schema.md §1.3).
_MAX_PAGE = 1000          # limit clamps to 1000
_MAX_OFFSET = 3000        # offset > 3000 → HTTP 400

_CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "cache" / "polyapi"
_USER_AGENT = "polymirror-polyapi/1.0 (research backtest; read-only)"


@dataclass(frozen=True)
class ResolutionInfo:
    """Parsed resolution of a binary Gamma market (immutable)."""
    condition_id: str
    resolved: bool
    winning_index: Optional[int]      # 0 | 1, or None if not resolved
    outcomes: list[str]               # e.g. ["Yes", "No"]
    outcome_prices: list[float]       # settled prices parallel to outcomes
    clob_token_ids: list[str]         # parallel CLOB token ids
    closed_time_unix: Optional[int] = None  # actual settlement time (Gamma closedTime), unix secs


class PolyAPIError(RuntimeError):
    """Raised when a request fails after exhausting retries, or returns bad data."""


class PolyClient:
    """Read-only HTTP client with caching + backoff for the three Polymarket APIs."""

    def __init__(self, config: Config = DEFAULT, cache_dir: Path | str = _CACHE_DIR):
        self.cfg = config
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})

    # --- cache ------------------------------------------------------------- #
    def _cache_path(self, host_tag: str, path: str, params: dict[str, Any]) -> Path:
        """Cache file keyed by host+endpoint+SORTED params (stable across call order)."""
        norm = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
        key = f"{host_tag}{path}?{norm}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        safe = path.strip("/").replace("/", "_") or "root"
        return self.cache_dir / f"{host_tag}_{safe}_{digest}.json"

    @staticmethod
    def _read_cache(p: Path) -> Optional[Any]:
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None  # corrupt cache → re-fetch

    @staticmethod
    def _write_cache(p: Path, data: Any) -> None:
        """Atomically persist `data` as JSON. BEST-EFFORT: a failed cache write must
        never crash the run — the data was already fetched successfully and caching is
        purely an optimisation (a missing entry is simply re-fetched next time).

        On Windows, antivirus/indexer real-time scanning intermittently holds a brief
        lock on a just-created file, surfacing as PermissionError on write or replace.
        We write to a UNIQUE temp file in the same directory (so concurrent writers and
        stale .tmp files can never collide), then os.replace() atomically, retrying a
        few times with short backoff before giving up gracefully.
        """
        payload = json.dumps(data, default=str)
        last_err: Optional[BaseException] = None
        for attempt in range(5):
            tmp_name: Optional[str] = None
            try:
                fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp_name, p)  # atomic on the same filesystem
                return
            except OSError as e:  # PermissionError (transient AV lock) or other FS error
                last_err = e
                if tmp_name is not None and os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass  # best-effort cleanup; a stale temp can't block us (unique name)
                time.sleep(0.15 * (attempt + 1))
        # Exhausted retries: warn but DO NOT raise — correctness does not depend on cache.
        print(f"[polyapi] WARN: cache write failed for {p.name} ({last_err}); "
              f"continuing uncached.", file=sys.stderr)

    # --- HTTP with backoff ------------------------------------------------- #
    def _request(
        self, base: str, host_tag: str, path: str, params: dict[str, Any],
        *, use_cache: bool = True, expect: str = "any",
    ) -> Any:
        """GET base+path with exponential backoff on 429/5xx. Caches successful JSON.

        expect: 'list' | 'dict' | 'any' — validates the decoded JSON shape.
        """
        cache_p = self._cache_path(host_tag, path, params)
        if use_cache:
            cached = self._read_cache(cache_p)
            if cached is not None:
                return cached

        url = f"{base}{path}"
        last_err: Optional[str] = None
        for attempt in range(self.cfg.http_max_retries + 1):
            try:
                r = self._session.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                last_err = f"network error: {e!r}"
                self._sleep_backoff(attempt)
                continue

            if r.status_code == 200:
                data = self._decode_json(r, url, params)
                self._check_shape(data, expect, url, params)
                if use_cache:
                    self._write_cache(cache_p, data)
                return data

            # Retry on throttle / transient server errors; fail fast otherwise.
            if r.status_code == 429 or 500 <= r.status_code < 600:
                last_err = f"HTTP {r.status_code} on {url} params={params}"
                self._sleep_backoff(attempt, retry_after=r.headers.get("Retry-After"))
                continue

            raise PolyAPIError(
                f"HTTP {r.status_code} on {url} params={params}: {r.text[:200]}"
            )

        raise PolyAPIError(
            f"exhausted {self.cfg.http_max_retries} retries on {url}: {last_err}"
        )

    @staticmethod
    def _decode_json(r: requests.Response, url: str, params: dict) -> Any:
        try:
            return r.json()
        except ValueError as e:
            raise PolyAPIError(f"non-JSON body from {url} params={params}: {e}") from e

    @staticmethod
    def _check_shape(data: Any, expect: str, url: str, params: dict) -> None:
        if expect == "list" and not isinstance(data, list):
            raise PolyAPIError(f"expected JSON array from {url} params={params}, got {type(data).__name__}")
        if expect == "dict" and not isinstance(data, dict):
            raise PolyAPIError(f"expected JSON object from {url} params={params}, got {type(data).__name__}")

    def _sleep_backoff(self, attempt: int, retry_after: Optional[str] = None) -> None:
        """Exponential backoff with jitter; honour Retry-After if the server sends one."""
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 30.0))
                return
            except (TypeError, ValueError):
                pass
        delay = self.cfg.http_backoff_base_s * (2 ** attempt)
        time.sleep(delay + random.uniform(0.0, 0.25 * delay))

    # --- Data API: paginated trades ---------------------------------------- #
    def get_trades(self, market: str, type: str = "TRADE", **q: Any) -> list[dict]:
        """Pull the FULL observable TRADE history for one market (conditionId).

        Pages limit/offset to completion within the Data API's hard ceilings
        (page<=1000, offset<=3000 → at most ~4000 most-recent records). Stops on the
        first short page (len < limit). De-duplicates on transactionHash defensively.

        Extra keyword args (e.g. side='BUY', sortBy='TIMESTAMP') pass straight through.
        Per notes §1.4 do NOT pass multiple conditionIds (CSV → HTTP 408); one market only.

        Page size defaults to the API max (1000), NOT config.http_page_limit: the offset
        is hard-capped at 3000 REGARDLESS of page size, so a larger page reaches strictly
        more records (limit=1000 → ~4000 reachable; limit=500 → only ~3500). Callers can
        still override `limit` explicitly if they want smaller pages.
        """
        if not market:
            raise ValueError("get_trades requires a single market (conditionId).")
        limit = min(int(q.pop("limit", _MAX_PAGE) or _MAX_PAGE), _MAX_PAGE)

        records: list[dict] = []
        seen: set[str] = set()
        offset = 0
        while True:
            params = {"market": market, "type": type, "limit": limit, "offset": offset, **q}
            page = self._request(DATA, "data", "/trades", params, expect="list")
            if not page:
                break  # genuine end of data
            for rec in page:
                tx = rec.get("transactionHash")
                key = tx if tx else f"_noidx_{offset}_{len(records)}"
                if key in seen:
                    continue
                seen.add(key)
                records.append(rec)
            if len(page) < limit:
                break  # short page == last page (termination rule, never trust [])
            offset += limit
            if offset > _MAX_OFFSET:
                # Documented hard ceiling: deeper history is unreachable via offset.
                break
        return records

    # --- Data API: per-wallet activity (FORWARD collector) ----------------- #
    def get_activity(
        self, wallet: str, *, type: str = "TRADE",
        start: Optional[int] = None, end: Optional[int] = None,
        limit: int = _MAX_PAGE, use_cache: bool = False, **q: Any,
    ) -> list[dict]:
        """Per-wallet activity feed (`/activity?user=…`). Unlike `/trades`, this endpoint
        HONOURS `start`/`end` (unix seconds) — see notes/api_schema.md §2 — so it is the
        right tool for the live forward collector's incremental "what's new since the last
        poll" pulls. `user` is REQUIRED.

        Defaults to use_cache=False: the whole point of a poll is to see fresh activity,
        and caching a windowed query would mask new fills. Pages limit/offset to completion
        within the same Data API ceilings as /trades (page<=1000, offset<=3000), stopping on
        the first short page. De-dupes on transactionHash.
        """
        if not wallet:
            raise ValueError("get_activity requires a wallet (user).")
        limit = min(int(limit or _MAX_PAGE), _MAX_PAGE)
        base_params: dict[str, Any] = {"user": wallet, "type": type, **q}
        if start is not None:
            base_params["start"] = int(start)
        if end is not None:
            base_params["end"] = int(end)

        records: list[dict] = []
        seen: set[str] = set()
        offset = 0
        while True:
            params = {**base_params, "limit": limit, "offset": offset}
            page = self._request(DATA, "data", "/activity", params,
                                 use_cache=use_cache, expect="list")
            if not page:
                break
            for rec in page:
                tx = rec.get("transactionHash")
                key = tx if tx else f"_noidx_{offset}_{len(records)}"
                if key in seen:
                    continue
                seen.add(key)
                records.append(rec)
            if len(page) < limit:
                break
            offset += limit
            if offset > _MAX_OFFSET:
                break
        return records

    # --- CLOB: live midpoint (FORWARD marks + favorite-at-entry) ------------ #
    def get_clob_midpoint(self, token_id: str, *, use_cache: bool = False) -> Optional[float]:
        """Current CLOB mid for one outcome token, or None if no live book.

        Returns float(mid) from `/midpoint?token_id=…`. A resolved/closed market has no
        order book, so the endpoint may 404/400 or return an empty/garbled body — we treat
        ANY such failure as "no live mid" (None) rather than raising, because the collector
        falls back to the settled 0/1 mark in that case. Uncached by default (marks must be
        read fresh at each horizon).
        """
        if not token_id:
            return None
        try:
            data = self._request(CLOB, "clob", "/midpoint", {"token_id": str(token_id)},
                                 use_cache=use_cache, expect="dict")
        except PolyAPIError:
            return None
        try:
            return float(data.get("mid"))
        except (TypeError, ValueError):
            return None

    # --- CLOB: historical price series (RETROSPECTIVE marks) --------------- #
    def get_price_history(self, token_id: str, *, start_ts: int, end_ts: int,
                          fidelity: int = 1, use_cache: bool = False) -> list[dict]:
        """Historical mid-price time series for one outcome token over [start_ts, end_ts].

        Returns a list of {"t": unix_seconds, "p": price} ascending in time. `fidelity` is
        the sampling interval in MINUTES (1 = minute resolution). Unlike the order BOOK,
        this series PERSISTS for days after a market resolves (probed 2026-06-07: a market
        resolved ~6 days earlier still returned its full history), which is exactly what lets
        the forward experiment reconstruct each holding-horizon mark retrospectively — no live
        polling needed at the horizon. Returns [] on any failure (e.g. a token with no series).
        """
        if not token_id:
            return []
        params = {"market": str(token_id), "startTs": int(start_ts),
                  "endTs": int(end_ts), "fidelity": int(fidelity)}
        try:
            data = self._request(CLOB, "clob", "/prices-history", params,
                                 use_cache=use_cache, expect="dict")
        except PolyAPIError:
            return []
        hist = data.get("history") if isinstance(data, dict) else None
        return hist if isinstance(hist, list) else []

    # --- Gamma: market resolution ------------------------------------------ #
    def get_market_gamma(self, condition_id: str, *, fresh: bool = False) -> dict:
        """Fetch the Gamma market record for a conditionId (resolution source of truth).

        Uses /markets?condition_ids=<id>&closed=true (the only reliable way to fetch a
        RESOLVED market — the default scope hides closed markets, and misspelled filter
        params are silently ignored). Returns the single matching market dict.

        fresh=True bypasses the on-disk cache: the forward collector polls markets that are
        still OPEN and must see the closed==true / outcomePrices transition the moment it
        happens, which a cached record would hide.
        """
        if not condition_id:
            raise ValueError("get_market_gamma requires a conditionId.")
        params = {"condition_ids": condition_id, "closed": "true", "limit": 100}
        data = self._request(GAMMA, "gamma", "/markets", params,
                             use_cache=not fresh, expect="list")
        matches = [m for m in data if m.get("conditionId") == condition_id]
        if not matches:
            # Fall back to including open markets (in case it is not yet closed).
            params_open = {"condition_ids": condition_id, "limit": 100}
            data2 = self._request(GAMMA, "gamma", "/markets", params_open,
                                  use_cache=not fresh, expect="list")
            matches = [m for m in data2 if m.get("conditionId") == condition_id]
        if not matches:
            raise PolyAPIError(f"no Gamma market found for conditionId {condition_id}")
        return matches[0]

    # --- Gamma: resolved-binary universe lister ---------------------------- #
    def list_resolved_binary_markets(
        self,
        *,
        min_volume_usd: float,
        start_unix: int,
        end_unix: int,
        require_order_book: bool = True,
        max_markets: int,
        page_budget: int = 60,
    ) -> list[dict]:
        """Enumerate RESOLVED BINARY sub-markets for the universe (spec §7.7, Phase 3).

        Paginates Gamma /markets with closed=true, order=volumeNum, ascending=false,
        limit=500, stepping offset by the page size. A market is KEPT iff ALL hold:
          * exactly 2 parsed outcomes (binary; negRisk events bundle many such
            distinct-conditionId sub-markets — select at sub-market level, §3.6),
          * parsed outcomePrices == {"1","0"} (settled: one "1", one "0", §3.4),
          * enableOrderBook is true (only enforced when require_order_book),
          * volumeNum >= min_volume_usd,
          * closedTime parses to a unix second in [start_unix, end_unix) (actual
            SETTLEMENT time — the universe window, NOT endDate).

        Stops as soon as `max_markets` are collected OR `page_budget` pages are read.
        Returns a list of plain dicts (cache reused via PolyClient._request) carrying:
          conditionId, slug, question, volumeNum, closed_time_unix, outcomes,
          outcomePrices, winning_index, clobTokenIds.

        Ordering by descending volume means the highest-volume resolved binaries are
        returned first; the volume floor is also applied per-market so a market that
        slips under the floor mid-page is still dropped.
        """
        if start_unix >= end_unix:
            raise ValueError(
                f"start_unix ({start_unix}) must be < end_unix ({end_unix})."
            )
        if max_markets <= 0:
            raise ValueError("max_markets must be >= 1.")

        # Gamma /markets caps a page at 100 regardless of the `limit` value, and 422s
        # once `offset` exceeds ~20k. So we step `offset` by the ACTUAL returned count
        # (never by an assumed page size), stop on the offset-ceiling 422, and bound
        # the scan by both page_budget and a safe max offset.
        page_limit = 100
        max_offset = 19_000
        kept: list[dict] = []
        seen_conditions: set[str] = set()

        offset = 0
        for _ in range(max(0, int(page_budget))):
            if offset > max_offset:
                break  # Gamma 422s past ~20k offset; stop before hitting it
            params = {
                "closed": "true",
                "order": "volumeNum",
                "ascending": "false",
                "limit": page_limit,
                "offset": offset,
            }
            try:
                page = self._request(GAMMA, "gamma", "/markets", params, expect="list")
            except PolyAPIError:
                break  # Gamma offset ceiling (HTTP 422) — stop paginating gracefully
            if not page:
                break  # exhausted the closed-market listing

            for market in page:
                kept_market = self._accept_resolved_binary(
                    market,
                    min_volume_usd=min_volume_usd,
                    start_unix=start_unix,
                    end_unix=end_unix,
                    require_order_book=require_order_book,
                    seen=seen_conditions,
                )
                if kept_market is not None:
                    kept.append(kept_market)
                    if len(kept) >= max_markets:
                        return kept

            offset += len(page)          # step by ACTUAL count (robust when page<limit)
            if len(page) < page_limit:
                break  # genuinely the last (short) page

        return kept

    @staticmethod
    def _accept_resolved_binary(
        market: dict,
        *,
        min_volume_usd: float,
        start_unix: int,
        end_unix: int,
        require_order_book: bool,
        seen: set[str],
    ) -> Optional[dict]:
        """Return a normalised universe dict if `market` passes every filter, else None.

        Pure predicate over one Gamma market record (no I/O). De-dupes on conditionId
        via `seen` (mutated) so paging overlaps never double-count a market.
        """
        cond = market.get("conditionId")
        if not cond or cond in seen:
            return None

        outcomes = [str(x) for x in _parse_json_array(market.get("outcomes"))]
        if len(outcomes) != 2:
            return None  # binary sub-markets only (negRisk events: §3.6)

        raw_prices = _parse_json_array(market.get("outcomePrices"))
        price_strs = {str(p).strip() for p in raw_prices}
        if price_strs != {"1", "0"}:
            return None  # settled iff exactly one "1" and one "0" (§3.4)

        if require_order_book and not bool(market.get("enableOrderBook")):
            return None

        try:
            volume = float(market.get("volumeNum"))
        except (TypeError, ValueError):
            return None
        if volume < float(min_volume_usd):
            return None

        closed_time_unix = _parse_iso_to_unix(market.get("closedTime"))
        if closed_time_unix is None:
            return None
        if not (start_unix <= closed_time_unix < end_unix):
            return None

        # winning_index = position of the "1" among the parsed outcomePrices.
        winning_index = next(
            (i for i, p in enumerate(raw_prices) if str(p).strip() == "1"), None
        )

        seen.add(cond)
        return {
            "conditionId": cond,
            "slug": market.get("slug"),
            "question": market.get("question"),
            "volumeNum": volume,
            "closed_time_unix": closed_time_unix,
            "outcomes": outcomes,
            "outcomePrices": [str(p) for p in raw_prices],
            "winning_index": winning_index,
            "clobTokenIds": [str(x) for x in _parse_json_array(market.get("clobTokenIds"))],
        }


# --- pure parsers (no I/O) --------------------------------------------------- #
def _parse_json_array(raw: Any) -> list:
    """Gamma encodes outcomes/outcomePrices/clobTokenIds as JSON strings; parse safely."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _parse_iso_to_unix(raw: Any) -> Optional[int]:
    """Parse a Gamma ISO-8601 timestamp (e.g. closedTime) to unix SECONDS, or None.

    Tolerates: None/empty, a trailing "Z" or a bare "+00" offset (which
    datetime.fromisoformat rejects on some Python versions → normalise to "+00:00"),
    and naive strings (assumed UTC). Never raises — returns None on anything unparseable
    so callers can treat a missing/garbled closedTime as "no settlement time known".
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Normalise UTC designators datetime.fromisoformat may not accept directly.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    elif s.endswith("+00"):
        s = s[:-3] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def resolution_from_gamma(market: dict) -> ResolutionInfo:
    """Derive the winning outcomeIndex from a Gamma market's settled outcomePrices.

    A binary market is RESOLVED iff closed==true AND outcomePrices is exactly one
    "1" and one "0". The winning index is the position of the "1". No look-ahead
    concern here: resolution is the ground-truth label we attach AFTER entry (R1).
    """
    cond = market.get("conditionId", "")
    outcomes = [str(x) for x in _parse_json_array(market.get("outcomes"))]
    raw_prices = _parse_json_array(market.get("outcomePrices"))
    clob_ids = [str(x) for x in _parse_json_array(market.get("clobTokenIds"))]

    prices: list[float] = []
    for p in raw_prices:
        try:
            prices.append(float(p))
        except (TypeError, ValueError):
            prices.append(float("nan"))

    closed = bool(market.get("closed"))
    # Settled binary: one price is 1.0 and the other 0.0 (within float tolerance).
    rounded = [round(v) for v in prices if v == v]  # drop NaN
    is_settled = (
        closed
        and len(prices) == 2
        and sorted(rounded) == [0, 1]
    )
    winning_index: Optional[int] = None
    if is_settled:
        winning_index = max(range(len(prices)), key=lambda i: prices[i])

    closed_time_unix = _parse_iso_to_unix(market.get("closedTime"))

    return ResolutionInfo(
        condition_id=cond,
        resolved=is_settled,
        winning_index=winning_index,
        outcomes=outcomes,
        outcome_prices=prices,
        clob_token_ids=clob_ids,
        closed_time_unix=closed_time_unix,
    )


# Module-level convenience instance + thin wrappers (typed helpers per the spec).
_default_client: Optional[PolyClient] = None


def _client() -> PolyClient:
    global _default_client
    if _default_client is None:
        _default_client = PolyClient()
    return _default_client


def get_trades(market: str, type: str = "TRADE", **q: Any) -> list[dict]:
    """Module-level helper: full TRADE history for a conditionId (see PolyClient.get_trades)."""
    return _client().get_trades(market, type=type, **q)


def get_market_gamma(condition_id: str, *, fresh: bool = False) -> dict:
    """Module-level helper: Gamma resolution record for a conditionId."""
    return _client().get_market_gamma(condition_id, fresh=fresh)


def get_activity(wallet: str, **q: Any) -> list[dict]:
    """Module-level helper: per-wallet /activity feed (see PolyClient.get_activity)."""
    return _client().get_activity(wallet, **q)


def get_clob_midpoint(token_id: str, **q: Any) -> Optional[float]:
    """Module-level helper: live CLOB mid for a token (see PolyClient.get_clob_midpoint)."""
    return _client().get_clob_midpoint(token_id, **q)


def get_price_history(token_id: str, **q: Any) -> list[dict]:
    """Module-level helper: historical price series for a token (see PolyClient.get_price_history)."""
    return _client().get_price_history(token_id, **q)


def list_resolved_binary_markets(
    *,
    min_volume_usd: float,
    start_unix: int,
    end_unix: int,
    require_order_book: bool = True,
    max_markets: int,
    page_budget: int = 60,
) -> list[dict]:
    """Module-level helper: resolved-binary universe (see PolyClient.list_resolved_binary_markets)."""
    return _client().list_resolved_binary_markets(
        min_volume_usd=min_volume_usd,
        start_unix=start_unix,
        end_unix=end_unix,
        require_order_book=require_order_book,
        max_markets=max_markets,
        page_budget=page_budget,
    )
