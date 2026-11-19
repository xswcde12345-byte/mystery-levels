#!/usr/bin/env python3
"""市場恐慌度：共用計算（百分位、總分、極度恐慌事件、之後報酬統計、台股廣度指標）。

百分位：每個指標取「含當天在內的最近 1260 個有資料交易日（約 5 年；不足就用全部）」，
當天數值在其中的位置 = (數值 <= 當天者的個數) / 視窗長度 × 100，並轉成越高越恐慌。
總分 = 計入總分各項百分位的平均，至少 4 項才算。
"""
from __future__ import annotations

import math

import numpy as np

WIN = 1260
MIN_ITEMS = 4
BANDS = [(25, "平靜"), (50, "正常"), (75, "緊張"), (90, "恐慌"), (101, "極度恐慌")]


def band(score):
    if score is None or (isinstance(score, float) and math.isnan(score)):
        return None
    for hi, name in BANDS:
        if score < hi:
            return name
    return BANDS[-1][1]


def rolling_pct(values, win=WIN):
    """values: list/array with NaN for missing. Returns array of percentiles (NaN where missing)."""
    v = np.asarray(values, dtype=float)
    out = np.full(len(v), np.nan)
    idx = np.where(~np.isnan(v))[0]
    vv = v[idx]
    for k in range(len(vv)):
        w = vv[max(0, k - win + 1):k + 1]
        out[idx[k]] = (w <= vv[k]).mean() * 100.0
    return out


def combine(pcts: list, min_items=MIN_ITEMS):
    P = np.vstack([np.asarray(p, dtype=float) for p in pcts])
    n = (~np.isnan(P)).sum(axis=0)
    with np.errstate(invalid="ignore"):
        s = np.nanmean(np.where(np.isnan(P), np.nan, P), axis=0) if P.size else np.array([])
    s = np.where(n >= min_items, s, np.nan)
    return s


def realized_vol(close, n=20):
    c = np.asarray(close, dtype=float)
    r = np.full(len(c), np.nan)
    r[1:] = np.log(c[1:] / c[:-1])
    out = np.full(len(c), np.nan)
    for i in range(n, len(c)):
        w = r[i - n + 1:i + 1]
        if not np.isnan(w).any():
            out[i] = w.std(ddof=1) * math.sqrt(252) * 100
    return out


def off_high(close, n=252):
    c = np.asarray(close, dtype=float)
    out = np.full(len(c), np.nan)
    for i in range(n - 1, len(c)):
        out[i] = (c[i] / np.nanmax(c[i - n + 1:i + 1]) - 1) * 100
    return out


def change(close, n):
    c = np.asarray(close, dtype=float)
    out = np.full(len(c), np.nan)
    out[n:] = (c[n:] / c[:-n] - 1) * 100
    return out


def rolling_mean(x, n=5):
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        w = x[i - n + 1:i + 1]
        if not np.isnan(w).any():
            out[i] = w.mean()
    return out


def episodes(dates, score, close, thr=90.0, gap=60):
    """極度恐慌事件：恐慌度 >= 90，且前 60 個交易日內沒有 >= 90 的日子 → 一次新事件（只算第一天）。"""
    out, last = [], -10**9
    s = np.asarray(score, dtype=float)
    c = np.asarray(close, dtype=float)
    oh = off_high(c)
    for i in range(len(s)):
        if np.isnan(s[i]) or s[i] < thr:
            continue
        if i - last > gap:
            def fwd(n):
                return None if i + n >= len(c) else round((c[i + n] / c[i] - 1) * 100, 1)
            fut = c[i + 1:i + 121]
            mn = None
            if len(fut):
                m = (np.nanmin(fut) / c[i] - 1) * 100
                mn = round(m, 1) if m < 0 else 0.0
            out.append(dict(date=dates[i], score=round(float(s[i])), off_high=None if np.isnan(oh[i]) else round(float(oh[i]), 1),
                            max_dd_120=mn, partial=len(fut) < 120, r20=fwd(20), r60=fwd(60), r120=fwd(120)))
        last = i
    return out[::-1]


