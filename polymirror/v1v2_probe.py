"""v1v2_probe.py — empirical investigation of the CTF Exchange V2 migration boundary.

Spec 9 gotcha: ~2026-04-28 Polymarket migrated to CTF Exchange V2 (new contracts,
pUSD collateral, deprecated subgraphs). This module determines EMPIRICALLY whether
historical trade ACTIVITY is observable continuously on BOTH sides of that boundary.

R0 compliant: read-only data collection. No keys, no wallet, no live trades.

Two complementary probes:

  Probe A (Data API /trades reach):
    The Data API /trades endpoint ignores every time-window param and hard-caps
    pagination at offset 3000. So /trades only ever returns the most-recent ~3000
    trades per market (or platform-wide). It CANNOT be windowed to March 2026.
    We document this empirically rather than pretend it gives a weekly series.

  Probe B (CLOB prices-history weekly activity, the usable continuity series):
    CLOB /prices-history DOES accept time windows (interval=max + fidelity=1440
    daily, or chunked startTs/endTs). Each price point exists only where there was
    book activity, so daily-point density is a proxy for trade activity. We bucket
    points into ISO weeks across ~2026-03 .. 2026-06 for several boundary-spanning
    high-volume markets and look for a cliff around 2026-04-28.

Run:  ./.venv/Scripts/python.exe -m polymirror.v1v2_probe
"""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

import requests

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
MIGRATION = dt.datetime(2026, 4, 28, tzinfo=dt.timezone.utc)
WINDOW_START = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)
WINDOW_END = dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc)

HTTP_MAX_RETRIES = 5
HTTP_BACKOFF_BASE_S = 0.75
OFFSET_HARD_CAP = 3000  # empirically: Data API rejects offset > 3000


