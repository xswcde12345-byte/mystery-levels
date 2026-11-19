#!/usr/bin/env python3
"""讀 cache_picks/pending.json，跑 Kronos mini，把預測寫回 data/picks。失敗不改動既有資料。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import picks_kronos as K  # noqa: E402
import picks_lib as L  # noqa: E402
import picks_scan as PS  # noqa: E402


def main() -> int:
    pend = [tuple(x) for x in json.load(open(os.path.join(L.CACHE, "pending.json")))]
    if not pend:
        return 0
    dates = [d for d in L.cached_dates() if (L.load_day(d) or {}).get("tpok")]
    store = L.build_store(dates)
    out = K.run(pend, dates, store, os.environ.get("KRONOS_DIR"))
    rows, idx = PS.load_picks()
    n = na = 0
    for code, d in pend:
        for r in rows.get(PS.iso(d), []):
            if r["c"] != code:
                continue
            p = out.get((code, d))
            if p:
                r["k"], r["k10"], r["kh"] = p["p5"], p["p10"], p["pmax"]
                r.pop("kna", None)
                n += 1
            else:
                r["kna"] = 1                  # 資料不足 200 日或缺漏過多，無法預測
                na += 1
    PS.write_picks(rows, dates, dict(last=idx.get("last")))
    L.log(f"kronos: wrote {n} predictions, {na} not predictable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
