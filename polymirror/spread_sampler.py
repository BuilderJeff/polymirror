"""spread_sampler.py — Phase-1 spread calibrator (spec §5.2, R6).

Read-only. Pulls a coverage sample of CURRENTLY-ACTIVE order-book markets from
Gamma, then reads live CLOB /midpoint and /spread per outcome token. Builds an
empirical table of (token, midpoint, spread_cents, market_volume_usd) spanning
LIQUID (high volume, prices near mid) and THIN (low volume, prices near 0/1)
contracts. Used to calibrate the MODELLED SpreadPreset numbers in config.py.

NEVER places a trade, never needs auth (R0). Gentle pacing + exponential backoff
+ on-disk cache under data/cache/ so reruns are reproducible (R8).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "cache"
RAW_DIR = ROOT / "data" / "raw"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Pacing / backoff (be gentle; §9).
_BACKOFF_BASE_S = 0.6
_MAX_RETRIES = 5
_INTER_REQUEST_S = 0.12


def _get(url: str, params: dict, *, cache_key: Optional[str] = None) -> dict | list:
    """GET with on-disk cache + exponential backoff on 429/5xx."""
    if cache_key is not None:
        cf = CACHE_DIR / f"{cache_key}.json"
        if cf.exists():
            return json.loads(cf.read_text())
    last_err: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"retryable {r.status_code}")
            r.raise_for_status()
            data = r.json()
            if cache_key is not None:
                (CACHE_DIR / f"{cache_key}.json").write_text(json.dumps(data))
            time.sleep(_INTER_REQUEST_S)
            return data
        except Exception as e:  # noqa: BLE001 — deliberate retry boundary
            last_err = e
            sleep_s = _BACKOFF_BASE_S * (2 ** attempt)
            time.sleep(sleep_s)
    raise RuntimeError(f"GET {url} failed after {_MAX_RETRIES} retries: {last_err}")


def _to_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class TokenSample:
    question: str
    slug: str
    condition_id: str
    token_id: str
    outcome: str
    market_volume_usd: float
    volume24hr_usd: float
    liquidity_usd: float
    midpoint: float
    spread_cents: float          # CLOB /spread, in cents
    best_bid: Optional[float]
    best_ask: Optional[float]
    # price level = distance toward a boundary; 0 at mid, ->1 near 0/1.
    boundary_closeness: float    # = 1 - 4*p*(1-p)


def fetch_market_pool(pages: int = 6, page_size: int = 100) -> list[dict]:
    """Pull a broad pool of active, order-book, non-closed markets.

    Pages ordered by volume desc to guarantee high-liquidity coverage, plus a
    separate ascending sweep is unnecessary because thin contracts surface via
    the low-volume tail of the same volume-sorted pool and via near-boundary
    price selection in build_sample().
    """
    pool: list[dict] = []
    for order, asc in (("volume24hr", "false"), ("volume24hr", "true")):
        for p in range(pages):
            key = f"gamma_markets_{order}_{asc}_p{p}"
            batch = _get(
                f"{GAMMA}/markets",
                {
                    "limit": page_size,
                    "offset": p * page_size,
                    "active": "true",
                    "closed": "false",
                    "enableOrderBook": "true",
                    "order": order,
                    "ascending": asc,
                },
                cache_key=key,
            )
            if not isinstance(batch, list) or not batch:
                break
            pool.extend(batch)
    # De-dupe by market id.
    seen: set = set()
    uniq: list[dict] = []
    for m in pool:
        mid = m.get("id")
        if mid in seen:
            continue
        seen.add(mid)
        uniq.append(m)
    return uniq


def _read_clob_token(token_id: str) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Return (midpoint, spread_cents, best_bid, best_ask) for a token."""
    mp = _get(f"{CLOB}/midpoint", {"token_id": token_id}, cache_key=f"clob_mid_{token_id}")
    sp = _get(f"{CLOB}/spread", {"token_id": token_id}, cache_key=f"clob_spread_{token_id}")
    midpoint = _to_float(mp.get("mid")) if isinstance(mp, dict) else None
    spread_dollars = _to_float(sp.get("spread")) if isinstance(sp, dict) else None
    spread_cents = spread_dollars * 100 if spread_dollars is not None else None
    # Book for best bid/ask (sanity vs /spread). best bid = max bid, ask = min ask.
    best_bid = best_ask = None
    try:
        bk = _get(f"{CLOB}/book", {"token_id": token_id}, cache_key=f"clob_book_{token_id}")
        if isinstance(bk, dict):
            bids = [_to_float(b.get("price")) for b in bk.get("bids", []) or []]
            asks = [_to_float(a.get("price")) for a in bk.get("asks", []) or []]
            bids = [b for b in bids if b is not None]
            asks = [a for a in asks if a is not None]
            best_bid = max(bids) if bids else None
            best_ask = min(asks) if asks else None
    except Exception:  # noqa: BLE001 — book is sanity-only, non-fatal
        pass
    return midpoint, spread_cents, best_bid, best_ask


