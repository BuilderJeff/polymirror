"""forward.py — LIVE FORWARD mirror-trading collector (read-only, R0).

This is the go-forward counterpart to the historical backtest. A scheduled job calls
`run_once()` every ~15 min. Each cycle:

  1. CAPTURE  — pull each watchlist wallet's NEW BUY entries since the last poll
                (`/activity` honours start/end), recording the executable entry price,
                the live CLOB mids of BOTH outcome tokens at capture, and the
                FAVORITE-at-entry (the side with mid > 0.5) — never with hindsight (R4).
  2. MARK     — for every open capture, as each holding horizon N ∈ {0,1,6,24,48}h
                falls due, record the live mid of both tokens (strategy leg = the bought
                token; benchmark leg = the favorite token). Marks land within one poll
                interval of the true horizon (a documented ±15-min granularity).
  3. SETTLE   — if the market resolved, marks at/after the settlement settle to 0/1
                (the book is gone, so no live mid exists); earlier horizons we failed to
                catch are recorded as "missed" — a gap is logged, never silently filled.
  4. CLOSE    — a capture finalises once 48h have elapsed or it is fully resolved+marked.

State is a single JSON doc under data/forward/ keyed by transactionHash (idempotent: a
re-poll never double-captures). Strategy and benchmark share entry timing, horizons and
the (later, modelled) cost model and differ ONLY in which side is bought — that isolation
is the experiment (R5). Costs are applied at ANALYSIS time (forward_report.py), per preset.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from config import Config, DEFAULT
from polymirror import polyapi
from polymirror.polyapi import PolyClient, resolution_from_gamma

# Where the live experiment state lives (git-ignored; regenerable only by re-collecting).
_FORWARD_DIR = Path(__file__).resolve().parents[1] / "data" / "forward"
STATE_PATH = _FORWARD_DIR / "state.json"
RUNLOG_PATH = _FORWARD_DIR / "runs.log"
WATCHLIST_PATH = Path(__file__).resolve().parents[1] / "notes" / "_watchlist_leaderboard.csv"

# First-run / poll overlap: look back this far so back-to-back polls never drop an entry
# that landed right on the boundary (dedupe by txhash absorbs the overlap).
_POLL_LOOKBACK_S = 15 * 60


# --------------------------------------------------------------------------- #
# State persistence (atomic; Windows AV-lock tolerant, mirrors polyapi._write_cache)
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"meta": {"experiment_start": None, "last_poll_ts": None, "runs": 0},
            "captures": {}}


def save_state(state: dict) -> None:
    _FORWARD_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=1, default=str)
    for attempt in range(5):
        tmp_name: Optional[str] = None
        try:
            fd, tmp_name = tempfile.mkstemp(dir=str(_FORWARD_DIR), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_name, STATE_PATH)
            return
        except OSError:
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            time.sleep(0.15 * (attempt + 1))
    raise OSError(f"could not persist forward state to {STATE_PATH}")


def load_watchlist(path: Path = WATCHLIST_PATH) -> list[dict]:
    """Read the wallet watchlist CSV (rank,lb_rank,wallet,name,...). Wallet col required."""
    import csv
    if not path.exists():
        raise FileNotFoundError(f"watchlist not found: {path} (run notes/_leaderboard_watchlist.py)")
    out = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            w = (row.get("wallet") or "").strip().lower()
            if w:
                out.append({"wallet": w, "name": row.get("name", "")})
    return out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _market_tokens(cli: PolyClient, condition_id: str, cache: dict) -> Optional[list[str]]:
    """The two CLOB token ids for a binary market (index-parallel to outcomeIndex), or
    None if the market isn't a clean binary. Cached per run."""
    if condition_id in cache:
        return cache[condition_id][0]
    try:
        rec = cli.get_market_gamma(condition_id)  # token ids are static -> cache OK
    except polyapi.PolyAPIError:
        cache[condition_id] = (None, None)
        return None
    info = resolution_from_gamma(rec)
    toks = info.clob_token_ids if len(info.clob_token_ids) == 2 else None
    cache[condition_id] = (toks, rec)
    return toks


def _mid(cli: PolyClient, token_id: str, mid_cache: dict) -> Optional[float]:
    """Live CLOB mid, memoised per poll cycle. All marks in one cycle are 'as of now',
    so reading each token once per run is correct AND cuts redundant HTTP sharply when
    many entries share a market (hyperactive wallets cluster in a few books)."""
    if token_id in mid_cache:
        return mid_cache[token_id]
    v = cli.get_clob_midpoint(token_id)
    mid_cache[token_id] = v
    return v