def _utc(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%d")


def _cache_path(tag: str, params: dict[str, Any]) -> Path:
    key = tag + "|" + json.dumps(params, sort_keys=True, default=str)
    h = hashlib.sha256(key.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{tag}_{h}.json"


def get_json(
    url: str, params: dict[str, Any], *, tag: str, use_cache: bool = True
) -> tuple[int, Any]:
    """GET with disk cache and exponential backoff on 429/5xx.

    Returns (status_code, parsed_body_or_None). 4xx (e.g. offset cap) is NOT
    retried and is returned so the caller can observe the structural limit.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp = _cache_path(tag, {"url": url, **params})
    if use_cache and cp.exists():
        payload = json.loads(cp.read_text())
        return payload["status"], payload["body"]

    last_exc: Optional[Exception] = None
    for attempt in range(HTTP_MAX_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code in (429,) or r.status_code >= 500:
                time.sleep(HTTP_BACKOFF_BASE_S * (2**attempt))
                continue
            body = r.json() if r.headers.get("content-type", "").startswith(
                "application/json"
            ) or r.text.startswith(("[", "{")) else None
            if use_cache:
                cp.write_text(json.dumps({"status": r.status_code, "body": body}))
            return r.status_code, body
        except Exception as e:  # noqa: BLE001 — network boundary, log + backoff
            last_exc = e
            time.sleep(HTTP_BACKOFF_BASE_S * (2**attempt))
    raise RuntimeError(f"GET {url} failed after {HTTP_MAX_RETRIES} retries: {last_exc}")


# --------------------------------------------------------------------------- #
# Probe A — Data API /trades structural reach                                 #
# --------------------------------------------------------------------------- #
def probe_trades_reach(condition_id: str, label: str) -> dict[str, Any]:
    """Walk /trades offset pagination until the hard cap; report reachable span."""
    off, lim, all_ts = 0, 500, []
    stop_reason = "exhausted"
    while True:
        status, body = get_json(
            f"{DATA}/trades",
            {"limit": lim, "market": condition_id, "offset": off},
            tag="trades",
        )
        if status != 200:
            stop_reason = f"http_{status}"
            break
        if not body:
            stop_reason = "empty_page"
            break
        all_ts += [t["timestamp"] for t in body]
        off += lim
        if len(body) < lim:
            stop_reason = "last_partial_page"
            break
        if off > OFFSET_HARD_CAP:
            stop_reason = "offset_cap"
            break
    span = (min(all_ts), max(all_ts)) if all_ts else None
    return {
        "label": label,
        "condition_id": condition_id,
        "reachable_trades": len(all_ts),
        "ts_span": [_iso(span[0]), _iso(span[1])] if span else None,
        "stop_reason": stop_reason,
    }


def _iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# Probe B — CLOB prices-history weekly activity across the boundary           #
# --------------------------------------------------------------------------- #
def weekly_activity(clob_token_id: str) -> dict[str, int]:
    """Daily-fidelity price points bucketed into ISO weeks (activity proxy)."""
    status, body = get_json(
        f"{CLOB}/prices-history",
        {"market": clob_token_id, "interval": "max", "fidelity": 1440},
        tag="phist",
    )
    if status != 200 or not body:
        return {}
    hist = body.get("history", [])
    counts: dict[str, int] = collections.Counter()
    for p in hist:
        d = dt.datetime.fromtimestamp(p["t"], dt.timezone.utc)
        if WINDOW_START <= d <= WINDOW_END:
            y, w, _ = d.isocalendar()
            counts[f"{y}-W{w:02d}"] += 1
    return dict(sorted(counts.items()))


def fetch_clob_token_id(condition_id: str) -> Optional[str]:
    status, body = get_json(
        f"{GAMMA}/markets", {"limit": 1, "condition_ids": condition_id}, tag="gmarket"
    )
    if status != 200 or not body:
        return None
    raw = body[0].get("clobTokenIds")
    if not raw:
        return None
    toks = json.loads(raw) if isinstance(raw, str) else raw
    return toks[0] if toks else None


def boundary_continuity(clob_token_id: str) -> dict[str, Any]:
    """Daily points just before vs just after MIGRATION; detect any gap/cliff."""
    status, body = get_json(
        f"{CLOB}/prices-history",
        {"market": clob_token_id, "interval": "max", "fidelity": 1440},
        tag="phist",
    )
    if status != 200 or not body:
        return {"ok": False}
    hist = body.get("history", [])
    ts = sorted(p["t"] for p in hist)
    if not ts:
        return {"ok": False}
    first = dt.datetime.fromtimestamp(ts[0], dt.timezone.utc)
    last = dt.datetime.fromtimestamp(ts[-1], dt.timezone.utc)
    # largest gap (days) between consecutive daily points inside the window
    win = [t for t in ts if WINDOW_START.timestamp() <= t <= WINDOW_END.timestamp()]
    max_gap_days = 0.0
    gap_at = None
    for a, b in zip(win, win[1:]):
        g = (b - a) / 86400.0
        if g > max_gap_days:
            max_gap_days = g
            gap_at = _iso(a)
    mig = MIGRATION.timestamp()
    before = sum(1 for t in win if t < mig)
    after = sum(1 for t in win if t >= mig)
    return {
        "ok": True,
        "first_point": _utc(first),
        "last_point": _utc(last),
        "points_before_migration": before,
        "points_after_migration": after,
        "max_gap_days_in_window": round(max_gap_days, 2),
        "max_gap_starts_at": gap_at,
        "spans_boundary": before > 0 and after > 0,
    }


# --------------------------------------------------------------------------- #
# Driver                                                                       #
# --------------------------------------------------------------------------- #
# High-volume markets whose lifetime straddles 2026-04-28 (start before, still
# active or resolved after). Verified via Gamma volumeNum ordering.
SPANNING_MARKETS = [
    ("0x9352c559e9648ab4cab236087b64ca85c5b7123a4c7d9d7d4efde4a39c18056f",
     "Iranian regime fall by June 30 (start 2025-12-17)"),
    ("0x965ebc5d79eb1ec02cad67245a44b9e45b33359018f013fb6cf81d5bbf7bcc8d",
     "Uzbekistan win 2026 World Cup (start 2025-07-02)"),
    ("0x0b4cc3b739e1dfe5d73274740e7308b6fb389c5af040c3a174923d928d134bee",
     "Jesus return before 2027 (start 2025-11-25)"),
]

# Markets that RESOLVED before migration — pure V1-era, used to prove /trades
# does return pre-migration trades (it does, but only the most-recent ~3000).
PRE_MIGRATION_RESOLVED = [
    ("0x7cb525e831729325d651017f81cbcb6f1adde5011c7b2283babea00b4ae93ae7",
     "Netanyahu out by March 31 (ended 2026-03-31)"),
    ("0x4c5701bcde0b8fb7d7f48c8e9d20245a6caa58c61a77f981fad98f2bfa0b1bc7",
     "US x Iran ceasefire by April 7 (ended 2026-04-07)"),
]


def main() -> dict[str, Any]:
    report: dict[str, Any] = {"probe_A_trades_reach": [], "probe_B_weekly": []}

    # Probe A: structural reach of /trades on pre-migration + spanning markets
    for cond, label in PRE_MIGRATION_RESOLVED + SPANNING_MARKETS[:1]:
        report["probe_A_trades_reach"].append(probe_trades_reach(cond, label))

    # Probe B: weekly activity + boundary continuity from prices-history
    for cond, label in SPANNING_MARKETS:
        tok = fetch_clob_token_id(cond)
        entry: dict[str, Any] = {"label": label, "condition_id": cond}
        if tok is None:
            entry["error"] = "no clobTokenId"
        else:
            entry["weekly_activity"] = weekly_activity(tok)
            entry["boundary"] = boundary_continuity(tok)
        report["probe_B_weekly"].append(entry)

    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    main()
