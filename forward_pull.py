"""forward_pull.py — ON-DEMAND retrospective puller for the short forward experiment.

No daemon, no uptime requirement. Run it whenever the laptop has wifi (e.g. each time you
check in). Between runs the machine can be fully OFF — nothing is lost, because every price
is reconstructed from server-side history, not read live.

COPY RULE: mirror EVERY trade the wallet makes in a live market, regardless of side
(not just opening BUYs). Each trade is normalised to the equivalent opening LONG via
schema.to_long_entry:
    BUY  of token k @ p  -> long k    @ p
    SELL of token k @ p  -> long (1-k) @ (1-p)   # copying a sell = taking the other side
A SELL may really be profit-taking rather than a fresh view (METHODS R5/§9), but for a pure
mirror we copy the action. "Active" is implicit: you can only trade a market that is live,
so every captured trade was copyable when placed; resolved-later markets settle to 0/1.

Each run:
  1. ENUMERATE trades — every watchlist wallet's BUYs AND SELLs in
     [experiment_start, min(now, end)] via Data /activity, deduped by tx hash.
  2. RECONSTRUCT marks — for each copied position, read the price of BOTH market tokens at
     entry+1h, +2h, … (every whole hour elapsed, up to the experiment end) from CLOB
     /prices-history, which persists for days after resolution. Strategy leg = the copied
     long; benchmark leg = the favorite-at-entry token. Horizons past resolution settle to 0/1.
  3. SNAPSHOT — print positions + a per-horizon mirror-vs-benchmark gross edge table.

The ONLY live dependency is wifi at run time. State in data/forward/experiment.json (keyed by
tx, idempotent). A single late run reconstructs the whole window.
"""
from __future__ import annotations

import bisect
import csv
import json
import os
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from polymirror import polyapi
from polymirror.polyapi import PolyClient, resolution_from_gamma
from polymirror.schema import to_long_entry
from polymirror.forward import load_watchlist  # reuse the watchlist CSV reader

WINDOW_HOURS = 48                      # extended 2-day span (was 6h pilot, then 12h)
FIDELITY_MIN = 1                       # /prices-history sampling (minutes)
CHUNK_S = 4 * 3600                     # /activity window chunk; Data API caps ~4k rows/query
_DIR = Path(__file__).resolve().parent / "data" / "forward"
EXP_PATH = _DIR / "experiment.json"


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_exp() -> dict:
    if EXP_PATH.exists():
        try:
            return json.loads(EXP_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"meta": {"experiment_start": None, "experiment_end": None,
                     "window_hours": WINDOW_HOURS, "last_pull_ts": None, "pulls": 0},
            "positions": {}}


def save_exp(state: dict) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=1, default=str)
    for attempt in range(5):
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(_DIR), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, EXP_PATH)
            return
        except OSError:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            time.sleep(0.15 * (attempt + 1))
    raise OSError(f"could not persist {EXP_PATH}")


def price_at(series: list[tuple[int, float]], t: int):
    """Price at-or-before time t from an ascending [(t,p)] series, or None if t precedes it."""
    if not series:
        return None
    i = bisect.bisect_right(series, (t, float("inf"))) - 1
    return series[i][1] if i >= 0 else None


# --------------------------------------------------------------------------- #
# Pull
# --------------------------------------------------------------------------- #
def _activity_chunked(cli: PolyClient, wallet: str, t0: int, t1: int, depth: int = 0) -> list[dict]:
    """Windowed /activity with adaptive bisection: the Data API's limit/offset ceilings
    cap any single query at ~4000 most-recent rows, so a hyperactive wallet over a long
    window WOULD silently truncate. Bisect any chunk that comes back near the ceiling."""
    acts = cli.get_activity(wallet, type="TRADE", start=t0, end=t1)
    if len(acts) >= 3900 and t1 - t0 > 600 and depth < 8:
        mid = (t0 + t1) // 2
        return (_activity_chunked(cli, wallet, t0, mid, depth + 1)
                + _activity_chunked(cli, wallet, mid + 1, t1, depth + 1))
    return acts


