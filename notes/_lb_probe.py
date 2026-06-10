"""Probe candidate Polymarket leaderboard endpoints (read-only). Prints status +
a tiny sample so we can see which URL/param shape returns ranked wallets."""
from __future__ import annotations
import json
import requests

CANDIDATES = [
    ("https://lb-api.polymarket.com/leaderboard", {"window": "all", "limit": 5}),
    ("https://lb-api.polymarket.com/profit",      {"window": "all", "limit": 5}),
    ("https://lb-api.polymarket.com/volume",      {"window": "all", "limit": 5}),
    ("https://lb-api.polymarket.com/profit",      {"window": "1m", "limit": 5}),
    ("https://lb-api.polymarket.com/volume",      {"window": "1m", "limit": 5}),
    ("https://data-api.polymarket.com/leaderboard", {"window": "all", "limit": 5}),
    ("https://gamma-api.polymarket.com/leaderboard", {"limit": 5}),
    ("https://data-api.polymarket.com/profit",    {"window": "all", "limit": 5}),
]

HEADERS = {"User-Agent": "Mozilla/5.0 (research backtest; read-only)"}

for url, params in CANDIDATES:
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        ct = r.headers.get("content-type", "")
        body = r.text[:300]
        sample = body
        if "json" in ct:
            try:
                j = r.json()
                sample = json.dumps(j, indent=0)[:400]
            except Exception:
                pass
        print(f"[{r.status_code}] {url}  {params}\n   ct={ct}\n   {sample}\n")
    except Exception as e:
        print(f"[ERR] {url}  {params}  -> {type(e).__name__}: {e}\n")
