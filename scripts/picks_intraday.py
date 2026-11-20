#!/usr/bin/env python3
"""「利弗莫爾關鍵點」盤中掃描（GitHub Actions 排程，台北 09:07–13:37 約每 15 分鐘）。

條件（取自統一回測 analyze.py 的定義）：現價創前 120 個交易日最高收盤新高、成交量 ≥ 前 20 日均量 x 1.5、
前 20 日均量 ≥ 150 張、非除權息日、前 20 個交易日內沒出現過同條件。盤中成交量用「推估全日量」：
依已交易時間外推（線性與 U 型曲線取較小值）。結果寫 data/picks/intraday.json（不進 git，只隨網站部署）。
休市日、非盤中時段直接結束並寫 GITHUB_OUTPUT ran=false。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.parse
from decimal import ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import picks_lib as L  # noqa: E402

TZ = ZoneInfo("Asia/Taipei")
OPEN_MIN, SESSION_MIN, ACT_X = 9 * 60, 270, 1.5
CURVE = [(0, 0.0), (5, 0.10), (30, 0.28), (60, 0.38), (90, 0.46), (120, 0.53), (150, 0.60), (180, 0.66),
         (210, 0.72), (240, 0.79), (265, 0.87), (270, 1.0)]       # 經驗假設的 U 型累積量比例（同 Grok Bot 盤中版）
MIS = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
UA = "Mozilla/5.0 (X11; Linux x86_64) mystery-levels-picks-intraday"
OUTFILE = os.path.join(L.OUT, "intraday.json")


def log(m):
    print(m, flush=True)


def set_output(**kw):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            for k, v in kw.items():
                f.write(f"{k}={v}\n")


def curve_frac(m):
    m = max(0, min(SESSION_MIN, m))
    for (a, fa), (b, fb) in zip(CURVE, CURVE[1:]):
        if a <= m <= b:
            return fa + (fb - fa) * (m - a) / (b - a)
    return 1.0


def fnum(x):
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def mis_fetch(keys, batch=50, delay=1.5):
    out, fails = {}, []
    for k in range(0, len(keys), batch):
        chunk = keys[k:k + batch]
        url = f"{MIS}?ex_ch={urllib.parse.quote('|'.join(chunk), safe='|_.')}&json=1&delay=0&_={int(time.time() * 1000)}"
        j = None
        for attempt in range(3):
            try:
                r = subprocess.run(["curl", "-s", "-m", "20", "-A", UA, "-H", "Referer: https://mis.twse.com.tw/stock/index.jsp", url],
                                   capture_output=True, timeout=30).stdout
                j = json.loads(r)
                if j.get("msgArray") is not None:
                    break
            except Exception:
                j = None
            time.sleep(delay * (attempt + 2))
        if not j or j.get("msgArray") is None:
            fails += chunk
            log(f"MIS batch failed ({len(chunk)}): {chunk[0]}...")
            continue
        for m in j["msgArray"]:
            out[m.get("c")] = m
        if k + batch < len(keys):
            time.sleep(delay)
    return out, fails


def tick_size(p):
    return 0.01 if p < 10 else 0.05 if p < 50 else 0.1 if p < 100 else 0.5 if p < 500 else 1.0 if p < 1000 else 5.0


def limit_up_price(prev):
    raw = Decimal(str(prev)) * Decimal("1.1")
    tk = Decimal(str(tick_size(float(raw))))
    lim = (raw / tk).to_integral_value(rounding=ROUND_FLOOR) * tk
    if any(float(lim) < b <= float(raw) for b in (10, 50, 100, 500, 1000)):
        tk = Decimal(str(tick_size(float(lim))))
        lim = (raw / tk).to_integral_value(rounding=ROUND_FLOOR) * tk
    return float(lim)


def parse_quote(m):
    o, h, l, y = fnum(m.get("o")), fnum(m.get("h")), fnum(m.get("l")), fnum(m.get("y"))
    v = fnum(m.get("v"))
    price = fnum(m.get("z"))
    if price is None and isinstance(m.get("trade"), dict):
        price = fnum(m["trade"].get("z"))
    if price is None:
        price = fnum(m.get("pz"))
    if price is None:
        b = fnum((m.get("b") or "").split("_")[0])
        a = fnum((m.get("a") or "").split("_")[0])
        price = (a + b) / 2 if a and b else (b or a)
    if price is None or o is None or v is None:
        return None
    h = max(h or price, price)
    l = min(l or price, price)
    u = fnum(m.get("u")) or (limit_up_price(y) if y else None)
    ask = (m.get("a") or "").split("_")[0].strip()
    bid1 = fnum((m.get("b") or "").split("_")[0])
    no_ask = ask in ("", "-") or fnum(ask) is None
    limit = bool(u) and (price >= u - 1e-6 or (no_ask and bid1 is not None and bid1 >= u - 1e-6))
    return dict(price=price, o=o, h=h, l=l, y=y, v=v * 1000, date=m.get("d"), time=m.get("t") or m.get("%"), limit_up=limit)


def main() -> int:
    now = dt.datetime.now(TZ)
    today = f"{now:%Y%m%d}"
    mins = now.hour * 60 + now.minute
    if now.weekday() >= 5 or mins < OPEN_MIN or mins > 13 * 60 + 50:
        log(f"intraday: {now:%F %H:%M} outside trading hours")
        set_output(ran="false")
        return 0
    probe, _ = mis_fetch(["tse_2330.tw", "tse_t00.tw"])
    pq = probe.get("2330") or probe.get("t00")
    if not pq or not pq.get("d"):
        log("intraday: MIS unavailable")
        set_output(ran="false")
        return 1
    sess = pq["d"]
    qtime = pq.get("t") or pq.get("%") or "13:30:00"
    if sess != today:
        log(f"intraday: MIS session {sess} != today {today} (holiday?)")
        set_output(ran="false")
        return 0
    L.load_seed()
    L.update_days(now.date() - dt.timedelta(days=1), refetch_days=0)
    dates = [d for d in L.cached_dates() if d < sess and (L.load_day(d) or {}).get("tpok")]
    if len(dates) < L.LIV_LOOK + L.LIV_DEDUP + 1:
        log("intraday: not enough cached days")
        set_output(ran="false")
        return 1
    store = L.build_store(dates + [sess])            # 最後一欄＝今天，先是空的
    t = len(dates)
    codes = sorted(store)
    raw, mx120, avg20, Cf = L.livermore_matrix(store, codes)
    elapsed = int(qtime.split(":")[0]) * 60 + int(qtime.split(":")[1]) - OPEN_MIN
    frac_lin = max(1, min(SESSION_MIN, elapsed)) / SESSION_MIN
    frac_cur = max(frac_lin * 0.5, curve_frac(elapsed)) if elapsed < SESSION_MIN else 1.0
    if elapsed >= SESSION_MIN:
        frac_lin = 1.0
    pc = Cf[:, t - 1]
    with np.errstate(invalid="ignore"):
        cand = ((avg20[:, t] >= L.LIV_MIN_AVG) & ~np.isnan(mx120[:, t]) & ~raw[:, t - L.LIV_DEDUP:t].any(axis=1)
                & (mx120[:, t] <= pc * 1.10 + 1e-6))
    cidx = np.where(cand)[0]
    keys = [f"{'tse' if store[codes[i]]['exch'] == 'L' else 'otc'}_{codes[i]}.tw" for i in cidx]
    log(f"intraday: session {sess} {qtime}, universe {len(codes)} -> {len(cidx)} candidates near 120d high")
    resp, fails = mis_fetch(keys)
    rows, quoted = [], 0
    for i in cidx:
        code = codes[i]
        q = parse_quote(resp[code]) if code in resp else None
        if not q or q["date"] != sess:
            continue
        quoted += 1
        proj = min(q["v"] / frac_lin, q["v"] / frac_cur)
        prev_c = store[code]["c"][t - 1]
        xr = bool(q["y"] and prev_c == prev_c and abs(q["y"] / prev_c - 1) > 0.001)
        if (not xr) and q["price"] >= mx120[i, t] and proj >= L.LIV_VX * avg20[i, t] and proj > 0:
            rows.append(dict(c=code, n=store[code]["name"], m=store[code]["exch"]))
    rows.sort(key=lambda r: r["c"])
    out = dict(session=f"{sess[:4]}-{sess[4:6]}-{sess[6:]}", quote_time=qtime,
               generated_at=dt.datetime.now(TZ).isoformat(timespec="seconds"),
               frac_lin=round(frac_lin, 3), frac_cur=round(frac_cur, 3), n_cand=len(cidx), n_quoted=quoted, rows=rows)
    os.makedirs(L.OUT, exist_ok=True)
    json.dump(out, open(OUTFILE, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    log(f"intraday: hits {len(rows)}, quoted {quoted}/{len(cidx)}, failed batches {len(fails)}")
    log("  " + " ".join(f"{r['c']}{r['n']}" for r in rows))
    set_output(ran="true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
