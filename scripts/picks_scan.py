#!/usr/bin/env python3
"""「選股回查」每日更新（GitHub Actions 內執行）：
1. 補抓官方日線 → 2. 掃描新交易日（及最近 3 日重掃）的第一根大陽線 → 3. 回填前幾日訊號的後續報酬
→ 4. 列出尚缺 Kronos 預測的訊號（pending）。Kronos 在下一步執行，失敗不影響本步結果。
輸出：data/picks/index.json、data/picks/yYYYY.json；若有 GITHUB_OUTPUT 寫 need_kronos=true/false。
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import picks_lib as L  # noqa: E402

FIRST = "20210104"


def iso(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def load_picks() -> tuple[dict, dict]:
    """回傳 (rows_by_date{iso: [row]}, index)"""
    rows = {}
    for f in sorted(glob.glob(os.path.join(L.OUT, "y*.json"))):
        rows.update(json.load(open(f, encoding="utf-8")))
    try:
        idx = json.load(open(os.path.join(L.OUT, "index.json"), encoding="utf-8"))
    except Exception:
        idx = {"d": [], "a": [], "k": []}
    return rows, idx


def compute_stats(rows: dict) -> dict:
    """全期統計（隨資料重算）：已有 5 日結果的訊號；10 日內 +5% 只計滿 10 日者（以收盤價計）。"""
    def grp(rs):
        r5 = [r["r5"] for r in rs if r.get("r5") is not None]
        mx = [r["mx"] for r in rs if r.get("mx") is not None and r.get("nd", 0) >= 10]
        return dict(n=len(rs), n5=len(r5), up5=round(100 * sum(1 for x in r5 if x > 0) / len(r5), 1) if r5 else None,
                    avg5=round(sum(r5) / len(r5), 2) if r5 else None,
                    hit5=round(100 * sum(1 for x in mx if x >= 5) / len(mx), 1) if mx else None, nh=len(mx))
    allr = [r for rs in rows.values() for r in rs]
    withk = [r for r in allr if r.get("k") is not None]
    ds = sorted(d for d, rs in rows.items() if rs)
    return dict(all=grp(allr), kall=grp(withk), kpick=grp([r for r in withk if r["k"] > 0]),
                knot=grp([r for r in withk if r["k"] <= 0]), first=ds[0] if ds else None, last=ds[-1] if ds else None)


def write_picks(rows: dict, trading: list[str], extra: dict | None = None) -> None:
    """rows: {iso: [row]}；trading: 全部交易日（YYYYMMDD，含無訊號日）。"""
    os.makedirs(L.OUT, exist_ok=True)
    by_year: dict[str, dict] = {}
    for d, rs in rows.items():
        if rs:
            by_year.setdefault(d[:4], {})[d] = rs
    for f in glob.glob(os.path.join(L.OUT, "y*.json")):
        os.remove(f)
    for y, m in by_year.items():
        with open(os.path.join(L.OUT, f"y{y}.json"), "w", encoding="utf-8") as f:
            json.dump(dict(sorted(m.items())), f, ensure_ascii=False, separators=(",", ":"))
    prev = set()
    try:                                                    # 保留已知的交易日（日線快取只有最近 300 天）
        prev = {d.replace("-", "") for d in json.load(open(os.path.join(L.OUT, "index.json"), encoding="utf-8"))["d"]}
    except Exception:
        pass
    ds = sorted(set(trading) | prev | {d.replace("-", "") for d in rows})
    ds = [d for d in ds if d >= FIRST]
    idx = {
        "d": [iso(d) for d in ds],
        "a": [len(rows.get(iso(d), [])) for d in ds],
        "k": [sum(1 for r in rows.get(iso(d), []) if r.get("k") is not None and r["k"] > 0) for d in ds],
        "updated": dt.datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds"),
        "stats": compute_stats(rows),
        "rule": "當日漲幅 ≥ %g%%" % L.P["PCT"],
    }
    idx.update(extra or {})
    with open(os.path.join(L.OUT, "index.json"), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))


def make_row(info: dict, out: dict, old: dict | None = None) -> dict:
    r = dict(c=info["code"], n=info["name"], m=info["exch"], cl=round(info["close"], 2), pc=round(info["pct"], 2),
             lot=int(round(info["vol"] / 1000)), vx=round(info["vx"], 1), bt=round(info["box_top"], 2),
             bk=round(info["brk"] * 100, 2), r1=out["r1"], r5=out["r5"], mx=out["mx"], mh=out["mh"], nd=out["nd"])
    for k in ("k", "k10", "kh", "kna"):
        if old and k in old:
            r[k] = old[k]
    return r


def main() -> int:
    today = dt.datetime.now(ZoneInfo("Asia/Taipei")).date()
    L.load_seed()
    L.update_days(today)
    dates = [d for d in L.cached_dates() if (L.load_day(d) or {}).get("tpok")]
    if len(dates) < L.NEED + L.P["DEDUP"] + 1:
        L.log(f"picks: only {len(dates)} cached days, skip")
        return 1
    store = L.build_store(dates)
    capf = os.path.join(L.CACHE, "caps.json")
    caps = L.fetch_caps()
    if len(caps) > 1500:
        json.dump(caps, open(capf, "w"))
    elif os.path.exists(capf):
        caps = json.load(open(capf))
        L.log("picks: cap fetch failed, using cached caps")
    else:
        L.log("picks: no caps available")
        return 1

    rows, idx = load_picks()
    known = set(idx["d"])
    nd = len(dates)
    didx = {d: i for i, d in enumerate(dates)}
    targets = [d for d in dates[-3:]] + [d for d in dates if iso(d) not in known and d >= FIRST]
    targets = sorted(set(targets))
    L.log(f"picks: scanning {targets}")
    res = L.scan_dates(store, dates, caps, targets)
    for d, hits in res.items():
        old = {r["c"]: r for r in rows.get(iso(d), [])}
        t = didx[d]
        rows[iso(d)] = [make_row(h, L.outcome(store[h["code"]], t, nd), old.get(h["code"])) for h in hits]
    # 回填：最近 16 個交易日內的訊號，結果可能還沒滿 10 日
    for d in dates[-16:]:
        if d in res or iso(d) not in rows:
            continue
        t = didx[d]
        for r in rows[iso(d)]:
            a = store.get(r["c"])
            if a is None:
                continue
            o = L.outcome(a, t, nd)
            r.update(r1=o["r1"], r5=o["r5"], mx=o["mx"], mh=o["mh"], nd=o["nd"])
    # 待 Kronos：沒有預測、也沒標過「無法預測」的訊號
    pend = []
    for d in dates:
        if d < "20210601" or didx[d] < 199:
            continue
        for r in rows.get(iso(d), []):
            if r.get("k") is None and not r.get("kna"):
                pend.append([r["c"], d])
    write_picks(rows, dates, dict(last=iso(dates[-1])))
    os.makedirs(L.CACHE, exist_ok=True)
    json.dump(pend, open(os.path.join(L.CACHE, "pending.json"), "w"))
    L.log(f"picks: last day {dates[-1]}, hits today {len(rows.get(iso(dates[-1]), []))}, pending Kronos {len(pend)}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"need_kronos={'true' if pend else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
