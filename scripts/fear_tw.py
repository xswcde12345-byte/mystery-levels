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
COLS = ["date", "taiex", "ld_day", "low_day", "a200", "mm", "ld_n", "n_traded", "low_n", "n_have", "a200_n", "a200_den"]
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
def maintenance(day):
    """推估融資維持率 = Σ(各股融資餘額張數 × 1000 × 收盤) ÷ (上市＋上櫃融資金額) × 100。"""
    if not day.get("mt") or not day.get("mo"):
        return None
    p = day["p"]
    mv, loan = 0.0, 0.0
    for key in ("mt", "mo"):
        ln, bal = day[key]
        loan += ln
        for code, b in bal.items():
            r = p.get(code)
            if r and r[0]:
                mv += b * 1000 * r[0]
    return round(mv / loan * 100, 4) if loan else None


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
        rows.append(b)
        dates.append(key)
        changed = True
        log(f"{d}: taiex {b['taiex']} ld {b['ld_day']} low {b['low_day']} a200 {b['a200']} mm {b['mm']}")
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
        rows.append(row_for(days, iso))
        if len(rows) % 250 == 0:
            log(f"{iso} {rows[-1]}")
    write_csv(rows)
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
    j = F.market_json("tw", "台股恐慌度", "加權指數", dates, taiex, comps, asof_note="收盤")
    os.makedirs(OUT, exist_ok=True)
    json.dump(j, open(os.path.join(OUT, "tw.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    log(f"tw: {j['asof']} score {j['score']} ({j['score_raw']}) {j['band']} | prev {j['prev']} week {j['week']} month {j['month']}")
    for r in j["components"]:
        log(f"   {r['name']}: {r['value']} pct {r['pct']}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--build":
        build(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == "--json-only":
        make_json(read_csv())
    else:
        sys.exit(daily())
