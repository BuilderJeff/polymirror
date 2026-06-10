"""Build a top-10 watchlist from the Polymarket PROFIT leaderboard, gated on
recent activity (read-only).

SELECTION BASIS (deliberate deviation from R2 for the forward exploratory test):
this picks by realised PnL / reputation, NOT by the skill-vs-luck filter. The
luck filter leaves zero survivors, so for a go-forward "track who's winning now"
experiment we use the leaderboard and label it as such.

  source : lb-api.polymarket.com/profit?window=30d   (recent earners => recent activity)
  active : >=1 TRADE in the last ACTIVE_DAYS, verified live via data-api /activity
  output : notes/_watchlist_leaderboard.csv  (top 10 active, ranked by 30d profit)
"""
from __future__ import annotations
import csv
import time
from pathlib import Path

import requests

LB = "https://lb-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
H = {"User-Agent": "Mozilla/5.0 (polymirror research; read-only)"}

SCAN = 50            # how many leaderboard names to consider
WANT = 10            # final watchlist size
ACTIVE_DAYS = 7      # "active" = traded within this many days
NOW = int(time.time())


def leaderboard(window: str, limit: int) -> list[dict]:
    r = requests.get(f"{LB}/profit", params={"window": window, "limit": limit},
                     headers=H, timeout=10)
    r.raise_for_status()
    return r.json()


def last_trade_unix(wallet: str) -> int | None:
    """Most-recent TRADE timestamp for a wallet, or None if no trades found."""
    r = requests.get(f"{DATA}/activity",
                     params={"user": wallet, "type": "TRADE", "limit": 20,
                             "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
                     headers=H, timeout=10)
    r.raise_for_status()
    recs = r.json()
    if not isinstance(recs, list) or not recs:
        return None
    ts = [int(x["timestamp"]) for x in recs if x.get("timestamp")]
    return max(ts) if ts else None


def main() -> None:
    prof30 = leaderboard("30d", SCAN)
    prof7 = {d["proxyWallet"]: d.get("amount") for d in leaderboard("7d", SCAN)}
    print(f"[lb] pulled top {len(prof30)} by 30d profit")

    rows = []
    for i, d in enumerate(prof30, 1):
        w = d["proxyWallet"]
        name = d.get("name") or d.get("pseudonym") or ""
        try:
            last = last_trade_unix(w)
        except Exception as e:
            print(f"  [{i}/{len(prof30)}] {name[:18]:18} activity ERR {type(e).__name__}")
            last = None
        age_h = (NOW - last) / 3600 if last else None
        active = age_h is not None and age_h <= ACTIVE_DAYS * 24
        rows.append(dict(lb_rank=i, wallet=w, name=name,
                         profit_30d=round(float(d.get("amount") or 0), 2),
                         profit_7d=round(float(prof7.get(w) or 0), 2),
                         last_trade_age_h=round(age_h, 1) if age_h is not None else None,
                         active=active))
        flag = "ACTIVE" if active else ("stale" if age_h is not None else "no-trades")
        agestr = f"{age_h:6.1f}h" if age_h is not None else "   n/a"
        print(f"  [{i:>2}/{len(prof30)}] {name[:20]:20} 30d=${rows[-1]['profit_30d']:>13,.0f} "
              f"last={agestr}  {flag}")

    active_rows = [r for r in rows if r["active"]][:WANT]
    out = Path(__file__).parent / "_watchlist_leaderboard.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        wcsv = csv.DictWriter(f, fieldnames=["rank", "lb_rank", "wallet", "name",
                                             "profit_30d", "profit_7d",
                                             "last_trade_age_h"])
        wcsv.writeheader()
        for rank, r in enumerate(active_rows, 1):
            wcsv.writerow({"rank": rank, "lb_rank": r["lb_rank"], "wallet": r["wallet"],
                           "name": r["name"], "profit_30d": r["profit_30d"],
                           "profit_7d": r["profit_7d"],
                           "last_trade_age_h": r["last_trade_age_h"]})

    print(f"\n[lb] {sum(r['active'] for r in rows)}/{len(rows)} scanned names are active "
          f"(traded <= {ACTIVE_DAYS}d). Top {len(active_rows)} -> {out.name}")
    print(f"\n{'rk':>2} {'wallet':44} {'30d profit':>14} {'7d profit':>13} {'last':>8}  name")
    for rank, r in enumerate(active_rows, 1):
        print(f"{rank:>2} {r['wallet']:44} ${r['profit_30d']:>12,.0f} ${r['profit_7d']:>11,.0f} "
              f"{r['last_trade_age_h']:>6.1f}h  {r['name'][:22]}")


if __name__ == "__main__":
    main()
