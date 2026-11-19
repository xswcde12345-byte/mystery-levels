#!/usr/bin/env python3
"""Kronos（開源 K 線預測基礎模型 github.com/shiyu-coder/Kronos，mini 4.1M，CPU）對第一根大陽線訊號預測未來 10 日。

做法與回測相同：訊號日（含）以前 200 根日 K（開高低收量，未還原）→ 預測未來 10 日，5 次抽樣平均
（T=1.0, top_p=0.9）。輸出預測 5 日／10 日收盤報酬與 10 日內最高點預測（相對訊號日收盤）。

環境變數 KRONOS_DIR 指向 Kronos 原始碼目錄（內含 model/）。失敗不影響網站其他部分。
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np

LB, H, SC, BS = 200, 10, 5, 16
MODEL = ("NeoQuasar/Kronos-Tokenizer-2k", "NeoQuasar/Kronos-mini")


def window(a, t):
    """與回測相同的輸入視窗；資料不足或缺漏過多回傳 None。"""
    import pandas as pd
    if t < LB - 1:
        return None
    s = slice(t - LB + 1, t + 1)
    X = np.stack([a["o"][s], a["h"][s], a["l"][s], a["c"][s], a["v"][s]], 1).astype(float)
    if np.isnan(X[:, 3]).sum() > 5 or np.isnan(X[-1]).any():
        return None
    X = pd.DataFrame(X).ffill().bfill().values.copy()
    X[np.isnan(X)] = 0
    df = pd.DataFrame(X, columns=["open", "high", "low", "close", "volume"])
    df["amount"] = df["volume"] * df[["open", "high", "low", "close"]].mean(axis=1)
    return df


def future_dates(dates: list[str], t: int) -> list:
    """訊號日後 10 個交易日的時間戳：已知的用實際日期，不足以後續平日補（只當時間特徵用）。"""
    out = [dt.datetime.strptime(d, "%Y%m%d") for d in dates[t + 1:t + 1 + H]]
    last = out[-1] if out else dt.datetime.strptime(dates[t], "%Y%m%d")
    while len(out) < H:
        last = last + dt.timedelta(days=1)
        if last.weekday() < 5:
            out.append(last)
    return out


def run(items, dates, store, kdir=None, log=print):
    """items: [(code, date_str)]；回傳 {(code, date): dict(p5, p10, pmax)}（缺資料的略過）。"""
    import pandas as pd
    import torch
    kdir = kdir or os.environ.get("KRONOS_DIR")
    sys.path.insert(0, kdir)
    from model import Kronos, KronosPredictor, KronosTokenizer
    idx = {d: i for i, d in enumerate(dates)}
    dts = [dt.datetime.strptime(d, "%Y%m%d") for d in dates]
    work = []
    for code, d in items:
        a = store.get(code)
        if a is None or d not in idx:
            continue
        t = idx[d]
        df = window(a, t)
        if df is not None:
            work.append((code, d, t, df))
    if not work:
        return {}
    torch.set_num_threads(max(1, os.cpu_count() or 2))
    torch.manual_seed(0)
    np.random.seed(0)
    tk, m = MODEL
    pred = KronosPredictor(Kronos.from_pretrained(m).eval(), KronosTokenizer.from_pretrained(tk).eval(),
                           device="cpu", max_context=2048)
    out = {}
    for i in range(0, len(work), BS):
        b = work[i:i + BS]
        res = pred.predict_batch([x[3] for x in b],
                                 [pd.Series(dts[x[2] - LB + 1:x[2] + 1]) for x in b],
                                 [pd.Series(future_dates(dates, x[2])) for x in b],
                                 pred_len=H, T=1.0, top_p=0.9, sample_count=SC, verbose=False)
        for x, r in zip(b, res):
            c0 = float(x[3].close.iloc[-1])
            pc = r.close.values / c0 - 1
            ph = r.high.values / c0 - 1
            out[(x[0], x[1])] = dict(p5=round(float(pc[4]) * 100, 2), p10=round(float(pc[9]) * 100, 2),
                                     pmax=round(float(ph.max()) * 100, 2))
        log(f"kronos {min(i + BS, len(work))}/{len(work)}")
    return out
