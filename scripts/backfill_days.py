#!/usr/bin/env python3
"""One-off / catch-up backfill of data/days/*.json (chart history).

Recomputes each past session with the same compute_asof() used for latest.json,
so the historical levels use exactly the site's formulas.

  python3 scripts/backfill_days.py --start 2026-05-04 [--end 2026-11-16] [--skip-existing]
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compute_all as ca  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--sleep", default="3.0", help="seconds between HTTP calls")
    ap.add_argument("--prefetch", default="0", help="threads to prefetch day files first")
    ap.add_argument("--tick-days", default="40", help="only fetch TX tick files this many days back")
    a = ap.parse_args()
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else ca.today_taipei()
    ca.warmup()
    ca.SLEEP_SEC = float(a.sleep)
    # Holidays return None and would be re-probed for every window; remember them.
    for name in ("fetch_twse_day", "fetch_tpex_day"):
        orig = getattr(ca, name)
        memo = {}

        def wrap(d, use_cache=True, _o=orig, _m=memo):
            if d not in _m:
                _m[d] = _o(d, use_cache=use_cache)
            return _m[d]
        setattr(ca, name, wrap)
    # TAIFEX keeps the daily tick files only ~30 trading days; skip older ones
    # (the full-session middle line then falls back to Q, flagged as pending).
    orig_ticks = ca.fetch_tx_full_ticks
    tick_from = ca.today_taipei() - timedelta(days=int(a.tick_days))
    ca.fetch_tx_full_ticks = lambda d, c, use_cache=True: (
        orig_ticks(d, c, use_cache=use_cache) if d >= tick_from else None)
    if int(a.prefetch):
        from concurrent.futures import ThreadPoolExecutor
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        days = [x for x in days if x.weekday() < 5]
        lo = ca.window_start(start)
        days = [lo + timedelta(days=i) for i in range((start - lo).days)] + days
        days = [x for x in days if x.weekday() < 5]

        def pf(x):
            try:
                ca.fetch_twse_day(x)
                ca.fetch_tpex_day(x)
            except Exception as e:
                ca.log(f"prefetch {x} failed: {e}")
        with ThreadPoolExecutor(int(a.prefetch)) as ex:
            list(ex.map(pf, days))
        ca.log(f"prefetched {len(days)} weekdays")
    d = start
    n = 0
    while d <= end:
        if d.weekday() < 5:
            path = ca.DAYS_DIR / f"{d.isoformat()}.json"
            if not (a.skip_existing and path.exists()):
                try:
                    res = ca.compute_asof(d, use_cache=True)
                except Exception as e:  # keep going; a later run can fill the gap
                    ca.log(f"ERROR {d}: {e}")
                    res = None
                if res:
                    stocks, indices = res
                    ca.write_day_file(d, stocks, indices)
                    n += 1
                    ca.log(f"day {d}: {len(stocks)} stocks, {len(indices)} indices")
        d += timedelta(days=1)
    ca.log(f"backfilled {n} sessions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
