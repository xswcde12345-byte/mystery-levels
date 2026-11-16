#!/usr/bin/env python3
"""Levels + chart history for two US indices (Nasdaq Composite, PHLX Semiconductor).

Index variant of the same formulas as TAIEX/OTC (middle = Q, no VWAP); week/month
lines use the ISO week / calendar month of US trading dates.

Primary source: Nasdaq public historical quotes API (official daily OHLC).
Fallback: Yahoo Finance chart API. Unfinished sessions are never used.

  python3 scripts/compute_us.py      -> data/us.json, data/us_hist/{IXIC,SOX}.json
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compute_all as ca  # noqa: E402  (formulas only)
import us_calendar as uc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "us.json"
HIST = ROOT / "data" / "us_hist"
KEEP = 260
UA = ca.UA

SYMS = [
    {"symbol": "IXIC", "name": "那斯達克綜合指數", "short": "納指", "nasdaq": "COMP", "yahoo": "%5EIXIC",
     "aliases": ["納指", "那斯達克", "那指", "NASDAQ", "IXIC", "COMP", "美股"]},
    {"symbol": "SOX", "name": "費城半導體指數", "short": "費半", "nasdaq": "SOX", "yahoo": "%5ESOX",
     "aliases": ["費半", "費城半導體", "半導體", "SOX", "PHLX", "美股"]},
]


def get_json(url: str, timeout: int = 40) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9", "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/"})
    last = None
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            ca.log(f"  retry {i+1}/3 {url[:80]} ({e})")
            import time; time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def fnum(s):
    if s in (None, "", "--", "N/A"):
        return None
    return float(str(s).replace(",", "").replace("$", ""))


def fetch_nasdaq(code: str, start: date, end: date) -> list[dict]:
    url = (f"https://api.nasdaq.com/api/quote/{code}/historical?assetclass=index"
           f"&fromdate={start.isoformat()}&todate={end.isoformat()}&limit=9999")
    j = get_json(url)
    rows = (((j.get("data") or {}).get("tradesTable") or {}).get("rows")) or []
    out = []
    for r in rows:
        m, d, y = [int(x) for x in r["date"].split("/")]
        o, h, l, c = fnum(r.get("open")), fnum(r.get("high")), fnum(r.get("low")), fnum(r.get("close"))
        if h is None or l is None or c is None:
            continue
        out.append({"date": date(y, m, d), "open": o if o else c, "high": h, "low": l, "close": c,
                    "volume": fnum(r.get("volume"))})
    return sorted(out, key=lambda x: x["date"])


def fetch_yahoo(code: str) -> list[dict]:
    out = []
    for host in ("query1", "query2"):
        try:
            j = get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{code}?range=2y&interval=1d")
            res = j["chart"]["result"][0]
            break
        except Exception as e:
            ca.log(f"  yahoo {host} failed: {e}")
            res = None
    if not res:
        return out
    off = res["meta"].get("gmtoffset", -18000)
    q = res["indicators"]["quote"][0]
    for i, ts in enumerate(res.get("timestamp") or []):
        d = (datetime(1970, 1, 1) + timedelta(seconds=ts + off)).date()
        h, l, c, o = q["high"][i], q["low"][i], q["close"][i], q["open"][i]
        if h is None or l is None or c is None:
            continue
        v = q.get("volume", [None] * (i + 1))[i]
        out.append({"date": d, "open": round(o if o else c, 2), "high": round(h, 2), "low": round(l, 2),
                    "close": round(c, 2), "volume": v or None})
    return out


def load(sym: dict, done: date) -> tuple[list[dict], str]:
    start = done - timedelta(days=420)
    bars, src = [], ""
    try:
        bars = fetch_nasdaq(sym["nasdaq"], start, done)
        src = "Nasdaq"
    except Exception as e:
        ca.log(f"{sym['symbol']}: Nasdaq API failed ({e}); trying Yahoo")
    yb = []
    if not bars or bars[-1]["date"] < done:
        yb = fetch_yahoo(sym["yahoo"])
        ymap = {b["date"]: b for b in yb}
        have = {b["date"] for b in bars}
        extra = [b for d, b in sorted(ymap.items()) if d not in have and d >= start]
        if extra:
            bars = sorted(bars + extra, key=lambda x: x["date"])
            src = (src + "+Yahoo") if src else "Yahoo"
    # volume (Nasdaq API omits index volume): take Yahoo's when it has one
    if sym["symbol"] == "IXIC":
        if not yb:
            try:
                yb = fetch_yahoo(sym["yahoo"])
            except Exception:
                yb = []
        vmap = {b["date"]: b["volume"] for b in yb if b.get("volume")}
        for b in bars:
            if not b.get("volume") and b["date"] in vmap:
                b["volume"] = vmap[b["date"]]
    # only finished sessions on real US trading days
    bars = [b for b in bars if b["date"] <= done and uc.is_trading_day(b["date"])]
    return bars, src


def main() -> int:
    done = uc.last_completed_session()
    ca.log(f"last completed US session: {done}")
    HIST.mkdir(parents=True, exist_ok=True)
    cards, sources = [], {}
    for sym in SYMS:
        bars, src = load(sym, done)
        if len(bars) < 30:
            ca.log(f"WARN {sym['symbol']}: only {len(bars)} bars; keeping previous files")
            continue
        for b in bars:
            b.update({"symbol": sym["symbol"], "name": sym["name"], "market": "US", "amount": None})
        first_ok = ca.month_start(bars[0]["date"]) + timedelta(days=32)  # need a full window
        rows = []
        for i, b in enumerate(bars):
            if b["date"] < first_ok:
                continue
            ws = ca.window_start(b["date"])
            hist = [x for x in bars[max(0, i - 30):i + 1] if x["date"] >= ws]
            rec = ca.compute_one(b, hist, b["date"], use_vwap=False)
            rows.append((b, rec))
        rows = rows[-KEEP:]
        b, rec = rows[-1]
        nxt = uc.next_trading_day(b["date"])
        card = ca.slim_row(rec)
        card.update({"date": b["date"].isoformat(), "next": nxt.isoformat(),
                     "short": sym["short"], "aliases": sym["aliases"], "tz": "America/New_York"})
        cards.append(card)
        sources[sym["symbol"]] = src
        has_vol = any(x.get("volume") for x, _ in rows)
        h = {"t": [], "o": [], "h": [], "l": [], "c": [], "u": [], "m": [], "d": [], "w": [], "mo": []}
        if has_vol:
            h["v"] = []
        for x, r in rows:
            h["t"].append(x["date"].isoformat())
            for k, v in (("o", x["open"]), ("h", x["high"]), ("l", x["low"]), ("c", x["close"]),
                         ("u", r["upper"]), ("m", r["middle"]), ("d", r["lower"]),
                         ("w", r["week_line"]), ("mo", r["month_line"])):
                h[k].append(ca._num(v))
            if has_vol:
                h["v"].append(int(x["volume"]) if x.get("volume") else None)
        h["next"] = nxt.isoformat()
        h["vunit"] = "股"
        (HIST / f"{sym['symbol']}.json").write_text(json.dumps(h, separators=(",", ":")), encoding="utf-8")
        ca.log(f"{sym['symbol']} {b['date']} close {b['close']} → {card} ({len(rows)} sessions, {src})")
    if not cards:
        return 1
    old = None
    if OUT.exists():
        old = json.loads(OUT.read_text(encoding="utf-8"))
        keep = {c["symbol"]: c for c in old.get("indices") or []}
        for c in cards:
            keep[c["symbol"]] = c
        cards = [keep[s["symbol"]] for s in SYMS if s["symbol"] in keep]
    payload = {"date": max(c["date"] for c in cards), "timezone": "America/New_York",
               "sources": {"US": "Nasdaq public historical quotes API (official daily OHLC); Yahoo Finance chart API as fallback and for Nasdaq Composite volume"},
               "source_used": sources, "indices": cards}
    if old and {k: v for k, v in old.items() if k != "generated_at"} == payload:
        ca.log("us.json unchanged")
        return 0
    payload["generated_at"] = datetime.now(ca.TAIPEI).isoformat(timespec="seconds")
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    ca.log(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