def enumerate_positions(cli: PolyClient, watchlist, state, win_start, cap_now,
                        market_tokens: dict) -> tuple[int, int]:
    """Capture every BUY/SELL in the window as a mirrored long position. Returns
    (n_new, n_skipped_no_market)."""
    pos = state["positions"]
    n_new = n_skip = 0
    for w in watchlist:
        try:
            acts = []
            t = win_start
            while t <= cap_now:  # fixed 4h chunks + adaptive bisection near the ceiling
                t2 = min(t + CHUNK_S - 1, cap_now)
                acts.extend(_activity_chunked(cli, w["wallet"], t, t2))
                t = t2 + 1
        except polyapi.PolyAPIError as e:
            print(f"  [activity] {w['name'][:16]:16} ERR: {e}")
            continue
        for a in acts:
            side = a.get("side")
            if side not in ("BUY", "SELL"):
                continue
            tx = a.get("transactionHash")
            ts = a.get("timestamp")
            if not tx or tx in pos or ts is None:
                continue
            try:
                ts = int(ts)
            except (TypeError, ValueError):
                continue
            if not (win_start <= ts <= cap_now):
                continue
            cond, oi, price = a.get("conditionId"), a.get("outcomeIndex"), a.get("price")
            if cond is None or oi is None or price is None:
                continue
            try:
                oi, price = int(oi), float(price)
            except (TypeError, ValueError):
                continue
            if oi not in (0, 1) or not (0.0 < price < 1.0):
                continue
            toks = market_tokens.get(cond)
            if toks is None:
                try:
                    info = resolution_from_gamma(cli.get_market_gamma(cond))
                    toks = info.clob_token_ids if len(info.clob_token_ids) == 2 else None
                except polyapi.PolyAPIError:
                    toks = None
                market_tokens[cond] = toks
            if not toks:
                n_skip += 1
                continue
            eff_index, eff_entry = to_long_entry(oi, price, side)  # copy-normalise to a long
            pos[tx] = {
                "tx": tx, "wallet": w["wallet"], "name": a.get("name") or w["name"],
                "condition_id": cond, "slug": a.get("slug"), "title": a.get("title"),
                "side": side, "outcome_index": oi, "entry_price": price, "entry_ts": ts,
                "tokens": [toks[0], toks[1]],
                "eff_index": eff_index, "eff_entry_prob": eff_entry,
                "favorite_index": None, "entry_mids": [None, None],
                "marks": {}, "latest": None, "resolved": False,
                "winning_index": None, "resolution_ts": None,
            }
            n_new += 1
    return n_new, n_skip


