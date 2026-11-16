#!/usr/bin/env python3
"""Assemble per-symbol chart history from data/days/*.json.

  python3 scripts/build_hist.py _site/data/hist

Writes one small file per symbol ({out}/{symbol}.json) so the page loads only
the chart it opens. Columns (same order for every array):
  t: dates, o h l c: daily OHLC, v: volume (stocks in 張, futures in 口, null for
  indices), u m d w mo: the level row computed from that session's data
  (上軌, 中軌, 下軌, 週守, 月守). The page draws each row as valid for the next
  session, exactly as the table does.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAYS = ROOT / "data" / "days"


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "_site" / "data" / "hist")
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(DAYS.glob("????-??-??.json"))
    series: dict[str, dict] = {}
    cols = ("o", "h", "l", "c", "v", "u", "m", "d", "w", "mo")
    for f in files:
        j = json.loads(f.read_text(encoding="utf-8"))
        dt = j["date"]
        pend = set(j.get("pending") or [])
        con = j.get("contract") or {}
        for group in ("s", "i"):
            for sym, row in (j.get(group) or {}).items():
                s = series.get(sym)
                if s is None:
                    s = series[sym] = {"t": [], **{k: [] for k in cols}}
                    if group == "i":
                        s["k"] = []
                        s["p"] = []
                s["t"].append(dt)
                for k, v in zip(cols, row):
                    s[k].append(v)
                if group == "i":
                    s["k"].append(con.get(sym))
                    s["p"].append(1 if sym in pend else 0)
    for sym, s in series.items():
        if "k" in s and not any(s["k"]):
            del s["k"]
        if "p" in s and not any(s["p"]):
            del s["p"]
        if all(v is None for v in s["v"]):
            del s["v"]
        safe = "".join(ch for ch in sym if ch.isalnum())
        (out / f"{safe}.json").write_text(
            json.dumps(s, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"build_hist: {len(series)} symbols from {len(files)} sessions "
          f"({files[0].stem if files else '-'} → {files[-1].stem if files else '-'}) → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