# Stratification thresholds. LIQUID = market volume above this; THIN below.
LIQUID_VOLUME_USD = 50_000.0
# Price bands by Yes-mid: contested mid vs near-boundary.
MID_BAND = (0.20, 0.80)        # "near-mid" contracts
# Degenerate-quote guard: a contract with no real two-sided book (e.g. 0.01/0.99
# placeholder on a zero-liquidity market) is NOT a spread observation (R6).
MAX_PLAUSIBLE_SPREAD_CENTS = 25.0


def _candidate_from_market(m: dict) -> Optional[dict]:
    """Parse a Gamma market into a binary candidate, or None if unusable."""
    try:
        outcomes = json.loads(m.get("outcomes") or "[]")
        tok_ids = json.loads(m.get("clobTokenIds") or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    if len(outcomes) != 2 or len(tok_ids) != 2:
        return None
    vol = _to_float(m.get("volumeNum")) or _to_float(m.get("volume")) or 0.0
    op = m.get("outcomePrices")
    try:
        p0 = float(json.loads(op)[0]) if op else None
    except (TypeError, ValueError, json.JSONDecodeError):
        p0 = None
    return {
        "m": m, "outcomes": outcomes, "tok_ids": tok_ids,
        "vol": vol, "p0": p0,
    }


def _sample_token(c: dict, outcome_idx: int) -> Optional[TokenSample]:
    """Read CLOB for one outcome token and build a TokenSample, or None."""
    m = c["m"]
    oc = c["outcomes"][outcome_idx]
    tid = str(c["tok_ids"][outcome_idx])
    midpoint, spread_cents, bb, ba = _read_clob_token(tid)
    if midpoint is None or spread_cents is None:
        return None
    if midpoint <= 0.0 or midpoint >= 1.0:
        return None  # degenerate fully-settled quote
    # Guard against placeholder books (e.g. 1c/99c on a dead market): a spread
    # that wide on a contract with ~0 volume is a modelling artefact, not a real
    # observation. Drop it so it cannot poison calibration (R6).
    if spread_cents > MAX_PLAUSIBLE_SPREAD_CENTS and c["vol"] < LIQUID_VOLUME_USD:
        return None
    closeness = 1.0 - 4.0 * midpoint * (1.0 - midpoint)
    return TokenSample(
        question=m.get("question", "")[:90],
        slug=m.get("slug", ""),
        condition_id=m.get("conditionId", ""),
        token_id=tid,
        outcome=oc,
        market_volume_usd=c["vol"],
        volume24hr_usd=_to_float(m.get("volume24hr")) or 0.0,
        liquidity_usd=_to_float(m.get("liquidityNum")) or _to_float(m.get("liquidity")) or 0.0,
        midpoint=midpoint,
        spread_cents=spread_cents,
        best_bid=bb,
        best_ask=ba,
        boundary_closeness=closeness,
    )


def _stratum(vol: float, p0: Optional[float]) -> str:
    liquid = vol >= LIQUID_VOLUME_USD
    near_mid = p0 is not None and MID_BAND[0] <= p0 <= MID_BAND[1]
    tier = "liquid" if liquid else "thin"
    band = "mid" if near_mid else "boundary"
    return f"{tier}_{band}"


def build_sample(target_n: int = 24, per_stratum: int = 6) -> list[TokenSample]:
    """Build a coverage sample explicitly spanning four strata:

        liquid_mid, liquid_boundary, thin_mid, thin_boundary

    Liquid contracts are read for both outcomes (the Yes-near-0 side gives a
    boundary observation for free). Thin contracts add the low-volume tail.
    """
    pool = fetch_market_pool()
    # Parse + bucket candidates by stratum, ordered within each by volume.
    by_stratum: dict[str, list[dict]] = {}
    seen_cond: set = set()
    for m in pool:
        c = _candidate_from_market(m)
        if c is None:
            continue
        cond = m.get("conditionId")
        if cond in seen_cond:
            continue
        seen_cond.add(cond)
        s = _stratum(c["vol"], c["p0"])
        by_stratum.setdefault(s, []).append(c)
    for s in by_stratum:
        # Liquid: highest volume first. Thin: still highest first for real books.
        by_stratum[s].sort(key=lambda c: c["vol"], reverse=True)

    samples: list[TokenSample] = []
    seen_tok: set = set()

    def add(ts: Optional[TokenSample]) -> bool:
        if ts is None or ts.token_id in seen_tok:
            return False
        seen_tok.add(ts.token_id)
        samples.append(ts)
        return True

    # Walk each stratum, reading CLOB until we hit per_stratum good samples.
    for stratum in ("liquid_mid", "liquid_boundary", "thin_mid", "thin_boundary"):
        cands = by_stratum.get(stratum, [])
        got = 0
        for c in cands:
            if got >= per_stratum:
                break
            # For liquid_mid read outcome 0 (the contested Yes). For boundary
            # strata, read whichever outcome sits nearer a boundary.
            idxs = (0, 1) if stratum.endswith("boundary") else (0,)
            for oi in idxs:
                if add(_sample_token(c, oi)):
                    got += 1
                    if got >= per_stratum:
                        break
    return samples


if __name__ == "__main__":
    import csv
    import statistics

    samples = build_sample(target_n=24, per_stratum=6)
    print(f"Collected {len(samples)} token samples.")
    out_csv = RAW_DIR / "spread_samples.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "stratum", "token_id", "outcome", "midpoint", "spread_cents",
            "best_bid", "best_ask", "market_volume_usd", "volume24hr_usd",
            "liquidity_usd", "boundary_closeness", "question",
        ])
        for s in samples:
            w.writerow([
                _stratum(s.market_volume_usd,
                         s.midpoint if s.outcome.lower() in ("yes", "up", "over", "down", "no") else s.midpoint),
                s.token_id, s.outcome, f"{s.midpoint:.4f}", f"{s.spread_cents:.3f}",
                "" if s.best_bid is None else f"{s.best_bid:.4f}",
                "" if s.best_ask is None else f"{s.best_ask:.4f}",
                f"{s.market_volume_usd:.0f}", f"{s.volume24hr_usd:.0f}",
                f"{s.liquidity_usd:.0f}", f"{s.boundary_closeness:.3f}", s.question,
            ])
    print(f"Wrote {out_csv}")

    # Per-stratum + per-price-band summary for the notes file.
    def band(p: float) -> str:
        return "mid(0.20-0.80)" if MID_BAND[0] <= p <= MID_BAND[1] else "boundary(<0.20 or >0.80)"

    print("\n--- spread_cents by (tier, price band) ---")
    groups: dict[str, list[float]] = {}
    for s in samples:
        tier = "liquid" if s.market_volume_usd >= LIQUID_VOLUME_USD else "thin"
        groups.setdefault(f"{tier} | {band(s.midpoint)}", []).append(s.spread_cents)
    for k in sorted(groups):
        v = groups[k]
        print(f"  {k:38s} n={len(v):2d}  "
              f"min={min(v):.2f} med={statistics.median(v):.2f} "
              f"max={max(v):.2f} mean={statistics.mean(v):.2f}")
    allv = [s.spread_cents for s in samples]
    print(f"\n  ALL n={len(allv)} median={statistics.median(allv):.3f} "
          f"mean={statistics.mean(allv):.3f} min={min(allv):.3f} max={max(allv):.3f}")