def reconstruct(cli: PolyClient, state, win_start, cap_now, exp_end):
    pos = state["positions"]
    horizons = list(range(1, state["meta"]["window_hours"] + 1))

    tokens = set()
    for p in pos.values():
        tokens.update(p["tokens"])
    series_cache: dict = {}
    for tok in tokens:
        raw = cli.get_price_history(tok, start_ts=win_start - 300, end_ts=cap_now,
                                    fidelity=FIDELITY_MIN)
        series_cache[tok] = sorted((int(x["t"]), float(x["p"])) for x in raw
                                   if x.get("t") is not None and x.get("p") is not None)

    res_cache: dict = {}

    def resolution(cond):
        if cond in res_cache:
            return res_cache[cond]
        out = None
        try:
            info = resolution_from_gamma(cli.get_market_gamma(cond, fresh=True))
            if info.resolved and info.winning_index is not None:
                out = (info.winning_index, info.closed_time_unix or cap_now)
        except polyapi.PolyAPIError:
            pass
        res_cache[cond] = out
        return out

    def settle_prices(win):
        return [1.0 if win == 0 else 0.0, 1.0 if win == 1 else 0.0]

    stats = {"marks": 0, "settled": 0, "resolved": 0}
    for p in pos.values():
        s0 = series_cache.get(p["tokens"][0], [])
        s1 = series_cache.get(p["tokens"][1], [])

        if p["favorite_index"] is None:  # fix entry mids + favorite once, from history
            m0 = price_at(s0, p_ts(p))
            m1 = price_at(s1, p_ts(p))
            m0 = m0 if m0 is not None else price_if_bought(p, 0)
            m1 = m1 if m1 is not None else price_if_bought(p, 1)
            p["entry_mids"] = [m0, m1]
            p["favorite_index"] = 0 if m0 >= m1 else 1

        r = resolution(p["condition_id"])
        if r and not p["resolved"]:
            stats["resolved"] += 1
        if r:
            p["resolved"] = True
            p["winning_index"], p["resolution_ts"] = r
        res_ts = p["resolution_ts"]

        for h in horizons:
            t_h = p_ts(p) + h * 3600
            if t_h > cap_now or t_h > exp_end:
                continue
            if p["resolved"] and res_ts is not None and t_h >= res_ts:
                sp = settle_prices(p["winning_index"])
                p["marks"][str(h)] = {"p0": sp[0], "p1": sp[1], "t": t_h, "source": "settle"}
                stats["settled"] += 1
            else:
                q0, q1 = price_at(s0, t_h), price_at(s1, t_h)
                p["marks"][str(h)] = {"p0": q0, "p1": q1, "t": t_h,
                                      "source": "history" if q0 is not None else "missing"}
                if q0 is not None:
                    stats["marks"] += 1

        if p["resolved"] and res_ts is not None and cap_now >= res_ts:
            sp = settle_prices(p["winning_index"])
            p["latest"] = {"p0": sp[0], "p1": sp[1], "t": cap_now, "source": "settle"}
        else:
            p["latest"] = {"p0": price_at(s0, cap_now), "p1": price_at(s1, cap_now),
                           "t": cap_now, "source": "history"}
    return stats, horizons


def p_ts(p) -> int:
    """Trade timestamp for a position (the field is stored as 'entry_ts')."""
    return int(p["entry_ts"])


def price_if_bought(p, idx) -> float:
    """Fallback entry mid for token `idx` from the raw fill when history has no point yet:
    the touched token sits at its fill price, its complement at 1-fill."""
    return p["entry_price"] if idx == p["outcome_index"] else (1.0 - p["entry_price"])


# --------------------------------------------------------------------------- #
# Returns, tidy rows, and the deduped edge curve.
# Collapse repeat fills to ONE position per (wallet, market, side) BEFORE
# averaging — otherwise a market hammered with hundreds of fills dominates (one
# had 1095) — then cluster by wallet (§7.5). The raw per-fill mean over-counts
# correlated bets several-fold, so the deduped curve is the honest readout.
# --------------------------------------------------------------------------- #
def _ret(entry_mid, exit_mid):
    if entry_mid is None or exit_mid is None or entry_mid <= 0:
        return None
    return exit_mid / entry_mid - 1.0


def position_rows(state) -> list[dict]:
    """One row per captured position (entry-level; populated even before any mark)."""
    rows = []
    for p in state["positions"].values():
        eff, fav = p["eff_index"], p["favorite_index"]
        em = p.get("entry_mids") or [None, None]
        lat = p.get("latest") or {}
        rows.append({
            "tx": p["tx"], "wallet": p["wallet"], "name": p["name"],
            "condition_id": p["condition_id"], "slug": p.get("slug"), "title": p.get("title"),
            "side": p["side"], "outcome_index": p["outcome_index"],
            "eff_index": eff, "favorite_index": fav,
            "entry_ts": p["entry_ts"],
            "entry_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(p["entry_ts"]))),
            "entry_price": p["entry_price"], "size": p.get("size"),
            "eff_entry_mid": em[eff] if em[eff] is not None else None,
            "fav_entry_mid": em[fav] if em[fav] is not None else None,
            "resolved": int(bool(p.get("resolved"))), "winning_index": p.get("winning_index"),
            "resolution_ts": p.get("resolution_ts"),
            "latest_strat_px": (lat.get("p0"), lat.get("p1"))[eff] if lat else None,
        })
    return rows


