#!/usr/bin/env python3
"""台股恐慌度 → data/fear/tw_ind.csv（每日原始指標，進 repo）與 data/fear/tw.json（網頁用）。

六個計入總分的指標（每項取近 5 年百分位，轉成越高越恐慌，總分 = 平均，至少 4 項）：
  加權指數近 20 日實際波動（年化）、加權指數離近 252 日最高收盤、跌停家數比例（5 日平均）、
  創 52 週新低家數比例（5 日平均）、站上 200 日線的比例（越低越恐慌）、推估融資維持率（越低越恐慌）。
資料：TWSE MI_INDEX（全部證券收盤＋加權指數）、TPEx 上櫃收盤、TWSE MI_MARGN、TPEx 融資餘額。

用法：
  python scripts/fear_tw.py                  # 每日：補抓缺的交易日、更新 csv 與 json
  python scripts/fear_tw.py --build DIR      # 一次性：用回補的日檔重建整段 csv
  python scripts/fear_tw.py --json-only      # 只用 csv 重算 json
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import gzip
import json
import math
import os
import re
import sys
import warnings
from zoneinfo import ZoneInfo

import numpy as np

warnings.filterwarnings("ignore", category=RuntimeWarning)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fear_lib as F  # noqa: E402

ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "data", "fear")
CSV = os.path.join(OUT, "tw_ind.csv")
DAYS = os.path.join(ROOT, "cache_fear", "days")
SEED = os.path.join(ROOT, "seed", "fear_tw_seed.json.gz")
COLS = ["date", "taiex", "ld_day", "low_day", "a200", "mm", "ld_n", "n_traded", "low_n", "n_have", "a200_n", "a200_den", "mm_l", "mm_o", "otc"]
COST_PATH = os.path.join(OUT, "margin_cost.json")
MARGIN_RATIO = 0.6  # 上市／上櫃法定融資成數上限（2014-11-10 起上櫃也是 60%）
WINDOW = 252
TZ = ZoneInfo("Asia/Taipei")


def log(m):
    print(m, flush=True)


# ------------------------------------------------------------------ csv
def read_csv():
    if not os.path.exists(CSV):
        return []
    with open(CSV, newline="") as f:
        return list(csv.DictReader(f))


def write_csv(rows):
    os.makedirs(OUT, exist_ok=True)
    tmp = CSV + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None or (isinstance(r.get(k), float) and math.isnan(r.get(k))) else r.get(k)) for k in COLS})
    os.replace(tmp, CSV)


def fnum(x):
    try:
        return float(x)
    except Exception:
        return float("nan")


# ------------------------------------------------------------------ 計算
def is_common_sec(code, r):
    """普通股：日檔 flag（is_common）為準；沒有 flag 時用四碼非 0 開頭代號。"""
    if r and len(r) > 4:
        return bool(r[4])
    return bool(re.fullmatch(r"[1-9]\d{3}", str(code)))


def maintenance(day, split=False):
    """推估融資維持率 = Σ(各股融資餘額張數 × 1000 × 收盤) ÷ 融資金額 × 100。
    含 ETF 等全部可融資標的（恐慌指數用這個，才能跟原始網站對上）。
    split=False：上市＋上櫃合計；split=True：回傳 (上市, 上櫃) 全市場值。"""
    if not day.get("mt") or not day.get("mo"):
        return (None, None) if split else None
    p = day["p"]
    parts = []
    for key in ("mt", "mo"):
        ln, bal = day[key]
        mv = 0.0
        for code, b in bal.items():
            r = p.get(code)
            if r and r[0]:
                mv += b * 1000 * r[0]
        parts.append((mv, ln))
    if split:
        return tuple(round(mv / ln * 100, 4) if ln else None for mv, ln in parts)
    mv, loan = sum(x[0] for x in parts), sum(x[1] for x in parts)
    return round(mv / loan * 100, 4) if loan else None


def load_costs():
    try:
        return json.load(open(COST_PATH))
    except Exception:
        return {"date": None, "l": {}, "o": {}}


def save_costs(costs):
    os.makedirs(OUT, exist_ok=True)
    json.dump(costs, open(COST_PATH, "w"), separators=(",", ":"))


def maintenance_ex_etf(day, costs):
    """普通股融資維持率（首頁圖用）。

    分子：只加總普通股的融資餘額 × 1000 × 收盤。
    分母：官方融資金額（含 ETF）減去非普通股的估借款。
    官方沒有公布各檔融資金額，所以 ETF／ETN／受益證券／TDR／特別股的借款
    用「餘額張數 × 1000 × 平均成本 × 60%（法定成數上限）」估算。
    平均成本由每日餘額變化與收盤價滾動推估：餘額增加視為以當日收盤買進，
    餘額減少不改成本。costs 會就地更新，結構 {l:{code:[cost,qty]}, o:{...}}。
    """
    p = day["p"]
    out = []
    for ex, key in (("l", "mt"), ("o", "mo")):
        if not day.get(key):
            out.append(None)
            continue
        loan, bal = day[key]
        book = costs.setdefault(ex, {})
        mv_stk = 0.0
        loan_ex = 0.0
        live = set()
        for code, b in bal.items():
            if not b:
                continue
            r = p.get(code)
            if not r or not r[0]:
                continue
            px = r[0]
            if is_common_sec(code, r):
                mv_stk += b * 1000 * px
                continue
            live.add(code)
            prev = book.get(code)
            if not prev:
                book[code] = [px, b]
            elif b > prev[1]:
                book[code] = [(prev[0] * prev[1] + px * (b - prev[1])) / b, b]
            else:
                book[code] = [prev[0], b]
            loan_ex += b * 1000 * book[code][0] * MARGIN_RATIO
        for code in [c for c in book if c not in live]:
            book.pop(code)
        denom = loan - loan_ex
        out.append(round(mv_stk / denom * 100, 4) if denom > 0 else None)
    return tuple(out)


def otc_close(date):
    """櫃買指數收盤：取同一個流程先算好的 data/latest.json 或 data/days。"""
    for path in (os.path.join(ROOT, "data", "days", f"{date}.json"), os.path.join(ROOT, "data", "latest.json")):
        try:
            j = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        if path.endswith("latest.json"):
            if j.get("date") == date:
                for r in j.get("indices") or []:
                    if r.get("symbol") == "OTC":
                        return r.get("close")
        else:
            v = (j.get("i") or {}).get("OTC")
            if v and len(v) > 3 and v[3]:
                return v[3]
    return None


def row_for(window_days, date):
    """window_days: 依日期排序的日檔 dict（最後一個 = 當天，最多 252 個）。"""
    codes = sorted({c for d in window_days for c in d["p"]})
    ix = {c: i for i, c in enumerate(codes)}
    C = np.full((len(window_days), len(codes)), np.nan)
    for t, d in enumerate(window_days):
        for c, r in d["p"].items():
            C[t, ix[c]] = r[0]
    last = window_days[-1]["p"]
    vol = np.zeros(len(codes)); chg = np.full(len(codes), np.nan); xr = np.zeros(len(codes)); common = np.zeros(len(codes), bool)
    for c, r in last.items():
        i = ix[c]
        vol[i] = r[1] or 0
        chg[i] = np.nan if r[2] is None else r[2]
        xr[i] = r[3]
    for d in window_days:
        for c, r in d["p"].items():
            if len(r) > 4 and r[4]:
                common[ix[c]] = True
    b = F.breadth_day(C, vol, chg, xr, common)
    b["date"] = date
    b["taiex"] = window_days[-1].get("taiex")
    b["mm"] = maintenance(window_days[-1])
    b["otc"] = otc_close(date)
    if len(window_days) < WINDOW:
        b["low_day"] = None
    if len(window_days) < 200:
        b["a200"] = None
    return b


# ------------------------------------------------------------------ 日檔快取
def day_path(d):
    return os.path.join(DAYS, f"{d}.json")


def load_day(d):
    try:
        return json.load(open(day_path(d)))
    except Exception:
        return None


def save_day(d, obj):
    os.makedirs(DAYS, exist_ok=True)
    json.dump(obj, open(day_path(d), "w"), separators=(",", ":"))


def restore_seed():
    if not os.path.exists(SEED):
        return 0
    S = json.load(gzip.open(SEED, "rt"))
    n = 0
    for t, d in enumerate(S["dates"]):
        if os.path.exists(day_path(d)):
            continue
        p = {c: [arr[t], 1.0, None, 0, S["common"].get(c, 0)] for c, arr in S["close"].items() if arr[t] is not None}
        save_day(d, {"p": p, "seed": True})
        n += 1
    return n


def write_seed(dates):
    close, common = {}, {}
    for t, d in enumerate(dates):
        day = load_day(d)
        for c, r in day["p"].items():
            close.setdefault(c, [None] * len(dates))[t] = r[0]
            if len(r) > 4 and r[4]:
                common[c] = 1
    os.makedirs(os.path.dirname(SEED), exist_ok=True)
    with gzip.open(SEED, "wt") as f:
        json.dump({"dates": dates, "common": common, "close": close}, f, separators=(",", ":"))


def prune(keep_dates):
    keep = set(keep_dates)
    for f in glob.glob(os.path.join(DAYS, "*.json")):
        if os.path.basename(f)[:8] not in keep:
            os.remove(f)


# ------------------------------------------------------------------ 每日更新
def daily():
    import fear_fetch as FF
    rows = read_csv()
    if not rows:
        log("no tw_ind.csv; run --build first")
        return 1
    dates = [r["date"].replace("-", "") for r in rows]
    restored = restore_seed()
    if restored:
        log(f"seed: restored {restored} day files")
    now = dt.datetime.now(TZ)
    today = now.date()
    last = dt.date.fromisoformat(rows[-1]["date"])
    changed = False
    d = last + dt.timedelta(days=1)
    while d <= today:
        if d.weekday() >= 5:
            d += dt.timedelta(days=1)
            continue
        if d == today and now.hour < 14:
            break
        # 先補齊視窗內缺的日檔（只要價格）
        need = dates[-(WINDOW - 1):]
        for k in need:
            if load_day(k) is None:
                kd = dt.datetime.strptime(k, "%Y%m%d").date()
                r = FF.fetch_day(kd, need_margin=False)
                if not r:
                    log(f"cannot fetch window day {k}; stop")
                    return finish(rows, changed)
                save_day(k, r)
                log(f"window day {k} fetched")
        r = FF.fetch_day(d)
        if r is None:
            log(f"{d}: not published yet / fetch failed")
            break
        if not r:
            log(f"{d}: market closed")
            d += dt.timedelta(days=1)
            continue
        if r.get("margin_missing"):
            age_h = (now - dt.datetime.combine(d, dt.time(15, 0), TZ)).total_seconds() / 3600
            if age_h < 30:
                log(f"{d}: margin data not published yet; wait for a later run")
                break
            log(f"{d}: margin data missing; writing row without it")
        key = f"{d:%Y%m%d}"
        save_day(key, r)
        win = [load_day(k) for k in need] + [r]
        b = row_for(win, d.isoformat())
        costs = load_costs()
        if costs.get("date") and costs["date"] != rows[-1]["date"]:
            log(f"warn: margin cost snapshot is {costs.get('date')}, csv last is {rows[-1]['date']}")
        b["mm_l"], b["mm_o"] = maintenance_ex_etf(r, costs)
        costs["date"] = d.isoformat()
        save_costs(costs)
        rows.append(b)
        dates.append(key)
        changed = True
        log(f"{d}: taiex {b['taiex']} ld {b['ld_day']} low {b['low_day']} a200 {b['a200']} mm {b['mm']} mm_l {b['mm_l']} mm_o {b['mm_o']}")
        d += dt.timedelta(days=1)
    prune(dates[-(WINDOW + 10):])
    return finish(rows, changed)


def finish(rows, changed):
    if changed:
        write_csv(rows)
    make_json(rows)
    return 0


# ------------------------------------------------------------------ 重建
def build(src):
    files = sorted(glob.glob(os.path.join(src, "*.json")))
    days, rows = [], []
    for f in files:
        d = os.path.basename(f)[:8]
        day = json.load(open(f))
        if not day.get("p"):
            continue
        days.append(day)
        if len(days) > WINDOW:
            days.pop(0)
        iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        row = row_for(days, iso)
        if "costs" not in locals():
            costs = {"date": None, "l": {}, "o": {}}
        if day.get("mt") and day.get("mo"):
            row["mm_l"], row["mm_o"] = maintenance_ex_etf(day, costs)
            costs["date"] = iso
        rows.append(row)
        if len(rows) % 250 == 0:
            log(f"{iso} {rows[-1]}")
    write_csv(rows)
    if "costs" in locals():
        save_costs(costs)
    # 最近的日檔放進快取並寫 seed
    os.makedirs(DAYS, exist_ok=True)
    keep = [os.path.basename(f)[:8] for f in files[-(WINDOW + 8):]]
    for k in keep:
        obj = json.load(open(os.path.join(src, f"{k}.json")))
        if obj.get("p"):
            save_day(k, obj)
    write_seed([k for k in keep if load_day(k)][-WINDOW:])
    make_json(read_csv())


# ------------------------------------------------------------------ json
def write_margin_json(rows):
    """首頁「融資維持率趨勢」用：上市（配加權指數）、上櫃（配櫃買指數），已扣除 ETF 等非普通股。"""
    out = {"d": [], "taiex": [], "otc": [], "mm_l": [], "mm_o": [],
           "ex_etf": True,
           "note": "已扣除 ETF、ETN、受益證券、TDR、特別股等非普通股。官方只公布全市場融資金額，非普通股借款以餘額張數×平均成本×60%估算後從分子分母扣除。"}
    for r in rows:
        if not r.get("mm_l") and not r.get("mm_o"):
            continue
        out["d"].append(r["date"])
        for k, nd in (("taiex", 2), ("otc", 2), ("mm_l", 1), ("mm_o", 1)):
            v = fnum(r.get(k))
            out[k].append(None if math.isnan(v) else round(v, nd))
    json.dump(out, open(os.path.join(OUT, "margin.json"), "w"), separators=(",", ":"))
    if out["d"]:
        log(f"margin: {out['d'][-1]} 上市 {out['mm_l'][-1]} 上櫃 {out['mm_o'][-1]}")


def make_json(rows):
    dates = [r["date"] for r in rows]
    taiex = np.array([fnum(r["taiex"]) for r in rows])
    for i in range(1, len(taiex)):
        if np.isnan(taiex[i]):
            taiex[i] = taiex[i - 1]
    ld = np.array([fnum(r["ld_day"]) for r in rows])
    low = np.array([fnum(r["low_day"]) for r in rows])
    a200 = np.array([fnum(r["a200"]) for r in rows])
    mm = np.array([fnum(r["mm"]) for r in rows])
    comps = [
        dict(key="vol", name="加權指數實際波動(近 20 日，年化)", desc="每天漲跌幅的標準差換成年化，數字越大代表最近震得越兇", unit="%", dec=1, values=F.realized_vol(taiex)),
        dict(key="dd", name="加權指數離近一年高點", desc="收盤比近 252 個交易日的最高收盤低幾 %", unit="%", dec=1, values=F.off_high(taiex), sign=-1),
        dict(key="ld", name="跌停家數比例(5 日平均)", desc="上市櫃跌停家數 ÷ 有成交的家數，近 5 天平均", unit="%", dec=2, values=F.rolling_mean(ld, 5)),
        dict(key="low", name="創 52 週新低家數比例(5 日平均)", desc="收盤創一年新低的家數占比，近 5 天平均", unit="%", dec=1, values=F.rolling_mean(low, 5)),
        dict(key="a200", name="站上 200 日線的比例", desc="收盤在 200 日均線之上的股票占比，越低越恐慌", unit="%", dec=1, values=a200, sign=-1),
        dict(key="mm", name="大盤推估融資維持率", desc="上市、上櫃依融資金額加權；越低代表融資戶越接近追繳", unit="%", dec=1, values=mm, sign=-1),
    ]
    write_margin_json(rows)
    j = F.market_json("tw", "台股恐慌度", "加權指數", dates, taiex, comps, asof_note="收盤")
    os.makedirs(OUT, exist_ok=True)
    json.dump(j, open(os.path.join(OUT, "tw.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    log(f"tw: {j['asof']} score {j['score']} ({j['score_raw']}) {j['band']} | prev {j['prev']} week {j['week']} month {j['month']}")
    for r in j["components"]:
        log(f"   {r['name']}: {r['value']} pct {r['pct']}")


def rebuild_split_margin(src):
    """只重算 mm_l / mm_o（已扣 ETF），不重跑廣度。mm（全市場、恐慌指數用）不動。"""
    files = {os.path.basename(f)[:8]: f for f in glob.glob(os.path.join(src, "*.json"))}
    rows = read_csv()
    if not rows:
        log("no tw_ind.csv")
        return 1
    costs = {"date": None, "l": {}, "o": {}}
    n = 0
    for r in rows:
        key = r["date"].replace("-", "")
        f = files.get(key)
        if not f:
            continue
        day = json.load(open(f))
        if not day.get("mt") or not day.get("mo"):
            continue
        r["mm_l"], r["mm_o"] = maintenance_ex_etf(day, costs)
        costs["date"] = r["date"]
        n += 1
    write_csv(rows)
    save_costs(costs)
    log(f"rebuild-margin: {n} days, last {costs['date']} 上市 {rows[-1].get('mm_l')} 上櫃 {rows[-1].get('mm_o')}")
    make_json(rows)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--build":
        build(sys.argv[2])
    elif len(sys.argv) > 2 and sys.argv[1] == "--rebuild-margin":
        sys.exit(rebuild_split_margin(sys.argv[2]))
    elif len(sys.argv) > 1 and sys.argv[1] == "--json-only":
        make_json(read_csv())
    else:
        sys.exit(daily())