def _favorite_index(outcome_index: int, mid_bought: Optional[float],
                    mid_other: Optional[float], entry_price: float) -> int:
    """Favorite (R4) = the outcome whose mid > 0.5 AT ENTRY. Prefer live mids; fall back
    to the fill price (binary: the bought side is the favorite iff its price > 0.5)."""
    other = 1 - int(outcome_index)
    if mid_bought is not None and mid_other is not None:
        return int(outcome_index) if mid_bought >= mid_other else other
    return int(outcome_index) if entry_price > 0.5 else other


# --------------------------------------------------------------------------- #
# Step 1 — capture new BUY entries
# --------------------------------------------------------------------------- #
def capture_new_entries(cli: PolyClient, watchlist: list[dict], state: dict,
                        now: int, market_cache: dict, mid_cache: dict) -> int:
    meta = state["meta"]
    caps = state["captures"]
    last = meta.get("last_poll_ts")
    start = (last - 60) if last else (now - _POLL_LOOKBACK_S)  # tiny overlap; dedupe handles it
    n_new = 0
    for entry in watchlist:
        wallet = entry["wallet"]
        try:
            acts = cli.get_activity(wallet, type="TRADE", start=start, end=now)
        except polyapi.PolyAPIError as e:
            print(f"  [capture] {entry['name'][:18]:18} activity ERR: {e}")
            continue
        for a in acts:
            if a.get("side") != "BUY":
                continue
            tx = a.get("transactionHash")
            if not tx or tx in caps:
                continue
            cond = a.get("conditionId")
            oi = a.get("outcomeIndex")
            price = a.get("price")
            ts = a.get("timestamp")
            if cond is None or oi is None or price is None or ts is None:
                continue
            try:
                oi = int(oi); price = float(price); ts = int(ts)
            except (TypeError, ValueError):
                continue
            if oi not in (0, 1) or not (0.0 < price < 1.0):
                continue
            toks = _market_tokens(cli, cond, market_cache)
            if not toks:
                continue  # non-binary or unfetchable market — out of scope
            tok_bought, tok_other = toks[oi], toks[1 - oi]
            mid_b = _mid(cli, tok_bought, mid_cache)
            mid_o = _mid(cli, tok_other, mid_cache)
            fav = _favorite_index(oi, mid_b, mid_o, price)
            caps[tx] = {
                "tx": tx, "wallet": wallet, "name": a.get("name") or entry["name"],
                "condition_id": cond, "slug": a.get("slug"), "title": a.get("title"),
                "outcome_index": oi, "token_bought": tok_bought, "token_other": tok_other,
                "entry_trade_ts": ts, "capture_ts": now,
                "entry_price": price, "entry_mid_bought": mid_b, "entry_mid_other": mid_o,
                "favorite_index": fav, "size": a.get("size"), "usdc_size": a.get("usdcSize"),
                "marks": {}, "resolved": False, "winning_index": None,
                "resolution_ts": None, "status": "open",
            }
            # N=0 mark is the entry itself (the instantaneous round-trip cost floor).
            caps[tx]["marks"]["0"] = {
                "bought": mid_b if mid_b is not None else price,
                "other": mid_o if mid_o is not None else (1.0 - price),
                "at_ts": now, "source": "mid" if mid_b is not None else "entry_price",
            }
            n_new += 1
            print(f"  [capture+] {entry['name'][:16]:16} {str(a.get('title'))[:34]:34} "
                  f"oi={oi} p={price:.3f} fav={fav}")
    return n_new