def forward_stats(dates, score, close):
    """依恐慌度區間統計之後 20/60/120 個交易日的報酬中位數與上漲比例（只算之後滿 120 天的日子）。"""
    s = np.asarray(score, dtype=float)
    c = np.asarray(close, dtype=float)
    n = len(c)
    last_i = n - 121
    rows = []
    groups = [("所有交易日", -1, 1000)] + [(nm, lo, hi) for (lo, (hi, nm)) in zip([0, 25, 50, 75, 90], BANDS)]
    for nm, lo, hi in groups:
        sel = [i for i in range(0, last_i + 1) if not np.isnan(s[i]) and (lo < 0 or (lo <= s[i] < hi))]
        row = dict(band=nm, days=len(sel))
        for h in (20, 60, 120):
            r = np.array([c[i + h] / c[i] - 1 for i in sel]) * 100 if sel else np.array([])
            row[f"med{h}"] = None if not len(r) else round(float(np.median(r)), 1)
            row[f"up{h}"] = None if not len(r) else round(float((r > 0).mean() * 100))
        rows.append(row)
    return rows, (dates[last_i] if last_i >= 0 else None), next((dates[i] for i in range(n) if not np.isnan(s[i])), None)


# ---------------------------------------------------------------- 台股廣度
def tick(p):
    return np.select([p < 10, p < 50, p < 100, p < 500, p < 1000], [0.01, 0.05, 0.1, 0.5, 1.0], 5.0)


def limit_down_price(prev):
    raw = prev * 0.9
    t = tick(raw)
    return np.ceil(np.round(raw / t, 6)) * t


def breadth_day(C, last_vol, last_chg, last_xr, common_mask):
    """C: (>=252 x N) 收盤矩陣（最後一列 = 當天，NaN = 無資料/無成交），last_*: 當天成交量、漲跌、除權息旗標。
    回傳 dict：跌停比例、創 52 週新低比例（全部證券）、站上 200 日線比例（普通股）。"""
    today = C[-1]
    have = ~np.isnan(today)
    # 前一日參考價：優先用 收盤 − 漲跌；沒有漲跌（除權息等）就用前一個有資料的收盤（除權息日略過）
    prev = today - last_chg
    Cf = C.copy()
    for i in range(1, Cf.shape[0]):
        m = np.isnan(Cf[i])
        Cf[i, m] = Cf[i - 1, m]
    prev_close = Cf[-2] if C.shape[0] > 1 else np.full(today.shape, np.nan)
    prev = np.where(np.isnan(prev), np.where(last_xr > 0, np.nan, prev_close), prev)
    traded = have & (last_vol > 0)
    with np.errstate(invalid="ignore"):
        ld = traded & ~np.isnan(prev) & (today <= limit_down_price(prev) + 1e-9)
    n_tr = int(traded.sum())
    # 52 週新低：當天收盤 <= 近 252 個交易日（含當天）最低收盤；要有滿 252 天的資料
    L = Cf[-252:]
    full = have & ((~np.isnan(L)).sum(axis=0) >= 252)
    with np.errstate(invalid="ignore"):
        mn = np.min(np.where(np.isnan(L), np.inf, L), axis=0)
        low = full & (today <= mn + 1e-9)
    # 200 日線：用最近 200 個交易日（缺值以前值補）的平均，普通股、有滿 200 天資料
    W = Cf[-200:]
    ok200 = have & common_mask & ((~np.isnan(W)).sum(axis=0) >= 200)
    with np.errstate(invalid="ignore"):
        ma = W.mean(axis=0)
        above = ok200 & (today > ma)
    return dict(
        ld_day=round(ld.sum() / n_tr * 100, 4) if n_tr else None, ld_n=int(ld.sum()), n_traded=n_tr,
        low_day=round(low.sum() / have.sum() * 100, 4) if have.sum() else None, low_n=int(low.sum()), n_have=int(have.sum()),
        a200=round(above.sum() / ok200.sum() * 100, 4) if ok200.sum() else None, a200_n=int(above.sum()), a200_den=int(ok200.sum()),
    )
