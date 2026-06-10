"""Probe CLOB endpoints (book/spread/midpoint/prices-history) + Data API rate-limit burst.
Read-only. Run: ./.venv/Scripts/python.exe data/probe_clob_rate.py
"""
from __future__ import annotations
import json, time, pathlib, requests

CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
CACHE = pathlib.Path(__file__).parent / "cache" / "probe_clob"
CACHE.mkdir(parents=True, exist_ok=True)
s = requests.Session()
s.headers.update({"User-Agent": "polymirror-schema-probe/1.0"})

# CLOB token id (YES side of Trump-2024).
TOKEN = "21742633143463906290569050155826241533067272736897614950488156847949938836455"


def dump(name, obj):
    (CACHE / f"{name}.json").write_text(json.dumps(obj, indent=2, default=str))


print("=" * 70)
print("CLOB /book")
r = s.get(f"{CLOB}/book", params={"token_id": TOKEN}, timeout=30)
print("status", r.status_code)
b = r.json()
dump("book", b)
print("keys:", sorted(b.keys()) if isinstance(b, dict) else type(b))
if isinstance(b, dict):
    for k in ("market", "asset_id", "timestamp", "hash"):
        print(f"  {k}: {str(b.get(k))[:60]}")
    print("  bids sample:", b.get("bids", [])[:2])
    print("  asks sample:", b.get("asks", [])[:2])

print("\n" + "=" * 70)
print("CLOB /spread")
r = s.get(f"{CLOB}/spread", params={"token_id": TOKEN}, timeout=30)
print("status", r.status_code, "body:", r.text[:200])
dump("spread", r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)

print("\n" + "=" * 70)
print("CLOB /midpoint")
r = s.get(f"{CLOB}/midpoint", params={"token_id": TOKEN}, timeout=30)
print("status", r.status_code, "body:", r.text[:200])
dump("midpoint", r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)

print("\n" + "=" * 70)
print("CLOB /prices-history")
# fidelity in minutes; interval is a coarse preset (max/1m/1w/1d). Use ts range.
now = int(time.time())
r = s.get(f"{CLOB}/prices-history",
          params={"market": TOKEN, "interval": "max", "fidelity": 60}, timeout=30)
print("status", r.status_code)
ph = r.json()
dump("prices_history", ph)
print("keys:", sorted(ph.keys()) if isinstance(ph, dict) else type(ph))
if isinstance(ph, dict):
    hist = ph.get("history", [])
    print("  history len:", len(hist))
    print("  first point:", hist[0] if hist else None)
    print("  last point:", hist[-1] if hist else None)

# also test startTs/endTs form
r2 = s.get(f"{CLOB}/prices-history",
           params={"market": TOKEN, "startTs": now - 30 * 86400, "endTs": now, "fidelity": 180}, timeout=30)
print("  startTs/endTs form status:", r2.status_code,
      "history len:", len(r2.json().get("history", [])) if r2.status_code == 200 else r2.text[:80])

print("\n" + "=" * 70)
print("DATA API rate-limit burst: 30 rapid /trades requests")
codes = []
headers_seen = {}
t0 = time.time()
for i in range(30):
    rr = s.get(f"{DATA}/trades", params={"limit": 1}, timeout=15)
    codes.append(rr.status_code)
    if i == 0:
        # capture rate-limit-ish headers from first response
        headers_seen = {k: v for k, v in rr.headers.items()
                        if any(t in k.lower() for t in ("rate", "limit", "retry", "remaining", "reset"))}
dt = time.time() - t0
from collections import Counter
print(f"30 requests in {dt:.2f}s ({30/dt:.1f} req/s)")
print("status codes:", dict(Counter(codes)))
print("rate-limit headers present:", headers_seen or "NONE")
if 429 in codes:
    print("  FIRST 429 at request index:", codes.index(429))

print("\nDONE. Cached under", CACHE)
