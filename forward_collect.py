"""forward_collect.py — ONE poll cycle of the live forward mirror collector.

Invoked by the Windows Scheduled Task every ~15 min (and runnable by hand). Captures
new watchlist BUYs, marks open positions at due horizons, settles resolved markets,
and persists data/forward/state.json. Read-only against Polymarket (R0). Exit 0 always
unless state cannot be persisted — a transient API hiccup must not kill the schedule.
"""
from __future__ import annotations

import sys

from polymirror import forward


def main() -> int:
    try:
        s = forward.run_once()
    except FileNotFoundError as e:
        print(f"[forward] FATAL: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # never let a transient error kill the scheduled run loop
        print(f"[forward] cycle error (will retry next poll): {e!r}", file=sys.stderr)
        return 0
    print(f"[forward] run #{forward.load_state()['meta']['runs']}: "
          f"wallets={s['wallets']} new_entries={s['new_entries']} "
          f"marked={s['marked']} settled={s['settled']} missed={s['missed']} "
          f"resolved={s['resolved']} closed={s['closed']} | open={s['open']} total={s['total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
