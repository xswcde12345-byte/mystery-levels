#!/usr/bin/env python3
"""選股回查「盤中」版（GitHub Actions 排程執行，台北 09:07–13:37 約每 15 分鐘）。

做法與 Grok Bot 的盤中預警相同：
  1. 用截至前一交易日的官方日線預篩（箱型、量縮、均線糾結、箱頂在漲停價內…）。
  2. 對預篩名單向證交所基本市況報導（TWSE MIS）批次查即時報價，組出今天的未完成 K 棒。
  3. 成交量依已交易時間推估全日量（線性與 U 型曲線取較小值），再套用與收盤版相同的條件；
     另要求目前實際累積量已 ≥ 前 20 日均量 1.5 倍，已漲停（買不到）者排除。
  4. 對命中的股票跑 Kronos mini（只對今天第一次入選的股票預測，之後沿用）。
輸出 data/picks/intraday.json（不進 git，只隨網站部署；收盤版掃描完成後網站不再包含它）。
休市日、非盤中時段直接結束並寫 GITHUB_OUTPUT run=false。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
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


def load_prev(sess):
    """上一輪盤中結果（從已部署網站取回，只為沿用 Kronos 預測）。"""
    url = os.environ.get("PREV_URL")
    if not url:
        return {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
            j = json.load(r)
        if j.get("session") == sess:
            return {x["c"]: x for x in j.get("rows", [])}
    except Exception:
        pass
    return {}


def main() -> int:
    now = dt.datetime.now(TZ)
    replay = os.environ.get("IDY_REPLAY")            # 離線測試：YYYYMMDD,HH:MM（用該日官方日線模擬盤中報價，不連 MIS）
    if replay:
        rd, rt = replay.split(",")
        now = dt.datetime.strptime(rd + rt, "%Y%m%d%H:%M").replace(tzinfo=TZ)
    today = f"{now:%Y%m%d}"
    mins = now.hour * 60 + now.minute
    if now.weekday() >= 5 or mins < OPEN_MIN or mins > 13 * 60 + 50:
        log(f"intraday: {now:%F %H:%M} outside trading hours")
        set_output(ran="false", need_kronos="false")
        return 0
    if replay:
        pq = dict(d=today, t=f"{now:%H:%M}:00")
    else:
        probe, _ = mis_fetch(["tse_2330.tw", "tse_t00.tw"])
        pq = probe.get("2330") or probe.get("t00")
    if not pq or not pq.get("d"):
        log("intraday: MIS unavailable")
        set_output(ran="false", need_kronos="false")
        return 1
    sess = pq["d"]
    qtime = pq.get("t") or pq.get("%") or "13:30:00"
    if sess != today:
        log(f"intraday: MIS session {sess} != today {today} (holiday?)")
        set_output(ran="false", need_kronos="false")
        return 0
    L.load_seed()
    yday = now.date() - dt.timedelta(days=1)
    if not replay:
        L.update_days(yday, refetch_days=0)
    dates = [d for d in L.cached_dates() if d < sess and (L.load_day(d) or {}).get("tpok")]
    if len(dates) < L.NEED + L.P["DEDUP"] + 1:
        log("intraday: not enough cached days")
        set_output(ran="false", need_kronos="false")
        return 1
    capf = os.path.join(L.CACHE, "caps.json")
    caps = None
    if os.path.exists(capf) and time.time() - os.path.getmtime(capf) < 3 * 86400:
        caps = json.load(open(capf))
    if not caps:
        caps = L.fetch_caps()
        if len(caps) > 1500:
            os.makedirs(L.CACHE, exist_ok=True)
            json.dump(caps, open(capf, "w"))
        elif os.path.exists(capf):
            caps = json.load(open(capf))
        else:
            log("intraday: no caps")
            set_output(ran="false", need_kronos="false")
            return 1
    n0 = len(dates)
    store = L.build_store(dates + [sess])            # 最後一欄＝今天，先是空的
    t = n0
    elapsed = int(qtime.split(":")[0]) * 60 + int(qtime.split(":")[1]) - OPEN_MIN
    frac_lin = max(1, min(SESSION_MIN, elapsed)) / SESSION_MIN
    frac_cur = max(frac_lin * 0.5, curve_frac(elapsed)) if elapsed < SESSION_MIN else 1.0
    if elapsed >= SESSION_MIN:
        frac_lin = 1.0

    cands, stats = [], 0
    for code, a in store.items():
        cp = caps.get(code)
        if not cp or cp > L.CAP_LIMIT:
            continue
        stats += 1
        ctx = L.prefilter(a, t)
        if ctx:
            cands.append((code, a, ctx))
    log(f"intraday: session {sess} {qtime} universe {stats} -> {len(cands)} prefilter candidates")
    keys = [f"{'tse' if a['exch'] == 'L' else 'otc'}_{c}.tw" for c, a, _ in cands]
    if replay:
        fr = curve_frac(elapsed)
        day = L.load_day(sess) or {"rows": {}}
        raw, fails = {}, []
        for code, a, ctx in cands:
            r = day["rows"].get(code)
            if r:
                raw[code] = dict(z=str(r[5]), o=str(r[2]), h=str(r[3]), l=str(r[4]), y=str(ctx["pc"]), v=str(r[6] * fr / 1000),
                                 d=sess, t=f"{now:%H:%M}:00", u=str(limit_up_price(ctx["pc"])), a="-", b="-")
    else:
        raw, fails = mis_fetch(keys)
    rows, limited, noquote = [], [], []
    for code, a, ctx in cands:
        m = raw.get(code)
        q = parse_quote(m) if m else None
        if not q or q["date"] != sess:
            noquote.append(code)
            continue
        base = ctx["base"]
        proj = min(q["v"] / frac_lin, q["v"] / frac_cur) if frac_lin > 0 and frac_cur > 0 else q["v"]
        xr = bool(q["y"] and abs(q["y"] / ctx["pc"] - 1) > 0.001)
        a["o"][t], a["h"][t], a["l"][t], a["c"][t], a["v"][t], a["xr"][t] = q["o"], q["h"], q["l"], q["price"], proj, xr
        info = L.check(a, t)
        act_ok = q["v"] >= ACT_X * base
        if info is not None and q["limit_up"]:
            limited.append(dict(c=code, n=a["name"], m=a["exch"], cl=round(q["price"], 2), pc=round(info["pct"], 2), ok=bool(act_ok)))
            continue
        if info is None or not act_ok:
            continue
        rows.append(dict(c=code, n=a["name"], m=a["exch"], cl=round(info["close"], 2), pc=round(info["pct"], 2),
                         lot=int(round(q["v"] / 1000)), plot=int(round(proj / 1000)), vx=round(info["vx"], 1),
                         ax=round(q["v"] / base, 1), bt=round(info["box_top"], 2), bk=round(info["brk"] * 100, 2),
                         up=round(info["upper"] * 100), qt=q["time"]))
    rows.sort(key=lambda r: -r["vx"])
    prev = load_prev(sess)
    pend = []
    for r in rows:
        p = prev.get(r["c"])
        if p and (p.get("k") is not None or p.get("kna")):
            for k in ("k", "k10", "kh", "kna"):
                if k in p:
                    r[k] = p[k]
        else:
            pend.append((r["c"], sess))
    PEND = os.path.join(L.CACHE, "idy_pending.json")
    os.makedirs(L.CACHE, exist_ok=True)
    bars = {}
    for code, _ in pend:
        a_ = store[code]
        bars[code] = [float(a_[k][t]) for k in "ohlcv"] + [int(a_["xr"][t])]
    json.dump(dict(session=sess, items=bars), open(PEND, "w"))
    out = dict(session=f"{sess[:4]}-{sess[4:6]}-{sess[6:]}", quote_time=qtime,
               generated_at=dt.datetime.now(TZ).isoformat(timespec="seconds"),
               frac_lin=round(frac_lin, 3), frac_cur=round(frac_cur, 3), hist_last=dates[-1],
               n_universe=stats, n_cand=len(cands), n_quoted=len(cands) - len(noquote) - len(fails),
               rows=rows, limit_up=limited)
    os.makedirs(L.OUT, exist_ok=True)
    json.dump(out, open(OUTFILE, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    log(f"intraday: hits {len(rows)}, limit-up excluded {len(limited)}, quoted {out['n_quoted']}/{len(cands)}")
    for r in rows:
        log(f"  {r['c']} {r['n']} {r['cl']} {r['pc']:+.2f}% 推估{r['vx']}x 實際{r['ax']}x k={r.get('k')}")
    set_output(ran="true", need_kronos="true" if pend else "false")
    return 0


def kronos_phase() -> int:
    """第二階段（workflow 內已裝好 torch 才執行）：對今天第一次入選的股票預測，寫回 intraday.json。"""
    import picks_kronos as K
    pend = json.load(open(os.path.join(L.CACHE, "idy_pending.json")))
    sess = pend["session"]
    out = json.load(open(OUTFILE, encoding="utf-8"))
    dates = [d for d in L.cached_dates() if d < sess and (L.load_day(d) or {}).get("tpok")]
    store = L.build_store(dates + [sess])
    t = len(dates)
    for code, v in pend["items"].items():
        a = store[code]
        a["o"][t], a["h"][t], a["l"][t], a["c"][t], a["v"][t], a["xr"][t] = v[0], v[1], v[2], v[3], v[4], bool(v[5])
    res = K.run([(c, sess) for c in pend["items"]], dates + [sess], store, os.environ.get("KRONOS_DIR"))
    n = 0
    for r in out["rows"]:
        if r["c"] in pend["items"]:
            p = res.get((r["c"], sess))
            if p:
                r["k"], r["k10"], r["kh"] = p["p5"], p["p10"], p["pmax"]
                n += 1
            else:
                r["kna"] = 1
    json.dump(out, open(OUTFILE, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    log(f"intraday: Kronos wrote {n}/{len(pend['items'])}")
    return 0


if __name__ == "__main__":
    sys.exit(kronos_phase() if "--kronos" in sys.argv else main())
