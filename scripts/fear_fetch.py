#!/usr/bin/env python3
"""恐慌度用的官方日資料抓取：TWSE / TPEx 全部證券收盤（含 ETF）、加權指數、融資餘額。

每天一個精簡檔：{"p": {code: [close, vol, chg, xr, common]}, "taiex": x, "mt": [loan, {code: bal}], "mo": [...]}
（mt/mo = 上市/上櫃 融資金額（元）與各股融資餘額（張））。
用法（回補）：python scripts/fear_fetch.py START END OUTDIR
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picks_lib import _curl, num, is_common  # noqa: E402


def get(url, post=None, tries=4, timeout=45, sleep=4.0):
    for i in range(tries):
        j = _curl(url, post, timeout)
        if j is not None:
            return j
        time.sleep(sleep * (i + 1))
    return None


def fetch_twse_px(d: dt.date):
    """回傳 (rows, taiex)；休市回傳 ({}, None)；失敗回傳 (None, None)。"""
    j = get(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d:%Y%m%d}&type=ALLBUT0999&response=json")
    if j is None:
        return None, None
    if j.get("stat") != "OK":
        return ({}, None) if "沒有符合條件" in (j.get("stat") or "") else (None, None)
    rows, taiex = {}, None
    for t in j.get("tables", []):
        title = t.get("title") or ""
        if "價格指數" in title and "臺灣證券交易所" in title:
            for r in t.get("data", []):
                if r[0].strip() == "發行量加權股價指數":
                    taiex = num(r[1])
        if "每日收盤行情" in title:
            for r in t["data"]:
                code, name = r[0].strip(), r[1].strip()
                c = num(r[8])
                if c is None:
                    continue
                sign = re.sub(r"<[^>]+>", "", r[9]).strip()
                ch = num(r[10])
                if ch is not None and sign == "-":
                    ch = -ch
                if sign == "X":
                    ch = None
                rows[code] = [c, num(r[2]) or 0.0, ch, 1 if sign == "X" else 0, 1 if is_common(code, name) else 0]
    if not rows:
        return None, None
    if taiex is None:   # 舊日期偶爾沒有附指數表：另查 type=IND
        k = get(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d:%Y%m%d}&type=IND&response=json", tries=2)
        for t in (k or {}).get("tables", []):
            for r in t.get("data", []):
                if r and r[0].strip() == "發行量加權股價指數":
                    taiex = num(r[1])
    return rows, taiex


def fetch_tpex_px(d: dt.date):
    j = get("https://www.tpex.org.tw/www/zh-tw/afterTrading/otc",
            post={"date": f"{d:%Y/%m/%d}", "type": "EW", "response": "json"}, timeout=90)
    if j is None:
        return None
    tabs = j.get("tables") or []
    if not (tabs and tabs[0].get("data")) or j.get("date") != f"{d:%Y%m%d}":
        return None
    rows = {}
    for r in tabs[0]["data"]:
        code, name = r[0].strip(), r[1].strip()
        c = num(r[2])
        if c is None:
            continue
        chs = r[3].strip()
        xr = chs.startswith("除")
        rows[code] = [c, num(r[7]) or 0.0, None if xr else num(chs), 1 if xr else 0, 1 if is_common(code, name) else 0]
    return rows


def fetch_twse_margin(d: dt.date):
    j = get(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={d:%Y%m%d}&selectType=ALL&response=json")
    if not j or j.get("stat") != "OK" or len(j.get("tables", [])) < 2 or not j["tables"][1].get("data"):
        return None
    tot = next((r for r in j["tables"][0]["data"] if "金額" in r[0]), None)
    if tot is None:
        return None
    bal = {r[0].strip(): num(r[6]) for r in j["tables"][1]["data"]}
    return [num(tot[5]) * 1000, {k: v for k, v in bal.items() if v}]


def fetch_tpex_margin(d: dt.date):
    j = get(f"https://www.tpex.org.tw/www/zh-tw/margin/balance?date={d:%Y}%2F{d:%m}%2F{d:%d}&response=json", timeout=60)
    if not j or not j.get("tables") or not j["tables"][0].get("data"):
        return None
    t = j["tables"][0]
    if j.get("date") and j.get("date") != f"{d:%Y%m%d}":
        return None
    row = next((r for r in t.get("summary") or [] if "融資金" in r[1]), None)
    if row is None:
        return None
    bal = {r[0].strip(): num(r[6]) for r in t["data"]}
    return [num(row[6]) * 1000, {k: v for k, v in bal.items() if v}]


def fetch_day(d: dt.date, need_margin=True):
    """回傳 dict 或 {}（休市）或 None（失敗/尚未公布）。"""
    tw, taiex = fetch_twse_px(d)
    if tw is None:
        return None
    if not tw:
        return {}
    tp = fetch_tpex_px(d)
    if tp is None:
        return None
    out = {"p": {**tw, **tp}, "taiex": taiex}
    if need_margin:
        mt = fetch_twse_margin(d)
        mo = fetch_tpex_margin(d) if mt else None
        if mt is None or mo is None:
            out["margin_missing"] = True
        else:
            out["mt"], out["mo"] = mt, mo
    return out


if __name__ == "__main__":
    s, e, od = dt.date.fromisoformat(sys.argv[1]), dt.date.fromisoformat(sys.argv[2]), sys.argv[3]
    os.makedirs(od, exist_ok=True)
    d = s
    while d <= e:
        if d.weekday() < 5 and not os.path.exists(f"{od}/{d:%Y%m%d}.json"):
            r = None
            for k in range(3):
                r = fetch_day(d)
                if r is not None and not r.get("margin_missing"):
                    break
                time.sleep(10)
            if r:
                json.dump(r, open(f"{od}/{d:%Y%m%d}.json", "w"), separators=(",", ":"))
                print(d, len(r["p"]), r.get("taiex"), "margin" if "mt" in r else "NO MARGIN", flush=True)
            elif r is None:
                print(d, "FAILED", flush=True)
            time.sleep(1.0)
        d += dt.timedelta(days=1)
