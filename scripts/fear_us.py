#!/usr/bin/env python3
"""美股大盤恐慌度、費半恐慌度 → data/fear/us.json、data/fear/sox.json。

資料：Yahoo Finance（SPY 還原權息、^SOX、^IXIC）、Cboe 官方 CSV（VIX、VIX3M、VXN；Yahoo 備援）、
FRED（聖路易聯準銀行金融壓力指數 STLFSI4，每週；抓不到就用 repo 內上次存的）。只用已收盤的交易日。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import subprocess
import sys
import time
import warnings

import numpy as np

warnings.filterwarnings("ignore", category=RuntimeWarning)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fear_lib as F  # noqa: E402
import us_calendar as uc  # noqa: E402

ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "data", "fear")
UA = "Mozilla/5.0"


def log(m):
    print(m, flush=True)


def http(url, timeout=60, tries=3):
    for i in range(tries):
        try:
            r = subprocess.run(["curl", "-sSL", "--http1.1", "-m", str(timeout), "-A", UA, url], capture_output=True, timeout=timeout + 15)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.decode("utf-8", "replace")
        except Exception:
            pass
        time.sleep(3 * (i + 1))
    return None


def yahoo(sym, adj=False):
    out = {}
    for q in ("period1=0&period2=%d" % (time.time() + 86400), "range=1mo"):
        for host in ("query1", "query2"):
            t = http(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?{q}&interval=1d&events=div%7Csplit")
            try:
                j = json.loads(t)["chart"]["result"][0]
            except Exception:
                continue
            off = j["meta"].get("gmtoffset", 0)
            cl = j["indicators"]["adjclose"][0]["adjclose"] if adj else j["indicators"]["quote"][0]["close"]
            for ts, c in zip(j["timestamp"], cl):
                if c is not None:
                    out[dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).date().isoformat()] = float(c)
            break
    return out


def cboe(name):
    t = http(f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv")
    out = {}
    if not t:
        return out
    for r in csv.DictReader(io.StringIO(t)):
        try:
            m, d, y = r["DATE"].split("/")
            out[f"{y}-{m}-{d}"] = float(r["CLOSE"])
        except Exception:
            pass
    return out


def fsi():
    path = os.path.join(OUT, "stlfsi4.csv")
    t = None if os.environ.get("FEAR_SKIP_FRED") else http("https://fred.stlouisfed.org/graph/fredgraph.csv?id=STLFSI4", timeout=60, tries=2)
    if t and "STLFSI4" in t.splitlines()[0]:
        os.makedirs(OUT, exist_ok=True)
        open(path, "w").write(t)
    else:
        log("FRED STLFSI4 fetch failed; using stored copy")
        t = open(path).read() if os.path.exists(path) else ""
    out = {}
    for line in t.splitlines()[1:]:
        try:
            d, v = line.split(",")
            out[d] = float(v)
        except Exception:
            pass
    return out


def align(dates, series, ffill=False):
    out, last = [], np.nan
    keys = sorted(series)
    j = 0
    for d in dates:
        if ffill:
            while j < len(keys) and keys[j] <= d:
                last = series[keys[j]]
                j += 1
            out.append(last)
        else:
            out.append(series.get(d, np.nan))
    return np.array(out, dtype=float)


def main():
    done = uc.last_completed_session().isoformat()
    log(f"last completed US session: {done}")
    spy, sox, ixic = yahoo("SPY", adj=True), yahoo("%5ESOX"), yahoo("%5EIXIC")
    vix = {**yahoo("%5EVIX"), **cboe("VIX")}
    v3 = {**yahoo("%5EVIX3M"), **cboe("VIX3M")}
    vxn = {**yahoo("%5EVXN"), **cboe("VXN")}
    fs = fsi()
    res = {}
    # ---------- 美股大盤
    if len(spy) > 3000:
        d = sorted(k for k in spy if "1994-01-01" <= k <= done)
        c = np.array([spy[k] for k in d])
        vx, v3a = align(d, vix), align(d, v3)
        comps = [
            dict(key="vix", name="VIX", desc="標普 500 選擇權算出的未來 30 天預期波動", unit="pt", dec=2, values=vx),
            dict(key="term", name="VIX ÷ 3 個月 VIX", desc="大於 1 叫倒掛：市場覺得眼前比之後更不安穩，多出現在急跌時", unit="x", dec=2, values=vx / v3a),
            dict(key="rv", name="標普 500 實際波動(近 20 日，年化)", desc="SPY 還原權息每天報酬的標準差，換成年化", unit="%", dec=1, values=F.realized_vol(c)),
            dict(key="dd", name="標普 500 離近一年高點", desc="SPY 比近 252 個交易日的最高點低幾 %", unit="%", dec=1, values=F.off_high(c), sign=-1),
            dict(key="fsi", name="金融壓力指數(聖路易聯準銀行，每週)", desc="18 個利率、利差、波動率合成；0 = 長期平均，正數 = 壓力高於平均", unit="pt", dec=2, values=align(d, fs, ffill=True)),
        ]
        res["us"] = F.market_json("us", "美股大盤恐慌度", "標普 500(SPY)", d, c, comps, asof_note="美東收盤", stats_from="2000-01-01")
    # ---------- 費半
    if len(sox) > 3000:
        d = sorted(k for k in sox if k <= done)
        c = np.array([sox[k] for k in d])
        ix = align(d, ixic, ffill=True)
        vxa, vxx = align(d, vxn), align(d, vix)
        comps = [
            dict(key="rv", name="費半實際波動(近 20 日，年化)", desc="費城半導體指數每天漲跌幅的標準差換成年化", unit="%", dec=1, values=F.realized_vol(c)),
            dict(key="dd", name="費半離近一年高點", desc="收盤比近 252 個交易日的最高收盤低幾 %", unit="%", dec=1, values=F.off_high(c), sign=-1),
            dict(key="r20", name="費半近 20 日漲跌", desc="跌得越多越快越恐慌", unit="%", dec=1, values=F.change(c, 20), sign=-1),
            dict(key="r5", name="費半近 5 日漲跌", desc="最近一週的急跌", unit="%", dec=1, values=F.change(c, 5), sign=-1),
        ]
        spx = align(d, spy, ffill=True)
        refs = [
            dict(key="vxn", name="那斯達克 100 波動率(VXN)", desc="科技股版的 VIX；費半沒有自己的波動率指數，放這裡參考", unit="pt", dec=2, values=vxa),
            dict(key="nrv", name="那斯達克實際波動(近 20 日，年化)", desc="那斯達克綜合指數每天漲跌幅的標準差換成年化", unit="%", dec=1, values=F.realized_vol(ix)),
            dict(key="ndd", name="那斯達克離近一年高點", desc="收盤比近 252 個交易日的最高收盤低幾 %", unit="%", dec=1, values=F.off_high(ix)),
            dict(key="vxnvix", name="VXN − VIX", desc="正數越大 = 科技股的保險費比整個大盤貴越多", unit="pt", dec=1, values=vxa - vxx),
            dict(key="rel", name="費半比標普 500(近 20 日)", desc="費半近 20 日漲跌幅減標普 500 的；負 = 半導體比大盤弱", unit="%", dec=1, values=F.change(c, 20) - F.change(spx, 20)),
        ]
        res["sox"] = F.market_json("sox", "費半恐慌度", "費半指數", d, c, comps, refs, asof_note="美東收盤", stats_from="1996-01-01")
    os.makedirs(OUT, exist_ok=True)
    for k, v in res.items():
        json.dump(v, open(os.path.join(OUT, f"{k}.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        log(f"{k}: {v['asof']} score {v['score']} ({v['score_raw']}) {v['band']} | prev {v['prev']} week {v['week']} month {v['month']}")
        for r in v["components"]:
            log(f"   {r['name']}: {r['value']} pct {r['pct']}")
    return 0 if len(res) == 2 else 1


if __name__ == "__main__":
    sys.exit(main())
