"""forward_report.py — edge-decay curve from the LIVE forward captures.

Reads data/forward/state.json and computes, per horizon N and per spread preset, the
mirror (strategy) vs buy-the-favorite (benchmark) net return and their EDGE, clustered
by wallet with a seeded bootstrap CI + two-sided p-value (same inference as the
historical simulator, §7.5). Strategy and benchmark differ ONLY in which side is bought
(R5). Every number is reported under all three presets; a result that survives only
`optimistic` is not a result (R6).

This is the analysis pass — run it any time; the collector keeps accumulating. Early on
the sample is tiny and CIs will be wide; that is expected and honest.
"""
from __future__ import annotations

from collections import defaultdict

from config import DEFAULT, SPREAD_PRESETS
from polymirror import costs
from polymirror.forward import load_state
from polymirror.stats import bootstrap_mean_ci, bootstrap_two_sided_p

_VALID = {"mid", "settle", "entry_price"}  # a usable mark (excludes "missed" gaps)


def _entry_mid(cap, side_index):
    """Entry mid for the token at `side_index` (binary complement fallback)."""
    if side_index == cap["outcome_index"]:
        return cap.get("entry_mid_bought") if cap.get("entry_mid_bought") is not None else cap["entry_price"]
    return cap.get("entry_mid_other") if cap.get("entry_mid_other") is not None else (1.0 - cap["entry_price"])


def _leg_return(entry_mid, exit_mid, preset):
    """Net round-trip return of a long: buy at entry mid + half-spread, sell at exit mid
    - half-spread. Returns None if entry is degenerate."""
    entry_exec = costs.apply_spread(entry_mid, "BUY", preset)
    exit_exec = costs.apply_spread(exit_mid, "SELL", preset)
    if entry_exec <= 0:
        return None
    return exit_exec / entry_exec - 1.0


def build_rows(state, preset, n):
    """Per-capture (wallet, strat_ret, bench_ret, edge) at horizon n under `preset`."""
    rows = []
    for cap in state["captures"].values():
        mk = cap["marks"].get(str(n))
        if not mk or mk.get("source") not in _VALID or mk.get("bought") is None:
            continue
        oi, fav = cap["outcome_index"], cap["favorite_index"]
        # strategy: the bought token
        strat = _leg_return(_entry_mid(cap, oi), mk["bought"], preset)
        # benchmark: the favorite-at-entry token (bought side or its complement)
        fav_exit = mk["bought"] if fav == oi else mk.get("other")
        if fav_exit is None:
            continue
        bench = _leg_return(_entry_mid(cap, fav), fav_exit, preset)
        if strat is None or bench is None:
            continue
        rows.append((cap["wallet"], strat, bench, strat - bench))
    return rows


def cluster_by_wallet(rows):
    """Mean within each wallet first (so one hyperactive wallet can't dominate, §7.5)."""
    by = defaultdict(list)
    for w, s, b, e in rows:
        by[w].append((s, b, e))
    strat, bench, edge = [], [], []
    for vals in by.values():
        strat.append(sum(v[0] for v in vals) / len(vals))
        bench.append(sum(v[1] for v in vals) / len(vals))
        edge.append(sum(v[2] for v in vals) / len(vals))
    return strat, bench, edge


def main() -> None:
    state = load_state()
    caps = state["captures"]
    meta = state["meta"]
    n_caps = len(caps)
    n_resolved = sum(1 for c in caps.values() if c.get("resolved"))
    print(f"forward captures: {n_caps} ({n_resolved} resolved)  "
          f"runs={meta.get('runs')}  experiment_start={meta.get('experiment_start')}")
    if n_caps == 0:
        print("no captures yet — let the collector run, then re-report.")
        return

    seed = DEFAULT.seed
    for pname, preset in SPREAD_PRESETS.items():
        print(f"\n=== spread preset: {pname} ===")
        print(f"{'N(h)':>5} {'wallets':>7} {'n_pos':>6} {'strat':>9} {'bench':>9} "
              f"{'edge':>9} {'95% CI':>21} {'p':>7}")
        for n in DEFAULT.N_hours:
            rows = build_rows(state, preset, n)
            if not rows:
                print(f"{n:>5} {'-':>7} {0:>6}  (no marked positions at this horizon yet)")
                continue
            strat, bench, edge = cluster_by_wallet(rows)
            ms, _, _ = bootstrap_mean_ci(strat, n_boot=DEFAULT.n_bootstrap, seed=seed)
            mb, _, _ = bootstrap_mean_ci(bench, n_boot=DEFAULT.n_bootstrap, seed=seed)
            me, lo, hi = bootstrap_mean_ci(edge, n_boot=DEFAULT.n_bootstrap, seed=seed)
            p = bootstrap_two_sided_p(edge, n_boot=DEFAULT.n_bootstrap, seed=seed)
            print(f"{n:>5} {len(strat):>7} {len(rows):>6} {ms:>+9.4f} {mb:>+9.4f} "
                  f"{me:>+9.4f} [{lo:>+7.4f},{hi:>+7.4f}] {p:>7.3f}")

    print("\nNote: spread is a MODELLED assumption (R6); thinness multiplier is off here "
          "(no per-contract in-window volume in the live feed). Marks land within one poll "
          "interval (~15 min) of each horizon. Selection was leaderboard-based, NOT "
          "skill-filtered (R2 deviation) — this forward test is exploratory.")


if __name__ == "__main__":
    main()