def mark_rows(state) -> list[dict]:
    """Tidy LONG table: one row per (position, elapsed horizon) with computed returns.
    This is the main charting input — group by horizon_h, wallet, market, etc."""
    rows = []
    for p in state["positions"].values():
        eff, fav = p["eff_index"], p["favorite_index"]
        em = p.get("entry_mids") or [None, None]
        for h_str, mk in sorted(p["marks"].items(), key=lambda kv: int(kv[0])):
            mp = [mk.get("p0"), mk.get("p1")]
            strat = _ret(em[eff], mp[eff])
            bench = _ret(em[fav], mp[fav])
            rows.append({
                "tx": p["tx"], "wallet": p["wallet"], "name": p["name"],
                "condition_id": p["condition_id"], "slug": p.get("slug"),
                "side": p["side"], "eff_index": eff, "favorite_index": fav,
                "entry_ts": p["entry_ts"], "entry_price": p["entry_price"],
                "horizon_h": int(h_str), "mark_ts": mk.get("t"), "mark_source": mk.get("source"),
                "strat_entry": em[eff], "strat_exit": mp[eff], "strat_ret": strat,
                "bench_entry": em[fav], "bench_exit": mp[fav], "bench_ret": bench,
                "edge": (strat - bench) if (strat is not None and bench is not None) else None,
            })
    return rows


def edge_curve_rows(state, horizons) -> list[dict]:
    """Deduped per-horizon summary: collapse fills to one position per
    (wallet, market, side), then cluster by wallet. Directly plottable as the edge curve."""
    pos = state["positions"].values()
    out = []
    for h in horizons:
        cell = defaultdict(list)  # (wallet, market, eff_side) -> [(strat, bench), ...]
        for p in pos:
            mk = p["marks"].get(str(h))
            if not mk or mk.get("p0") is None:
                continue
            eff, fav = p["eff_index"], p["favorite_index"]
            em, mp = p["entry_mids"], [mk["p0"], mk["p1"]]
            strat, bench = _ret(em[eff], mp[eff]), _ret(em[fav], mp[fav])
            if strat is None or bench is None:
                continue
            cell[(p["wallet"], p["condition_id"], eff)].append((strat, bench))
        if not cell:
            out.append({"horizon_h": h, "n_wallets": 0, "n_positions": 0,
                        "strat": None, "bench": None, "edge": None})
            continue
        bw_s, bw_b = defaultdict(list), defaultdict(list)
        for (w, _m, _e), v in cell.items():
            bw_s[w].append(sum(x[0] for x in v) / len(v))
            bw_b[w].append(sum(x[1] for x in v) / len(v))
        s = [sum(v) / len(v) for v in bw_s.values()]
        b = [sum(v) / len(v) for v in bw_b.values()]
        ms, mb = sum(s) / len(s), sum(b) / len(b)
        out.append({"horizon_h": h, "n_wallets": len(s), "n_positions": len(cell),
                    "strat": round(ms, 6), "bench": round(mb, 6), "edge": round(ms - mb, 6)})
    return out


def _write_csv(path: Path, rows: list[dict], header: list[str]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in header})
    for attempt in range(5):  # target may be open in Excel/AV — retry, then sidestep
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.2 * (attempt + 1))
    alt = path.with_name(path.stem + "_new" + path.suffix)
    os.replace(tmp, alt)
    print(f"  [csv] {path.name} locked by another process; wrote {alt.name} instead")


