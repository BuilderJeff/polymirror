"""Quick, dependency-free analysis of the discovery candidate set (stdlib only)."""
import json, time
from pathlib import Path

rows = json.loads(Path("notes/_candidates.json").read_text(encoding="utf-8"))
NOW = int(time.time())
H48 = 48 * 3600

print(f"NOW (unix) = {NOW}  ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(NOW))})")
print(f"total candidates (>=15 resolved BUYs): {len(rows)}\n")

def frac(cond):
    n = sum(1 for r in rows if cond(r))
    return n, 100.0 * n / len(rows)

# --- recency proxy: last scored-trade ts within 48h --------------------------
rec48 = [r for r in rows if (NOW - r["last_ts"]) <= H48]
rec7d = [r for r in rows if (NOW - r["last_ts"]) <= 7 * 86400]
print(f"last scored-trade within 48h : {len(rec48)}")
print(f"last scored-trade within  7d : {len(rec7d)}")
# distribution of recency
ages_days = sorted((NOW - r["last_ts"]) / 86400 for r in rows)
import statistics as st
print(f"recency(days) of last scored trade: min={ages_days[0]:.1f} "
      f"median={st.median(ages_days):.1f} max={ages_days[-1]:.1f}\n")

# --- tiers --------------------------------------------------------------------
def t_ok(r):  # distinguishable from zero
    return r["t_stat"] is not None and r["t_stat"] >= 2.0

credible = [r for r in rows if r["n"] >= 30 and r["n_markets"] >= 4
            and r["mean_edge"] > 0 and t_ok(r)]
longshot = [r for r in rows if r["mean_p"] < 0.35 and r["mean_edge"] > 0.3
            and r["n_markets"] <= 3]
favbuyer = [r for r in rows if r["mean_p"] >= 0.80]

print(f"CREDIBLE  (n>=30 & mkts>=4 & edge>0 & t>=2) : {len(credible)}")
print(f"LONGSHOT-LUCK suspects (mean_p<.35, edge>.3, mkts<=3): {len(longshot)}")
print(f"FAVORITE-buyers (mean_p>=.80)               : {len(favbuyer)}\n")

def show(title, lst, key, rev=True, k=15):
    lst = sorted(lst, key=lambda r: r[key], reverse=rev)[:k]
    print(f"--- {title} (top {len(lst)} by {key}) ---")
    print(f"{'wallet':44}{'n':>4}{'mkt':>4}{'edge':>7}{'t':>6}{'brier':>7}"
          f"{'win':>6}{'mean_p':>7}{'recd':>6}  name")
    for r in lst:
        recd = (NOW - r["last_ts"]) / 86400
        print(f"{r['wallet']:44}{r['n']:>4}{r['n_markets']:>4}{r['mean_edge']:>7.3f}"
              f"{(r['t_stat'] if r['t_stat'] is not None else 0):>6.1f}{r['brier']:>7.3f}"
              f"{r['winrate']:>6.2f}{r['mean_p']:>7.3f}{recd:>6.1f}  {r['name'][:22]}")
    print()

show("CREDIBLE cross-market skill", credible, "t_stat")
show("CREDIBLE by edge", credible, "mean_edge")
show("CREDIBLE & active<=7d", [r for r in credible if (NOW-r['last_ts'])<=7*86400], "t_stat")