# --------------------------------------------------------------------------- #
# Steps 2-4 — mark open captures at due horizons; settle; close
# --------------------------------------------------------------------------- #
def update_marks(cli: PolyClient, state: dict, now: int, cfg: Config,
                 market_cache: dict, mid_cache: dict) -> dict:
    caps = state["captures"]
    n_hours = list(cfg.N_hours)
    max_n = max(n_hours)
    stats = {"marked": 0, "settled": 0, "missed": 0, "resolved": 0, "closed": 0}
    res_cache: dict = {}  # one FRESH resolution check per market per cycle (captures cluster)

    def _resolution(cond: str):
        if cond in res_cache:
            return res_cache[cond]
        out = None
        try:
            info = resolution_from_gamma(cli.get_market_gamma(cond, fresh=True))
            if info.resolved and info.winning_index is not None:
                out = (info.winning_index, info.closed_time_unix or now)
        except polyapi.PolyAPIError:
            pass
        res_cache[cond] = out
        return out

    for cap in caps.values():
        if cap.get("status") == "closed":
            continue
        entry_ts = cap["entry_trade_ts"]
        marks = cap["marks"]

        # Refresh resolution for still-open markets (fresh => see the close transition).
        if not cap["resolved"]:
            r = _resolution(cap["condition_id"])
            if r is not None:
                cap["resolved"] = True
                cap["winning_index"], cap["resolution_ts"] = r
                stats["resolved"] += 1

        resolved = cap["resolved"]
        res_ts = cap["resolution_ts"]
        win = cap["winning_index"]
        bought_settle = (1.0 if win == cap["outcome_index"] else 0.0) if resolved else None

        for n in n_hours:
            key = str(n)
            if key in marks:
                continue
            horizon = entry_ts + n * 3600
            if now < horizon:
                continue  # not due yet
            if resolved and res_ts is not None and horizon >= res_ts:
                # Held past settlement -> exit at the settled 0/1.
                marks[key] = {"bought": bought_settle, "other": 1.0 - bought_settle,
                              "at_ts": now, "source": "settle"}
                stats["settled"] += 1
                continue
            # Horizon predates settlement (or market still open): use the live mid now,
            # which is within one poll interval of the true horizon.
            mid_b = _mid(cli, cap["token_bought"], mid_cache)
            mid_o = _mid(cli, cap["token_other"], mid_cache)
            if mid_b is not None:
                marks[key] = {"bought": mid_b,
                              "other": mid_o if mid_o is not None else (1.0 - mid_b),
                              "at_ts": now, "source": "mid"}
                stats["marked"] += 1
            elif resolved:
                marks[key] = {"bought": bought_settle, "other": 1.0 - bought_settle,
                              "at_ts": now, "source": "settle"}
                stats["settled"] += 1
            else:
                # No live book and not (yet) flagged resolved — genuine gap, logged not faked.
                marks[key] = {"bought": None, "other": None, "at_ts": now, "source": "missed"}
                stats["missed"] += 1

        # Close once the full 48h window elapsed, or fully resolved and every mark present.
        all_due_marked = all(str(n) in marks for n in n_hours if now >= entry_ts + n * 3600)
        if now >= entry_ts + max_n * 3600 and all_due_marked:
            cap["status"] = "closed"
            stats["closed"] += 1
        elif resolved and all(str(n) in marks for n in n_hours):
            cap["status"] = "closed"
            stats["closed"] += 1
    return stats


# --------------------------------------------------------------------------- #
# One full poll cycle
# --------------------------------------------------------------------------- #
def run_once(cli: Optional[PolyClient] = None, *, cfg: Config = DEFAULT,
             watchlist_path: Path = WATCHLIST_PATH) -> dict:
    cfg.validate()
    cli = cli or PolyClient(cfg)
    now = int(time.time())
    state = load_state()
    if state["meta"].get("experiment_start") is None:
        state["meta"]["experiment_start"] = now
    market_cache: dict = {}
    mid_cache: dict = {}  # one live mid per token per cycle (all marks are 'as of now')

    watchlist = load_watchlist(watchlist_path)
    n_new = capture_new_entries(cli, watchlist, state, now, market_cache, mid_cache)
    mark_stats = update_marks(cli, state, now, cfg, market_cache, mid_cache)

    caps = state["captures"]
    n_open = sum(1 for c in caps.values() if c.get("status") != "closed")
    state["meta"]["last_poll_ts"] = now
    state["meta"]["runs"] = state["meta"].get("runs", 0) + 1
    save_state(state)

    summary = {"now": now, "wallets": len(watchlist), "new_entries": n_new,
               "open": n_open, "total": len(caps), **mark_stats}
    _append_runlog(summary)
    return summary


def _append_runlog(summary: dict) -> None:
    try:
        _FORWARD_DIR.mkdir(parents=True, exist_ok=True)
        line = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(summary["now"])) +
                "  " + json.dumps({k: v for k, v in summary.items() if k != "now"}) + "\n")
        with RUNLOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass  # logging is best-effort
