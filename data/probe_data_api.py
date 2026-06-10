"""Probe Data API /trades and /activity: enumerate fields, test every query param,
determine real pagination + default sort. Read-only; caches raw responses.

Run: ./.venv/Scripts/python.exe data/probe_data_api.py
"""
from __future__ import annotations
import json
import time
import pathlib
import requests

DATA = "https://data-api.polymarket.com"
CACHE = pathlib.Path(__file__).parent / "cache" / "probe_data"
CACHE.mkdir(parents=True, exist_ok=True)

# Active wallet from Trump-2024 holders (large position => many trades).
WALLET = "0x614f8c216086a1b7eead36b89b456938406d3b8a"
COND = "0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917"
COND2 = "0x9634720e2a79a983d1770a529475a712b440021fe3aa61fc3c4491e69ef0f120"

session = requests.Session()
session.headers.update({"User-Agent": "polymirror-schema-probe/1.0"})


def get(path: str, params: dict, tag: str):
    """GET with status capture; never raises on HTTP error (we WANT to see 400s)."""
    url = f"{DATA}{path}"
    try:
        r = session.get(url, params=params, timeout=30)
    except Exception as e:
        return {"tag": tag, "ok": False, "error": repr(e), "url": url, "params": params}
    out = {
        "tag": tag,
        "status": r.status_code,
        "final_url": r.url,
        "params": params,
        "n": None,
        "body_head": None,
    }
    try:
        j = r.json()
        if isinstance(j, list):
            out["n"] = len(j)
            out["body_head"] = j[:2]
        else:
            out["n"] = "obj"
            out["body_head"] = j
    except Exception:
        out["body_head"] = r.text[:300]
    # cache raw
    (CACHE / f"{tag}.json").write_text(
        json.dumps({"status": r.status_code, "url": r.url, "body": out["body_head"]}, indent=2, default=str)
    )
    return out


def line(o):
    print(f"[{o.get('status','ERR')}] {o['tag']:<34} n={o.get('n')}  {o['final_url'] if 'final_url' in o else o.get('error','')}")


print("=" * 80)
print("DATA API /trades  -- field enumeration + param tests")
print("=" * 80)

# Baseline: which params are accepted on /trades?
tests_trades = [
    ("trades_user", "/trades", {"user": WALLET, "limit": 3}),
    ("trades_user_market", "/trades", {"user": WALLET, "market": COND, "limit": 3}),
    ("trades_market_only", "/trades", {"market": COND, "limit": 3}),
    ("trades_market_csv2", "/trades", {"market": f"{COND},{COND2}", "limit": 3}),
    ("trades_type_TRADE", "/trades", {"user": WALLET, "type": "TRADE", "limit": 3}),
    ("trades_side_BUY", "/trades", {"user": WALLET, "side": "BUY", "limit": 5}),
    ("trades_side_SELL", "/trades", {"user": WALLET, "side": "SELL", "limit": 5}),
    ("trades_side_lower", "/trades", {"user": WALLET, "side": "buy", "limit": 5}),
    ("trades_filterType_CASH", "/trades", {"user": WALLET, "filterType": "CASH", "filterAmount": 100, "limit": 3}),
    ("trades_filterType_TOKENS", "/trades", {"user": WALLET, "filterType": "TOKENS", "filterAmount": 1, "limit": 3}),
    ("trades_sortBy_TIMESTAMP", "/trades", {"user": WALLET, "sortBy": "TIMESTAMP", "limit": 3}),
    ("trades_sortBy_PRICE", "/trades", {"user": WALLET, "sortBy": "PRICE", "limit": 3}),
    ("trades_sortDir_ASC", "/trades", {"user": WALLET, "sortDirection": "ASC", "limit": 3}),
    ("trades_sortDir_DESC", "/trades", {"user": WALLET, "sortDirection": "DESC", "limit": 3}),
    ("trades_start_end_unix", "/trades", {"user": WALLET, "start": 1700000000, "end": 1735000000, "limit": 3}),
    ("trades_bogus_param", "/trades", {"user": WALLET, "this_is_not_real": "xyz", "limit": 3}),
    ("trades_no_user", "/trades", {"limit": 3}),
]
results_trades = [get(*t[1:], t[0]) for t in [(x[0], x[1], x[2]) for x in tests_trades]]
for o in results_trades:
    line(o)

# Full field dump of one real trade record
print("\n--- /trades single record full field dump ---")
base = get("/trades", {"user": WALLET, "limit": 1}, "trades_fielddump")
rec = base["body_head"][0] if base.get("body_head") else None
if rec:
    for k in sorted(rec.keys()):
        v = rec[k]
        print(f"  {k:<22} {type(v).__name__:<8} {str(v)[:60]}")

print("\n" + "=" * 80)
print("DATA API /activity  -- field enumeration + param tests")
print("=" * 80)
tests_activity = [
    ("activity_user", "/activity", {"user": WALLET, "limit": 3}),
    ("activity_type_TRADE", "/activity", {"user": WALLET, "type": "TRADE", "limit": 3}),
    ("activity_type_csv", "/activity", {"user": WALLET, "type": "TRADE,SPLIT,MERGE", "limit": 3}),
    ("activity_side_BUY", "/activity", {"user": WALLET, "side": "BUY", "limit": 3}),
    ("activity_market", "/activity", {"user": WALLET, "market": COND, "limit": 3}),
    ("activity_start_end", "/activity", {"user": WALLET, "start": 1700000000, "end": 1735000000, "limit": 3}),
    ("activity_sortBy_TS", "/activity", {"user": WALLET, "sortBy": "TIMESTAMP", "sortDirection": "ASC", "limit": 3}),
    ("activity_no_user", "/activity", {"limit": 3}),
]
results_activity = [get(t[1], t[2], t[0]) for t in tests_activity]
for o in results_activity:
    line(o)

print("\n--- /activity single record full field dump ---")
basea = get("/activity", {"user": WALLET, "limit": 1}, "activity_fielddump")
reca = basea["body_head"][0] if basea.get("body_head") else None
if reca:
    for k in sorted(reca.keys()):
        v = reca[k]
        print(f"  {k:<22} {type(v).__name__:<8} {str(v)[:60]}")

print("\nDONE. Raw responses cached under", CACHE)