def export_csv(state, horizons) -> dict:
    """Write flat CSVs for charting next to experiment.json. Returns the paths/counts."""
    _DIR.mkdir(parents=True, exist_ok=True)
    pr, mr, ec = position_rows(state), mark_rows(state), edge_curve_rows(state, horizons)
    paths = {"positions": _DIR / "positions.csv",
             "marks_long": _DIR / "marks_long.csv",
             "edge_curve": _DIR / "edge_curve.csv"}
    if pr:
        _write_csv(paths["positions"], pr, list(pr[0].keys()))
    _write_csv(paths["marks_long"], mr,
               list(mr[0].keys()) if mr else
               ["tx", "wallet", "name", "condition_id", "slug", "side", "eff_index",
                "favorite_index", "entry_ts", "entry_price", "horizon_h", "mark_ts",
                "mark_source", "strat_entry", "strat_exit", "strat_ret", "bench_entry",
                "bench_exit", "bench_ret", "edge"])
    _write_csv(paths["edge_curve"], ec, list(ec[0].keys()))
    return {"positions": len(pr), "marks_long": len(mr), "edge_curve": len(ec), "paths": paths}


def snapshot(state, horizons):
    print(f"\n{'N(h)':>4} {'wallets':>7} {'mkts':>5} {'strat':>9} {'bench':>9} {'edge':>9}")
    for r in edge_curve_rows(state, horizons):
        if r["strat"] is None:
            print(f"{r['horizon_h']:>4} {'-':>7} {0:>5}  (no elapsed marks yet)")
        else:
            print(f"{r['horizon_h']:>4} {r['n_wallets']:>7} {r['n_positions']:>5} "
                  f"{r['strat']:>+9.4f} {r['bench']:>+9.4f} {r['edge']:>+9.4f}")


def main() -> int:
    cli = PolyClient()
    now = int(time.time())
    state = load_exp()
    meta = state["meta"]
    if meta.get("experiment_start") is None:
        meta["experiment_start"] = now
        meta["experiment_end"] = now + meta["window_hours"] * 3600
    elif int(meta.get("window_hours") or WINDOW_HOURS) < WINDOW_HOURS:
        # Span extension (12h -> 2-day): same start, longer capture + horizon window.
        meta["window_hours"] = WINDOW_HOURS
        meta["experiment_end"] = meta["experiment_start"] + WINDOW_HOURS * 3600
    win_start, exp_end = meta["experiment_start"], meta["experiment_end"]
    cap_now = min(now, exp_end)

    market_tokens: dict = {}
    watchlist = load_watchlist()
    n_new, n_skip = enumerate_positions(cli, watchlist, state, win_start, cap_now, market_tokens)
    stats, horizons = reconstruct(cli, state, win_start, cap_now, exp_end)

    meta["last_pull_ts"] = now
    meta["pulls"] = meta.get("pulls", 0) + 1
    save_exp(state)
    exp = export_csv(state, horizons)

    pos = state["positions"]
    n_buy = sum(1 for p in pos.values() if p["side"] == "BUY")
    n_sell = sum(1 for p in pos.values() if p["side"] == "SELL")
    n_res = sum(1 for p in pos.values() if p.get("resolved"))
    mkts = len({p["condition_id"] for p in pos.values()})
    elapsed_h = (cap_now - win_start) / 3600
    print(f"[pull #{meta['pulls']}] window {time.strftime('%H:%M', time.gmtime(win_start))}-"
          f"{time.strftime('%H:%M UTC', time.gmtime(exp_end))} | elapsed {elapsed_h:.1f}h"
          f"{' (ENDED)' if now >= exp_end else ''}")
    print(f"  positions: {len(pos)} (+{n_new} new; {n_buy} buy / {n_sell} sell) across "
          f"{mkts} markets | resolved {n_res} | marks +{stats['marks']} settled +{stats['settled']}"
          + (f" | skipped {n_skip} (no market)" if n_skip else ""))
    snapshot(state, horizons)
    print(f"\nCSV for charts -> data/forward/: positions.csv ({exp['positions']} rows), "
          f"marks_long.csv ({exp['marks_long']}), edge_curve.csv ({exp['edge_curve']})")
    print("Gross (no spread). strat=mirror (every trade, both sides), bench=buy-the-favorite, "
          "edge=strat-bench. Closed-laptop hours are reconstructed from price history.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
