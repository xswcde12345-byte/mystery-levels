#!/usr/bin/env python3
"""「選股回查」共用函式：第一根大陽線（出量突破多日箱型）掃描 + 官方日線抓取與快取。

只用官方公開資料：
  TWSE 每日收盤行情 MI_INDEX (ALLBUT0999)、TPEx 上櫃每日收盤行情（OpenAPI 與網頁 API）、
  TWSE / TPEx OpenAPI 上市櫃公司基本資料（實收資本額）。
價格為未還原權息的原始價格。
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse
from decimal import Decimal, ROUND_FLOOR

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "cache_picks")          # Actions cache；不進 repo
DAYS = os.path.join(CACHE, "days")
SEED = os.path.join(ROOT, "seed", "picks_ohlcv_seed.json.gz")
OUT = os.path.join(ROOT, "data", "picks")
UA = "Mozilla/5.0 (X11; Linux x86_64) mystery-levels-picks"
KEEP_DAYS = 300                                      # 日線快取保留交易日數（Kronos 需 200 根）

# ---- 第一根大陽線參數（preset B，2026-10-23 版：前 10 日爆量上限 4 倍） ----
P = dict(PCT=7.0, BODY=3.0, UPPER=0.50, VX=2.0, BOX_N=15, BOX_MAX=30, AMP=0.25, PRIOR_SPIKE=4.0,
         CONV=0.07, MIN_BASE=150_000, MIN_VOL=1_000_000, DEDUP=20, TOUCH_TOL=0.02, MIN_TOUCH=2,
         TOUCH_GAP=3, BRK=0.0)
NEED = 41                                            # 20 日基準量 + 20 日均線 + 當日
CAP_LIMIT = 30_000_000_000                           # 實收資本額 <= 300 億


def log(msg: str) -> None:
    print(msg, flush=True)


def num(s):
    if s is None:
        return None
    s = re.sub(r"<[^>]+>", "", str(s)).replace(",", "").strip()
    if s in ("", "--", "---", "----", "X", "除權", "除息", "除權息"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def is_common(code: str, name: str) -> bool:
    if not re.fullmatch(r"[1-9]\d{3}", code):       # 排除 ETF、權證、特別股
        return False
    if name.endswith("-DR") or "DR" in name[-3:]:    # 存託憑證
        return False
    return True


# ------------------------------------------------------------------ HTTP
def _curl(url: str, post: dict | None = None, timeout: int = 45):
    cmd = ["curl", "-s", "-m", str(timeout), "-A", UA, url]
    if post is not None:
        cmd += ["-X", "POST", "-d", urllib.parse.urlencode(post)]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout + 15).stdout
        if out.strip():
            return json.loads(out)
    except Exception:
        pass
    return None


def get_json(url: str, post: dict | None = None, tries: int = 3, timeout: int = 45, sleep: float = 3.0):
    for i in range(tries):
        j = _curl(url, post, timeout)
        if j is not None:
            return j
        time.sleep(sleep * (i + 1))
    return None


# ------------------------------------------------------------------ 官方日線
def fetch_twse(d: dt.date):
    """回傳 {code: row}；休市/尚未公布回傳 {}；失敗回傳 None。row=[name,'L',o,h,l,c,vol,xr]"""
    j = get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d:%Y%m%d}&type=ALLBUT0999&response=json")
    if j is None:
        return None
    out = {}
    if j.get("stat") == "OK":
        tabs = [t for t in j.get("tables", []) if "每日收盤行情" in (t.get("title") or "")]
        if not tabs:
            return None
        for r in tabs[0]["data"]:
            code, name = r[0].strip(), r[1].strip()
            if not is_common(code, name):
                continue
            sign = re.sub(r"<[^>]+>", "", r[9]).strip()
            c = num(r[8])
            if c is None:
                continue
            out[code] = [name, "L", num(r[5]) or c, num(r[6]) or c, num(r[7]) or c, c, num(r[2]) or 0.0, 1 if sign == "X" else 0]
    elif "沒有符合條件" not in (j.get("stat") or ""):
        return None
    return out


def fetch_tpex(d: dt.date):
    """上櫃：先用 OpenAPI（只有最新交易日、很快），日期不符再用網頁 API。失敗回傳 None。"""
    want = f"{d.year - 1911}{d:%m%d}"
    arr = get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", tries=2, timeout=60)
    out = {}
    if isinstance(arr, list) and any(str(o.get("Date") or "").strip() == want for o in arr[:50]):
        for o in arr:
            if str(o.get("Date") or "").strip() != want:
                continue
            code, name = str(o.get("SecuritiesCompanyCode") or "").strip(), str(o.get("CompanyName") or "").strip()
            if not is_common(code, name):
                continue
            c = num(o.get("Close"))
            if c is None:
                continue
            chg = str(o.get("Change") or "").strip()
            out[code] = [name, "O", num(o.get("Open")) or c, num(o.get("High")) or c, num(o.get("Low")) or c, c,
                         num(o.get("TradingShares")) or 0.0, 1 if chg.startswith("除") else 0]
        if out:
            return out
    j = get_json("https://www.tpex.org.tw/www/zh-tw/afterTrading/otc",
                 post={"date": f"{d:%Y/%m/%d}", "type": "EW", "response": "json"}, tries=2, timeout=90)
    if j is None:
        return None
    tabs = j.get("tables") or []
    if tabs and tabs[0].get("data") and j.get("date") == f"{d:%Y%m%d}":
        for r in tabs[0]["data"]:
            code, name = r[0].strip(), r[1].strip()
            if not is_common(code, name):
                continue
            c = num(r[2])
            if c is None:
                continue
            chg = r[3].strip()
            out[code] = [name, "O", num(r[4]) or c, num(r[5]) or c, num(r[6]) or c, c, num(r[7]) or 0.0, 1 if chg.startswith("除") else 0]
    return out


def fetch_caps():
    """實收資本額（元）。外幣面額者以已發行股數 x 10 元估算。回傳 {code: cap}。"""
    cap = {}
    j = get_json("https://openapi.twse.com.tw/v1/opendata/t187ap03_L")
    for r in j or []:
        k = r["公司代號"].strip()
        v = num(r.get("實收資本額"))
        par = (r.get("普通股每股面額") or "").replace(" ", "")
        sh = num(r.get("已發行普通股數或TDR原股發行股數"))
        if par and "新台幣" not in par and "無面額" not in par and sh:
            v = sh * 10
        cap[k] = v
    j = get_json("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O")
    for r in j or []:
        k = r["SecuritiesCompanyCode"].strip()
        v = num(r.get("Paidin.Capital.NTDollars"))
        par = (r.get("ParValueOfCommonStock") or "").replace(" ", "")
        sh = num(r.get("IssueShares"))
        if par and "新台幣" not in par and "無面額" not in par and sh:
            v = sh * 10
        cap[k] = v
    return cap


# ------------------------------------------------------------------ 日線快取（每個交易日一個檔）
def day_path(d: str) -> str:
    return os.path.join(DAYS, f"{d}.json")


def save_day(d: str, rows: dict, tpok: bool) -> None:
    os.makedirs(DAYS, exist_ok=True)
    with open(day_path(d), "w", encoding="utf-8") as f:
        json.dump({"tpok": tpok, "rows": rows}, f, ensure_ascii=False, separators=(",", ":"))


def load_day(d: str):
    try:
        return json.load(open(day_path(d), encoding="utf-8"))
    except Exception:
        return None


def cached_dates() -> list[str]:
    if not os.path.isdir(DAYS):
        return []
    return sorted(f[:-5] for f in os.listdir(DAYS) if re.fullmatch(r"\d{8}\.json", f))


def load_seed() -> None:
    """快取空（或缺日）時，用 repo 內 seed 補齊（欄式 JSON.gz：最近約 260 個交易日的日線）。"""
    if not os.path.exists(SEED):
        return
    have = set(cached_dates())
    S = json.load(gzip.open(SEED, "rt", encoding="utf-8"))
    dates = S["dates"]
    need = [i for i, d in enumerate(dates) if d not in have]
    if not need:
        return
    per = {dates[i]: {} for i in need}
    for code, a in S["codes"].items():
        name, exch, o, h, l, c, v, xr = a
        for i in need:
            if c[i] is None:
                continue
            per[dates[i]][code] = [name, exch, o[i], h[i], l[i], c[i], v[i] or 0.0, xr[i]]
    for d, rows in per.items():
        save_day(d, rows, True)
    log(f"seed: restored {len(need)} day files ({dates[need[0]]}..{dates[need[-1]]})")


def prune_days(keep: int = KEEP_DAYS) -> None:
    ds = cached_dates()
    for d in ds[:-keep]:
        os.remove(day_path(d))


def update_days(today: dt.date, max_back: int = 12, refetch_days: int = 3) -> list[str]:
    """補抓缺的交易日，最近 refetch_days 天內的已有日一併重抓（交易所晚間會修正數字；盤中掃描傳 0）。回傳這次有寫入的日期。"""
    ds = cached_dates()
    last = dt.datetime.strptime(ds[-1], "%Y%m%d").date() if ds else today - dt.timedelta(days=max_back)
    start = max(last - dt.timedelta(days=max(0, refetch_days - 1)), today - dt.timedelta(days=max_back + 40))
    wrote = []
    d = start
    while d <= today:
        if d.weekday() < 5:
            key = f"{d:%Y%m%d}"
            old = load_day(key)
            recent = refetch_days > 0 and (today - d).days <= refetch_days
            if old is None or not old.get("tpok") or recent:
                tw = fetch_twse(d)
                if tw:                                    # 有交易
                    tp = fetch_tpex(d)
                    rows = dict(tw)
                    ok = bool(tp)
                    if tp:
                        rows.update(tp)
                    if old is not None and old.get("tpok") and not ok:
                        pass                              # 上櫃這次抓失敗，保留舊檔
                    else:
                        save_day(key, rows, ok)
                        wrote.append(key)
                        if not ok:
                            log(f"warn: TPEx {key} missing (TWSE ok)")
                elif tw is None:
                    log(f"warn: TWSE {key} fetch failed")
        d += dt.timedelta(days=1)
    prune_days()
    return wrote


# ------------------------------------------------------------------ 陣列
def build_store(dates: list[str]):
    """由日線快取建 {code: dict(o,h,l,c,v,xr,name,exch)} numpy 陣列（對齊 dates）。"""
    n = len(dates)
    idx = {d: i for i, d in enumerate(dates)}
    st: dict[str, dict] = {}
    for d in dates:
        day = load_day(d)
        if not day:
            continue
        i = idx[d]
        for code, r in day["rows"].items():
            a = st.get(code)
            if a is None:
                a = st[code] = dict(name=r[0], exch=r[1], xr=np.zeros(n, bool))
                for k in "ohlcv":
                    a[k] = np.full(n, np.nan)
            a["name"] = r[0]
            a["exch"] = r[1]
            a["o"][i], a["h"][i], a["l"][i], a["c"][i], a["v"][i] = r[2], r[3], r[4], r[5], r[6]
            a["xr"][i] = bool(r[7])
    return st


# ------------------------------------------------------------------ 第一根大陽線
def close_top_box(a, t, p=P):
    best = None
    h, l, c = a["h"], a["l"], a["c"]
    for N in range(p["BOX_N"], p["BOX_MAX"] + 1):
        if t - N < 0:
            break
        s = slice(t - N, t)
        if np.isnan(c[s]).any():
            break
        hi, lo = float(h[s].max()), float(l[s].min())
        if hi / lo - 1 > p["AMP"]:
            break
        best = (N, hi, lo)
    if not best:
        return None
    N, hi, lo = best
    top = float(c[t - N:t].max())
    sep: list[int] = []
    for i in range(t - N, t):
        if h[i] >= top * (1 - p["TOUCH_TOL"]) and (not sep or i - sep[-1] >= p["TOUCH_GAP"]):
            sep.append(i)
    return dict(N=N, top=top, high=hi, low=lo, amp=hi / lo - 1, touches=sep)


def check(a, t, p=P):
    """第 t 日是否符合條件（不含去重）。回傳 info dict 或 None。"""
    if t < NEED - 1:
        return None
    c, v = a["c"], a["v"]
    Cl, V, pc = c[t], v[t], c[t - 1]
    if not (Cl == Cl and pc == pc) or a["xr"][t]:
        return None
    pct = (Cl / pc - 1) * 100
    if pct < p["PCT"] - 1e-9 or not (V >= p["MIN_VOL"]):
        return None
    W = slice(t - NEED + 1, t + 1)
    if np.isnan(c[W]).any() or not (v[W] > 0).all():
        return None
    O, H, L = a["o"][t], a["h"][t], a["l"][t]
    base = float(v[t - 20:t].mean())
    pmax = float(v[t - 10:t].max())
    cl = c[t - 20:t]
    ma5, ma10, ma20 = float(cl[-5:].mean()), float(cl[-10:].mean()), float(cl.mean())
    conv = max(ma5, ma10, ma20) / min(ma5, ma10, ma20) - 1
    bx = close_top_box(a, t, p)
    if not bx or len(bx["touches"]) < p["MIN_TOUCH"]:
        return None
    body = (Cl - O) / pc * 100
    upper = (H - Cl) / (H - L) if H > L else 0.0
    vx = V / base if base else 0
    ok = (base >= p["MIN_BASE"] and pmax < p["PRIOR_SPIKE"] * base and conv <= p["CONV"] and body >= p["BODY"]
          and upper <= p["UPPER"] and vx >= p["VX"] and Cl > bx["top"] * (1 + p["BRK"]))
    if not ok:
        return None
    return dict(close=float(Cl), pct=float(pct), vol=float(V), vx=float(vx), box_top=bx["top"], box_low=bx["low"],
                box_n=bx["N"], touches=len(bx["touches"]), brk=float(Cl / bx["top"] - 1), upper=float(upper))


def check_first(a, t, p=P):
    info = check(a, t, p)
    if info is None:
        return None
    for k in range(1, p["DEDUP"] + 1):
        if t - k >= NEED - 1 and check(a, t - k, p) is not None:
            return None
    return info


def scan_dates(store, dates, caps, targets, capmiss_ok=False):
    """對 targets（日期字串）逐日掃描。回傳 {date: [dict(code,name,exch,...)]}。"""
    idx = {d: i for i, d in enumerate(dates)}
    res = {}
    for d in targets:
        t = idx[d]
        hits = []
        for code, a in store.items():
            cp = caps.get(code)
            if not cp or cp > CAP_LIMIT:
                continue
            info = check_first(a, t)
            if info:
                info.update(code=code, name=a["name"], exch=a["exch"])
                hits.append(info)
        hits.sort(key=lambda x: -x["vx"])
        res[d] = hits
    return res


def outcome(a, t, n_dates):
    """訊號日收盤進場後的結果：次日、5 日、10 日內最高（收盤／盤中）。資料不足則為 None。"""
    c, h = a["c"], a["h"]
    c0 = c[t]
    out = dict(r1=None, r5=None, mx=None, mh=None, nd=0)
    nd = min(10, n_dates - 1 - t)
    out["nd"] = int(nd)
    if nd >= 1 and c[t + 1] == c[t + 1]:
        out["r1"] = round((c[t + 1] / c0 - 1) * 100, 2)
    if nd >= 5 and c[t + 5] == c[t + 5]:
        out["r5"] = round((c[t + 5] / c0 - 1) * 100, 2)
    if nd >= 1:
        cs = c[t + 1:t + 1 + nd]
        hs = h[t + 1:t + 1 + nd]
        if not np.isnan(cs).all():
            out["mx"] = round((float(np.nanmax(cs)) / c0 - 1) * 100, 2)
            out["mh"] = round((float(np.nanmax(hs)) / c0 - 1) * 100, 2)
    return out


def prefilter(a, t, p=P):
    """盤中用：只看 t 之前（不含今日）的條件，通過者才需要查即時報價。回傳 dict 或 None。
    與收盤條件相同，另加：箱頂 < 昨收 x 1.10（漲停內可突破）；前 20 日未觸發過。"""
    if t < NEED - 1:
        return None
    c, v = a["c"], a["v"]
    W = slice(t - NEED + 1, t)
    if np.isnan(c[W]).any() or not (v[W] > 0).all():
        return None
    base = float(v[t - 20:t].mean())
    if base < p["MIN_BASE"] or float(v[t - 10:t].max()) >= p["PRIOR_SPIKE"] * base:
        return None
    cl = c[t - 20:t]
    ma = [float(cl[-5:].mean()), float(cl[-10:].mean()), float(cl.mean())]
    if max(ma) / min(ma) - 1 > p["CONV"]:
        return None
    bx = close_top_box(a, t, p)
    if not bx or len(bx["touches"]) < p["MIN_TOUCH"]:
        return None
    pc = float(c[t - 1])
    if bx["top"] >= pc * 1.10:
        return None
    for k in range(1, p["DEDUP"] + 1):
        if t - k >= NEED - 1 and check(a, t - k, p) is not None:
            return None
    return dict(base=base, pc=pc, box=bx)


# ------------------------------------------------------------------ 利弗莫爾關鍵點
# 定義取自統一回測（strategy_compare/analyze.py 的「7 利弗莫爾關鍵點」）：
#   收盤 ≥ 前 120 個交易日的最高收盤（停牌日以前收補，窗內有缺值則不成立）
#   且 當日量 ≥ 前 20 日均量（不含當日）x 1.5
#   且 流動性：前 20 日均量 ≥ 150 張；收盤、成交量有效；非除權息日
#   且 前 20 個交易日內沒有出現過同條件（去重）。不設股本上限。
#   回測與 raw 矩陣不排除漲停（頁面統計沿用此定義）。對外公布的即時／當日收盤名單另外剔除漲停股。
LIV_LOOK, LIV_AVG, LIV_VX, LIV_MIN_AVG, LIV_DEDUP = 120, 20, 1.5, 150_000, 20


def _ffill(X):
    n = X.shape[1]
    idx = np.where(~np.isnan(X), np.arange(n), 0)
    np.maximum.accumulate(idx, axis=1, out=idx)
    return X[np.arange(X.shape[0])[:, None], idx]


def _prior_roll(X, w, fn):
    """out[:, t] = fn(X[:, t-w:t])（不含 t）；t < w 為 NaN。"""
    from numpy.lib.stride_tricks import sliding_window_view
    out = np.full(X.shape, np.nan)
    if X.shape[1] > w:
        out[:, w:] = fn(sliding_window_view(X, w, axis=1), axis=2)[:, :-1]
    return out


def livermore_matrix(store: dict, codes: list[str], extra_cols: int = 0):
    """回傳 (raw 布林矩陣, mx120, avg20, Cf)；欄＝store 陣列的各日（可含尾端空的今日欄）。"""
    C = np.vstack([store[c]["c"] for c in codes])
    V = np.vstack([store[c]["v"] for c in codes])
    XR = np.vstack([store[c]["xr"] for c in codes])
    Cf = _ffill(C)
    avg20 = _prior_roll(np.nan_to_num(V), LIV_AVG, np.mean)
    mx120 = _prior_roll(Cf, LIV_LOOK, np.max)
    with np.errstate(invalid="ignore"):
        raw = ((C >= mx120) & (V >= LIV_VX * avg20) & (avg20 >= LIV_MIN_AVG) & ~np.isnan(C)
               & (np.nan_to_num(V) > 0) & ~XR)
    return raw, mx120, avg20, Cf


def tick_size(p: float) -> float:
    return 0.01 if p < 10 else 0.05 if p < 50 else 0.1 if p < 100 else 0.5 if p < 500 else 1.0 if p < 1000 else 5.0


def limit_up_price(prev: float) -> float:
    """參考價 × 1.10，依該金額所在升降單位無條件捨去；若捨去後跨過跳動檔位則改用新檔位再捨一次。
    處置股、新股等非 10% 幅度，請改傳交易所公布的漲停價。"""
    raw = Decimal(str(prev)) * Decimal("1.1")
    tk = Decimal(str(tick_size(float(raw))))
    lim = (raw / tk).to_integral_value(rounding=ROUND_FLOOR) * tk
    if any(float(lim) < b <= float(raw) for b in (10, 50, 100, 500, 1000)):
        tk = Decimal(str(tick_size(float(lim))))
        lim = (raw / tk).to_integral_value(rounding=ROUND_FLOOR) * tk
    return float(lim)


def is_limit_up(price, official_u=None, prev=None, no_ask=False, bid1=None) -> bool:
    """現價或收盤價是否達到當日漲停價。優先使用交易所欄位 official_u（如 MIS 的 u）。"""
    u = official_u if official_u else (limit_up_price(prev) if prev else None)
    if not u or price is None:
        return False
    if price >= u - 1e-6:
        return True
    if no_ask and bid1 is not None and bid1 >= u - 1e-6:
        return True
    return False


def livermore_day(store: dict, dates: list[str], d: str):
    """收盤版：回傳日期 d 的訊號 [(code, name, exch)]。需要 store 涵蓋 d 以前至少 140 個交易日。
    含漲停股（與回測定義一致）；對外公布前請再呼叫 filter_livermore_limit_up。"""
    codes = sorted(store)
    raw, _, _, _ = livermore_matrix(store, codes)
    t = dates.index(d)
    if t < LIV_LOOK + LIV_DEDUP:
        return []
    sig = raw[:, t] & ~raw[:, t - LIV_DEDUP:t].any(axis=1)
    return [(c, store[c]["name"], store[c]["exch"]) for c, s in zip(codes, sig) if s]


def filter_livermore_limit_up(hits, store, dates, d, official_u=None):
    """從已算出的收盤訊號剔除漲停股，不改 raw 矩陣與 20 日去重。
    official_u: {code: 漲停價}；缺漏時改用昨收 × 1.10 的計算值。
    回傳 (kept, excluded)，excluded 為 (code, name, exch, close, limit_u)。"""
    official_u = official_u or {}
    t = dates.index(d)
    kept, excluded = [], []
    for c, n, m in hits:
        close = store[c]["c"][t]
        prev = store[c]["c"][t - 1] if t >= 1 else float("nan")
        close = float(close) if close == close else None
        prev = float(prev) if prev == prev else None
        u = official_u.get(c)
        if is_limit_up(close, official_u=u, prev=prev):
            used = u if u else (limit_up_price(prev) if prev else None)
            excluded.append((c, n, m, close, used))
        else:
            kept.append((c, n, m))
    return kept, excluded
