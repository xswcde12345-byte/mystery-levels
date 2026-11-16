#!/usr/bin/env python3
"""Compute custom levels for all TWSE + TPEx common stocks/ETFs, TAIEX, TX, and OTC index.

Usage:
  python3 scripts/compute_all.py
  python3 scripts/compute_all.py --date 2026-11-13
  python3 scripts/compute_all.py --out data/latest.json

Writes data/latest.json (compact JSON for the static site). Official sources,
cached by date under cache/ (gitignored). Sleeps ~3s between HTTP calls.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
CACHE_TWSE = CACHE / "twse"
CACHE_TPEX = CACHE / "tpex"
CACHE_TAIEX = CACHE / "taiex"
CACHE_TX = CACHE / "tx"
CACHE_OTC = CACHE / "otc"
CACHE_TXTICK = CACHE / "txtick"
for _p in (CACHE_TWSE, CACHE_TPEX, CACHE_TAIEX, CACHE_TX, CACHE_OTC, CACHE_TXTICK):
    _p.mkdir(parents=True, exist_ok=True)

TAIPEI = ZoneInfo("Asia/Taipei")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SLEEP_SEC = 3.0
K_BAND = Decimal(221) / Decimal(274)
GOLDEN = ROOT / "data" / "levels_20261113.csv"
DEFAULT_OUT = ROOT / "data" / "latest.json"

EXCLUDE_NAME = (
    "權證", "認購", "認售", "牛證", "熊證", "展延", "可轉債", "可交債",
    "公司債", "轉換公司債", "附認股權",
)


_T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - _T0:6.1f}s] {msg}", flush=True)


def D(x) -> Decimal:
    return Decimal(str(x))


def r2(x: Decimal) -> float:
    return float(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fnum(x):
    if x is None:
        return None
    s = str(x).strip().replace(",", "").replace("+", "").replace("−", "-")
    if s in {"", "-", "－", "X", "x", "nan", "N/A", "n/a"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def iso(d: date) -> str:
    return d.isoformat()


def yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def roc_slash(d: date) -> str:
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def window_start(d: date) -> date:
    return min(month_start(d), week_monday(d))


def daterange(a: date, b: date):
    cur = a
    while cur <= b:
        yield cur
        cur += timedelta(days=1)


# ---------------- HTTP ----------------
_last_http = 0.0


def _sleep_gate():
    global _last_http
    now = time.time()
    wait = SLEEP_SEC - (now - _last_http)
    if _last_http > 0 and wait > 0:
        time.sleep(wait)
    _last_http = time.time()


_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
_warmed = False


def _headers(extra: dict | None = None, referer: str | None = None) -> dict:
    h = {
        "User-Agent": UA,
        "Accept": "application/json,text/csv,text/plain,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }
    if referer:
        h["Referer"] = referer
    if extra:
        h.update(extra)
    return h


def warmup() -> None:
    """Hit homepages first; some official endpoints expect a session cookie."""
    global _warmed
    if _warmed:
        return
    for url in (
        "https://www.twse.com.tw/zh/",
        "https://www.tpex.org.tw/zh-tw/",
        "https://www.taifex.com.tw/cht/3/futDailyMarketView",
    ):
        try:
            http_get(url, tries=1, timeout=10, sleep=True)
        except Exception as e:
            log(f"  warmup {url} failed ({e})")
    _warmed = True


def http_get(url: str, tries: int = 3, timeout: int = 90, sleep: bool = True, referer: str | None = None) -> bytes:
    last = None
    for i in range(tries):
        if sleep:
            _sleep_gate()
        try:
            req = urllib.request.Request(url, headers=_headers(referer=referer))
            t0 = time.time()
            with _opener.open(req, timeout=timeout) as r:
                body = r.read()
            dt = time.time() - t0
            if dt > 15:
                log(f"    slow GET {dt:.0f}s {len(body)}B {url[:90]}")
            return body
        except Exception as e:
            last = e
            log(f"  retry {i+1}/{tries} GET {url[:90]}… ({e})")
            time.sleep(SLEEP_SEC * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def http_post(url: str, data: dict, tries: int = 3, timeout: int = 90, referer: str | None = None) -> bytes:
    last = None
    body = urllib.parse.urlencode(data).encode()
    for i in range(tries):
        _sleep_gate()
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers=_headers(
                    extra={"Content-Type": "application/x-www-form-urlencoded",
                           "Accept": "text/csv,application/octet-stream,*/*"},
                    referer=referer or "https://www.taifex.com.tw/cht/3/futDailyMarketView",
                ),
            )
            with _opener.open(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            log(f"  retry {i+1}/{tries} POST {url[:90]}… ({e})")
            time.sleep(SLEEP_SEC * (i + 1))
    raise RuntimeError(f"POST failed {url}: {last}")


# ---------------- parsers / fetchers ----------------
def parse_twse_stock_day_all(raw: bytes) -> list[dict]:
    # Endpoint may return CSV even when response=json is requested.
    text = raw.decode("utf-8-sig")
    if text.lstrip().startswith("{") or text.lstrip().startswith("["):
        d = json.loads(text)
        rows = []
        fields = d.get("fields")
        data = d.get("data") or []
        if fields and data:
            for rec in data:
                item = dict(zip(fields, rec))
                rows.append(_twse_row(item))
            return [r for r in rows if r]
        # MI_INDEX-like tables
        for t in d.get("tables") or []:
            f = t.get("fields") or []
            if f and "證券代號" in f and "收盤價" in f:
                for rec in t.get("data") or []:
                    item = dict(zip(f, rec))
                    rows.append(_twse_row(item))
                return [r for r in rows if r]
        return []
    # CSV
    # skip possible title lines
    lines = text.splitlines()
    header_i = next((i for i, ln in enumerate(lines) if "證券代號" in ln and "收盤價" in ln), None)
    if header_i is None:
        return []
    r = csv.DictReader(io.StringIO("\n".join(lines[header_i:])))
    return [x for x in (_twse_row(row) for row in r) if x]


def _twse_row(row: dict) -> dict | None:
    code = str(row.get("證券代號") or row.get("代號") or "").strip().strip('"')
    name = str(row.get("證券名稱") or row.get("名稱") or "").strip().strip('"')
    if not code:
        return None
    h, l, c = fnum(row.get("最高價")), fnum(row.get("最低價")), fnum(row.get("收盤價"))
    if h is None or l is None or c is None:
        return None
    return {
        "symbol": code,
        "name": name,
        "market": "TWSE",
        "open": fnum(row.get("開盤價")),
        "high": h,
        "low": l,
        "close": c,
        "volume": fnum(row.get("成交股數")),
        "amount": fnum(row.get("成交金額")),
    }


def _date_ok(raw: bytes, d: date) -> bool:
    """Official endpoints silently return the latest day for holidays; reject mismatches."""
    try:
        j = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return False
    return str(j.get("date") or "") == yyyymmdd(d)


def _twse_urls(d: date) -> list[tuple[str, str]]:
    ymd = yyyymmdd(d)
    return [
        (
            "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
            f"?date={ymd}&type=ALLBUT0999&response=json",
            "https://www.twse.com.tw/zh/trading/historical/mi-index.html",
        ),
        (
            "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL"
            f"?date={ymd}&response=json",
            "https://www.twse.com.tw/zh/trading/historical/stock-day-all.html",
        ),
        (
            "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL"
            f"?response=json&date={ymd}",
            "https://www.twse.com.tw/zh/",
        ),
    ]


_DAY_MEMO: dict = {}


def fetch_twse_day(d: date, use_cache: bool = True) -> list[dict] | None:
    if ("twse", d) in _DAY_MEMO:
        return _DAY_MEMO[("twse", d)]
    rows = _fetch_twse_day(d, use_cache=use_cache and d != today_taipei())
    if rows:
        _DAY_MEMO[("twse", d)] = rows
    return rows


def _fetch_twse_day(d: date, use_cache: bool = True) -> list[dict] | None:
    """Today's file is always refetched: the exchange can revise same-day
    figures (e.g. turnover) in the evening; earlier days are final."""
    path = CACHE_TWSE / f"{iso(d)}.json"
    # also accept older .csv cache name
    legacy = CACHE_TWSE / f"{iso(d)}.csv"
    if use_cache:
        for cand in (path, legacy):
            if cand.exists() and cand.stat().st_size > 200:
                rows = parse_twse_stock_day_all(cand.read_bytes())
                if rows:
                    return rows
    log(f"  fetch TWSE {iso(d)}")
    last_err = None
    body = None
    date_mismatch = False
    for url, ref in _twse_urls(d):
        try:
            raw = http_get(url, referer=ref)
        except Exception as e:
            last_err = e
            log(f"    endpoint failed: {e}")
            continue
        if not _date_ok(raw, d):
            # holiday endpoints often echo another date; try next URL before giving up
            date_mismatch = True
            body = raw
            continue
        rows = parse_twse_stock_day_all(raw)
        if rows:
            path.write_bytes(raw)
            return rows
        body = raw
    if date_mismatch:
        return None  # holiday / not published
    if last_err and body is None:
        raise RuntimeError(f"TWSE unreachable for {iso(d)}: {last_err}")
    return None


def parse_tpex_quotes(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig")
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        return []
    tables = d.get("tables") or []
    found = []
    for t in tables:
        title = t.get("title") or ""
        data = t.get("data") or []
        if not data:
            continue
        if title and "管理" in title:
            found.extend(data)
            continue
        if "上櫃" in title or not title:
            found.extend(data)
    rows = []
    for o in found:
        if not isinstance(o, (list, tuple)) or len(o) < 10:
            continue
        code = str(o[0]).strip()
        name = str(o[1]).strip() if len(o) > 1 else ""
        c, o_, h, l = fnum(o[2]), fnum(o[4]), fnum(o[5]), fnum(o[6])
        vol, amt = fnum(o[8]), fnum(o[9])
        if h is None or l is None or c is None:
            continue
        rows.append({
            "symbol": code,
            "name": name,
            "market": "TPEX",
            "open": o_,
            "high": h,
            "low": l,
            "close": c,
            "volume": vol,
            "amount": amt,
        })
    return rows


def _tpex_date_ok(raw: bytes, d: date) -> bool:
    if _date_ok(raw, d):
        return True
    # older APIs may use ROC dates in "date" or "dateForTheList"
    try:
        j = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return False
    candidates = [
        str(j.get("date") or ""),
        str(j.get("dateForTheList") or ""),
        str((j.get("tables") or [{}])[0].get("date") or "") if j.get("tables") else "",
    ]
    want = {yyyymmdd(d), iso(d), roc_slash(d), f"{d.year-1911}/{d.month:02d}/{d.day:02d}",
            f"{d.year}/{d.month:02d}/{d.day:02d}"}
    for c in candidates:
        s = c.replace("-", "/").strip()
        if s in want or s.replace("/", "") in want:
            return True
        # ROC without zero pad e.g. 115/11/13 already in roc_slash
        try:
            parts = s.split("/")
            if len(parts) == 3:
                y, m, dd = int(parts[0]), int(parts[1]), int(parts[2])
                if y < 200:
                    y += 1911
                if date(y, m, dd) == d:
                    return True
        except Exception:
            pass
    return False


def _parse_tpex_legacy(raw: bytes) -> list[dict]:
    """Older stk_quote_result.php uses 張 / 千元; convert to 股 / 元."""
    try:
        j = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return []
    tables = j.get("aaData") or j.get("tables") or []
    # if already handled by parse_tpex_quotes style tables, skip
    if j.get("tables"):
        return []
    rows = []
    for o in tables:
        if not isinstance(o, (list, tuple)) or len(o) < 10:
            continue
        code = str(o[0]).strip()
        name = str(o[1]).strip() if len(o) > 1 else ""
        c, o_, h, l = fnum(o[2]), fnum(o[4]), fnum(o[5]), fnum(o[6])
        vol, amt = fnum(o[8]), fnum(o[9])
        if h is None or l is None or c is None:
            continue
        # 張 → 股, 千元 → 元
        if vol is not None:
            vol = vol * 1000
        if amt is not None:
            amt = amt * 1000
        rows.append({
            "symbol": code, "name": name, "market": "TPEX",
            "open": o_, "high": h, "low": l, "close": c,
            "volume": vol, "amount": amt,
        })
    return rows


def _parse_tpex_openapi(raw: bytes, d: date) -> list[dict]:
    """TPEx OpenAPI mainboard daily close quotes (latest session only)."""
    try:
        arr = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return []
    want = f"{d.year - 1911}{d.month:02d}{d.day:02d}"
    rows = []
    for o in arr if isinstance(arr, list) else []:
        if str(o.get("Date") or "").strip() != want:
            continue
        h, l, c = fnum(o.get("High")), fnum(o.get("Low")), fnum(o.get("Close"))
        if h is None or l is None or c is None:
            continue
        rows.append({
            "symbol": str(o.get("SecuritiesCompanyCode") or "").strip(),
            "name": str(o.get("CompanyName") or "").strip(),
            "market": "TPEX",
            "open": fnum(o.get("Open")), "high": h, "low": l, "close": c,
            "volume": fnum(o.get("TradingShares")),
            "amount": fnum(o.get("TransactionAmount")),
        })
    return rows


_TPEX_OPENAPI: dict = {}


def fetch_tpex_day(d: date, use_cache: bool = True) -> list[dict] | None:
    if ("tpex", d) in _DAY_MEMO:
        return _DAY_MEMO[("tpex", d)]
    rows = _fetch_tpex_day(d, use_cache=use_cache and d != today_taipei())
    if rows:
        _DAY_MEMO[("tpex", d)] = rows
    return rows


def _fetch_tpex_day(d: date, use_cache: bool = True) -> list[dict] | None:
    path = CACHE_TPEX / f"{iso(d)}.json"
    if use_cache and path.exists() and path.stat().st_size > 200:
        raw = path.read_bytes()
        try:
            j = json.loads(raw.decode("utf-8-sig"))
            if isinstance(j, dict) and j.get("openapi_rows"):
                return j["openapi_rows"]
        except Exception:
            pass
        rows = parse_tpex_quotes(raw) or _parse_tpex_legacy(raw)
        if rows:
            return rows
    western = f"{d.year}/{d.month:02d}/{d.day:02d}"
    roc = roc_slash(d)
    endpoints = [
        (
            "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
            f"?date={western}&type=EW&response=json",
            "https://www.tpex.org.tw/zh-tw/mainboard/trading/info/dailyQuotes.html",
            False,
        ),
        (
            "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
            f"?date={roc}&type=EW&response=json",
            "https://www.tpex.org.tw/zh-tw/mainboard/trading/info/dailyQuotes.html",
            False,
        ),
        (
            "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/"
            f"stk_quote_result.php?l=zh-tw&o=json&d={roc}",
            "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote.php?l=zh-tw",
            True,
        ),
    ]
    log(f"  fetch TPEX {iso(d)}")
    # Fast path: OpenAPI snapshot (latest session only; same official numbers)
    if d >= today_taipei() - timedelta(days=4):
        try:
            if "raw" not in _TPEX_OPENAPI:
                _TPEX_OPENAPI["raw"] = b""
                _TPEX_OPENAPI["raw"] = http_get(
                    "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
                    referer="https://www.tpex.org.tw/openapi/", tries=2, timeout=150)
            raw = _TPEX_OPENAPI["raw"]
            rows = _parse_tpex_openapi(raw, d)
            if rows:
                log(f"    TPEX {iso(d)} via OpenAPI ({len(rows)} rows)")
                path.write_text(json.dumps({"date": yyyymmdd(d), "openapi_rows": rows},
                                           ensure_ascii=False), encoding="utf-8")
                return rows
        except Exception as e:
            log(f"    TPEX OpenAPI fallback failed: {e}")
    last_err = None
    date_mismatch = False
    for url, ref, legacy in endpoints:
        try:
            # TPEx is sometimes very slow from cloud runners: bounded tries
            raw = http_get(url, referer=ref, tries=2, timeout=120)
        except Exception as e:
            last_err = e
            log(f"    endpoint failed: {e}")
            continue
        if not _tpex_date_ok(raw, d):
            date_mismatch = True
            continue
        rows = _parse_tpex_legacy(raw) if legacy else parse_tpex_quotes(raw)
        if not rows and not legacy:
            rows = _parse_tpex_legacy(raw)
        if rows:
            path.write_bytes(raw)
            return rows
    if date_mismatch:
        return None
    if last_err:
        raise RuntimeError(f"TPEX unreachable for {iso(d)}: {last_err}")
    return None


def parse_taiex_month(raw: bytes) -> list[dict]:
    d = json.loads(raw.decode("utf-8-sig"))
    rows = []
    data = d.get("data") or []
    for rec in data:
        # date, open, high, low, close (ROC date)
        ds = str(rec[0]).replace("-", "/")
        try:
            y, m, dd = [int(x) for x in ds.split("/")]
            if y < 200:
                y += 1911
            dt = date(y, m, dd)
        except Exception:
            continue
        o, h, l, c = fnum(rec[1]), fnum(rec[2]), fnum(rec[3]), fnum(rec[4])
        if h is None or l is None or c is None:
            continue
        rows.append({"symbol": "TAIEX", "name": "加權指數", "market": "INDEX",
                     "date": dt, "open": o, "high": h, "low": l, "close": c,
                     "volume": None, "amount": None})
    return rows


def fetch_taiex_month(year: int, month: int, use_cache: bool = True) -> list[dict]:
    key = f"{year:04d}-{month:02d}"
    path = CACHE_TAIEX / f"{key}.json"
    if use_cache and path.exists() and path.stat().st_size > 50:
        rows = parse_taiex_month(path.read_bytes())
        if rows:
            return rows
    url = (
        "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
        f"?response=json&date={year:04d}{month:02d}01"
    )
    log(f"  fetch TAIEX {key}")
    raw = http_get(url, referer="https://www.twse.com.tw/zh/trading/indices/mi-5mins-hist.html")
    rows = parse_taiex_month(raw)
    if rows:
        path.write_bytes(raw)
    return rows


def parse_tx_csv(raw: bytes, sessions: tuple[str, ...] = ("一般",)) -> list[dict]:
    """TAIFEX futDataDown CSV. 交易時段 一般 = day session (08:45–13:45);
    盤後 = night session. A 盤後 row dated D is the night session that started
    15:00 on the previous trading day and belongs to trading day D."""
    text = None
    for enc in ("utf-8-sig", "cp950", "big5"):
        try:
            cand = raw.decode(enc)
        except Exception:
            continue
        if "交易日期" in cand or "契約" in cand:
            text = cand
            break
    if not text:
        return []
    r = csv.DictReader(io.StringIO(text))
    rows = []
    for row in r:
        sess = (row.get("交易時段") or "").strip() or "一般"
        if sess not in sessions:
            continue
        ds = (row.get("交易日期") or "").strip().replace("/", "-")
        try:
            dt = datetime.strptime(ds, "%Y-%m-%d").date()
        except Exception:
            continue
        rec = {
            "date": dt,
            "open": fnum(row.get("開盤價")),
            "high": fnum(row.get("最高價")),
            "low": fnum(row.get("最低價")),
            "close": fnum(row.get("收盤價") or row.get("最後成交價")),
            "volume": fnum(row.get("成交量")),
            "settle": fnum(row.get("結算價")),
            "contract": (row.get("到期月份(週別)") or "").strip(),
            "session": "day" if sess == "一般" else "night",
        }
        if rec["high"] is None or rec["low"] is None or rec["close"] is None:
            continue
        rows.append(rec)
    return rows


def fetch_tx_month(year: int, month: int, use_cache: bool = True, sessions: tuple[str, ...] = ("一般",)) -> list[dict]:
    key = f"{year:04d}-{month:02d}"
    path = CACHE_TX / f"{key}.csv"
    if use_cache and path.exists() and path.stat().st_size > 50:
        rows = parse_tx_csv(path.read_bytes(), sessions)
        if rows:
            return rows
    last = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    if month == 2 and year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
        last = 29
    start = f"{year}/{month:02d}/01"
    end = f"{year}/{month:02d}/{last:02d}"
    log(f"  fetch TX {key} {start}–{end}")
    raw = http_post("https://www.taifex.com.tw/cht/3/futDataDown", {
        "down_type": "1",
        "commodity_id": "TX",
        "commodity_id2": "",
        "queryStartDate": start,
        "queryEndDate": end,
    })
    rows = parse_tx_csv(raw, sessions)
    if rows:
        path.write_bytes(raw)
    else:
        # keep failed body for debug
        (CACHE_TX / f"{key}_failed.bin").write_bytes(raw[:4000])
    return rows


def third_wednesday(y: int, m: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(2 - d.weekday()) % 7)
    return d + timedelta(days=14)


def tx_full_contract(asof: date) -> str:
    """Nearest monthly contract that does not expire on/before asof
    (on settlement day the next month is used)."""
    y, m = asof.year, asof.month
    if third_wednesday(y, m) <= asof:
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return f"{y:04d}{m:02d}"


def tx_full_bars(rows_all: list[dict], contract: str) -> list[dict]:
    """Merge day + night rows of one contract per trading day (full session)."""
    by: dict[date, dict] = {}
    for r in rows_all:
        if r.get("contract") != contract:
            continue
        b = by.setdefault(r["date"], {"day": None, "night": None})
        b[r["session"]] = r
    out = []
    for d, b in sorted(by.items()):
        day, night = b["day"], b["night"]
        if not day:
            continue
        hs = [x["high"] for x in (day, night) if x and x.get("high") is not None]
        ls = [x["low"] for x in (day, night) if x and x.get("low") is not None]
        out.append({
            "symbol": "TXF", "name": "台指期全盤", "market": "FUT",
            "date": d, "open": (night or day).get("open"),
            "high": max(hs), "low": min(ls), "close": day["close"],
            "volume": (day.get("volume") or 0) + ((night or {}).get("volume") or 0),
            "amount": None, "contract": contract, "has_night": bool(night),
        })
    return out


def fetch_tx_full_vwap(d: date, contract: str, use_cache: bool = True) -> float | None:
    """Full-session (night + day) VWAP of one TX contract from TAIFEX's public
    daily tick file (published ~16:45 Taipei). Spread trades are excluded."""
    path = CACHE_TXTICK / f"{iso(d)}_{contract}.json"
    if use_cache and path.exists():
        try:
            return json.loads(path.read_text())["vwap"]
        except Exception:
            pass
    url = ("https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/"
           f"Daily_{d.year:04d}_{d.month:02d}_{d.day:02d}.zip")
    try:
        raw = http_get(url, tries=2, timeout=120)
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception as e:
        log(f"  TX tick file not available for {iso(d)} ({e})")
        return None
    pv = Decimal(0)
    vol = Decimal(0)
    day_seen = False
    ymd = yyyymmdd(d)
    for nm in zf.namelist():
        with zf.open(nm) as fh:
            for bline in fh:
                if not bline.startswith(b"2") or b",TX " not in bline[:20]:
                    continue
                parts = bline.decode("ascii", "ignore").split(",")
                if len(parts) < 6 or parts[1].strip() != "TX" or parts[2].strip() != contract:
                    continue
                try:
                    px = Decimal(parts[4].strip())
                    q = Decimal(parts[5].strip())
                except Exception:
                    continue
                t = parts[3].strip()
                if parts[0].strip() == ymd and "084500" <= t <= "134500":
                    day_seen = True
                pv += px * q
                vol += q
    if vol == 0 or not day_seen:
        log(f"  TX tick file for {iso(d)} has no {contract} day trades")
        return None
    vwap = float(pv / vol)
    path.write_text(json.dumps({"vwap": vwap, "contract": contract}))
    return vwap


def tx_front_by_day(rows: list[dict]) -> dict[date, dict]:
    by: dict[date, list[dict]] = {}
    for r in rows:
        by.setdefault(r["date"], []).append(r)
    out = {}
    for d, items in by.items():
        out[d] = max(items, key=lambda x: (x.get("volume") or -1))
    return out


# ---------------- universe / formulas ----------------
def keep_security(row: dict) -> bool:
    name = row.get("name") or ""
    if any(k in name for k in EXCLUDE_NAME):
        return False
    code = row["symbol"]
    if row["high"] is None or row["low"] is None or row["close"] is None:
        return False
    if row["high"] < row["low"] or row["close"] <= 0:
        return False
    # common stocks (4 digits), ETFs/ETNs (00...), preferred shares like 2881A
    if len(code) == 4 and code.isdigit():
        return True
    if code.startswith("00"):
        return True
    if len(code) == 5 and code[:4].isdigit() and code[4].isalpha():
        return True
    return False


def center_bands(h, l, c):
    H, L, C = D(h), D(l), D(c)
    ctr = (9 * H + 9 * L + 10 * C) / 28
    rng = H - L
    upper = ctr + K_BAND * rng
    lower = ctr - K_BAND * rng
    return r2(ctr), r2(upper), r2(lower)


def q_value(h, l, c) -> float:
    H, L, C = D(h), D(l), D(c)
    return r2((13 * H + 13 * L + 10 * C) / 36)


def middle_value(h, l, c, amount, volume, has_vwap: bool) -> float:
    q = (13 * D(h) + 13 * D(l) + 10 * D(c)) / 36
    if has_vwap and amount and volume and volume != 0:
        vwap = D(amount) / D(volume)
        return r2((q + vwap) / 2)
    return r2(q)


def mix_514(hh, ll, c) -> float:
    mid = (D(hh) + D(ll)) / 2
    typ = (D(hh) + D(ll) + D(c)) / 3
    return r2(mid + (D(5) / 14) * (typ - mid))


def pos_flag(close, upper, middle, lower) -> str:
    if close >= upper:
        return "au"      # 站上 上軌
    if close >= middle:
        return "bu"      # 介於 中軌–上軌（含等於中軌）
    if close > lower:
        return "bl"      # 介於 下軌–中軌
    return "dl"          # 跌破 下軌（含等於下軌）


POS_LABEL = {
    "au": "站上 上軌",
    "bu": "介於 中軌–上軌",
    "bl": "介於 下軌–中軌",
    "dl": "跌破 下軌",
}


def compute_one(last: dict, history: list[dict], asof: date, use_vwap: bool) -> dict:
    h, l, c = last["high"], last["low"], last["close"]
    _, upper, lower = center_bands(h, l, c)
    middle = middle_value(h, l, c, last.get("amount"), last.get("volume"), use_vwap)
    mon, fri = week_monday(asof), asof
    ms = month_start(asof)
    week = [b for b in history if mon <= b["date"] <= fri]
    month = [b for b in history if ms <= b["date"] <= asof]
    if not week:
        week = [last]
    if not month:
        month = [last]
    week_line = mix_514(max(b["high"] for b in week), min(b["low"] for b in week), c)
    month_line = mix_514(max(b["high"] for b in month), min(b["low"] for b in month), c)
    pos = pos_flag(c, upper, middle, lower)
    return {
        "symbol": last["symbol"],
        "name": last.get("name") or last["symbol"],
        "market": last.get("market") or "",
        "close": c,
        "open": last.get("open"),
        "high": h,
        "low": l,
        "upper": upper,
        "middle": middle,
        "lower": lower,
        "week_line": week_line,
        "month_line": month_line,
        "pos": pos,
        "pos_label": POS_LABEL[pos],
        "above_week": c >= week_line,
        "above_month": c >= month_line,
        "amount": last.get("amount"),
    }


# ---------------- orchestration ----------------
def today_taipei() -> date:
    return datetime.now(TAIPEI).date()


def resolve_asof(explicit: date | None, use_cache: bool = True) -> date | None:
    """Return the latest session date, or the explicit date.

    If explicit is set but that day has no official data, return None (holiday /
    not yet published). Network failures raise.
    """
    if explicit:
        log(f"probing session {iso(explicit)}")
        rows = fetch_twse_day(explicit, use_cache=use_cache)
        if rows:
            return explicit
        return None
    today = today_taipei()
    last_err = None
    # walk back up to 14 calendar days
    for i in range(0, 15):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        log(f"probing latest session {iso(d)}")
        try:
            rows = fetch_twse_day(d, use_cache=use_cache)
        except Exception as e:
            last_err = e
            log(f"  probe error {iso(d)}: {e}")
            continue
        if rows:
            return d
    if last_err:
        raise SystemExit(f"無法判斷最近交易日（官方行情連線失敗：{last_err}）")
    raise SystemExit("無法判斷最近交易日（連續無資料）")


def load_or_fetch_market_days(start: date, end: date, use_cache: bool = True) -> tuple[dict, dict, list[date]]:
    twse_by_day: dict[date, list[dict]] = {}
    tpex_by_day: dict[date, list[dict]] = {}
    trading: list[date] = []
    for d in daterange(start, end):
        if d.weekday() >= 5:
            continue
        tw = fetch_twse_day(d, use_cache=use_cache)
        tp = fetch_tpex_day(d, use_cache=use_cache)
        if tw:
            twse_by_day[d] = tw
            trading.append(d)
        if tp:
            tpex_by_day[d] = tp
        elif tw:
            log(f"  warn: TPEX missing {iso(d)} (TWSE has data)")
    return twse_by_day, tpex_by_day, trading


def history_map(by_day: dict[date, list[dict]]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for d, rows in sorted(by_day.items()):
        for r in rows:
            rec = dict(r)
            rec["date"] = d
            out.setdefault(r["symbol"], []).append(rec)
    return out



def parse_otc_index_month(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig")
    rows: list[dict] = []
    if text.lstrip().startswith("{") or text.lstrip().startswith("["):
        d = json.loads(text)
        tables = d.get("tables") or []
        data = []
        for tb in tables:
            title = str(tb.get("title") or "")
            if "櫃買指數" in title and tb.get("data"):
                data = tb.get("data") or []
                break
        if not data and tables:
            data = tables[0].get("data") or []
        for rec in data:
            if not isinstance(rec, (list, tuple)) or len(rec) < 5:
                continue
            ds = str(rec[0]).strip().replace("-", "/")
            try:
                parts = [int(x) for x in ds.split("/")]
                y, m, dd = parts[0], parts[1], parts[2]
                if y < 200:
                    y += 1911
                dt = date(y, m, dd)
            except Exception:
                continue
            o, h, l, c = fnum(rec[1]), fnum(rec[2]), fnum(rec[3]), fnum(rec[4])
            if h is None or l is None or c is None:
                continue
            rows.append({
                "symbol": "OTC", "name": "櫃買指數", "market": "INDEX",
                "date": dt, "open": o, "high": h, "low": l, "close": c,
                "volume": None, "amount": None,
            })
        return rows
    # CSV: 資料日期,開市,最高價,最低價,收市,漲跌
    r = csv.DictReader(io.StringIO(text))
    for rec in r:
        ds = str(rec.get("資料日期") or rec.get("日期") or "").strip().strip('"')
        ds = ds.replace("-", "/")
        try:
            if "/" in ds:
                parts = [int(x) for x in ds.split("/")]
                y, m, dd = parts[0], parts[1], parts[2]
            else:
                y, m, dd = int(ds[:4]), int(ds[4:6]), int(ds[6:8])
            if y < 200:
                y += 1911
            dt = date(y, m, dd)
        except Exception:
            continue
        o = fnum(rec.get("開市") or rec.get("開盤"))
        h = fnum(rec.get("最高價") or rec.get("最高"))
        l = fnum(rec.get("最低價") or rec.get("最低"))
        c = fnum(rec.get("收市") or rec.get("收盤"))
        if h is None or l is None or c is None:
            continue
        rows.append({
            "symbol": "OTC", "name": "櫃買指數", "market": "INDEX",
            "date": dt, "open": o, "high": h, "low": l, "close": c,
            "volume": None, "amount": None,
        })
    return rows


def _otc_month_ok(rows: list[dict], year: int, month: int) -> bool:
    return any(r["date"].year == year and r["date"].month == month for r in rows)


def fetch_otc_month(year: int, month: int, use_cache: bool = True) -> list[dict]:
    """Official monthly OHLC for the OTC / TPEx capitalization-weighted index."""
    key = f"{year:04d}-{month:02d}"
    path = CACHE_OTC / f"{key}.json"
    if use_cache and path.exists() and path.stat().st_size > 50:
        rows = parse_otc_index_month(path.read_bytes())
        if _otc_month_ok(rows, year, month):
            return rows
    url = (
        "https://www.tpex.org.tw/www/zh-tw/indexInfo/inx"
        f"?date={year:04d}/{month:02d}/01&response=json"
    )
    log(f"  fetch OTC index {key}")
    raw = http_get(url, referer="https://www.tpex.org.tw/zh-tw/indices/stock-index/industrial/inxh.html")
    rows = parse_otc_index_month(raw)
    if _otc_month_ok(rows, year, month):
        path.write_bytes(raw)
        return rows
    # Fallback: OpenAPI snapshot (typically the current month only)
    try:
        raw2 = http_get(
            "https://www.tpex.org.tw/openapi/v1/tpex_index",
            referer="https://www.tpex.org.tw/openapi/",
        )
        alt = []
        for rec in json.loads(raw2.decode("utf-8-sig")):
            ds = str(rec.get("Date") or "")
            if len(ds) != 8:
                continue
            dt = date(int(ds[:4]), int(ds[4:6]), int(ds[6:8]))
            if dt.year != year or dt.month != month:
                continue
            h, l, c = fnum(rec.get("High")), fnum(rec.get("Low")), fnum(rec.get("Close"))
            if h is None or l is None or c is None:
                continue
            alt.append({
                "symbol": "OTC", "name": "櫃買指數", "market": "INDEX",
                "date": dt, "open": fnum(rec.get("Open")), "high": h, "low": l, "close": c,
                "volume": None, "amount": None,
            })
        if _otc_month_ok(alt, year, month):
            path.write_text(json.dumps(json.loads(raw2.decode("utf-8-sig")), ensure_ascii=False), encoding="utf-8")
            return alt
    except Exception as e:
        log(f"    OTC OpenAPI fallback failed: {e}")
    log(f"  WARN: OTC index missing {key}")
    return rows


def verify(rows_by_symbol: dict[str, dict], asof: date) -> dict:
    if not GOLDEN.exists():
        return {"skipped": True, "reason": "no golden file"}
    checks = []
    with GOLDEN.open() as f:
        for rec in csv.DictReader(f):
            if rec.get("date") and rec["date"] != iso(asof):
                continue
            sym = rec["symbol"]
            got = rows_by_symbol.get(sym)
            if not got:
                checks.append({"symbol": sym, "ok": False, "reason": "missing in output"})
                continue
            ok = True
            diffs = {}
            for k_src, k_out in (
                ("upper", "upper"), ("middle", "middle"), ("lower", "lower"),
                ("week_line", "week_line"), ("month_line", "month_line"),
            ):
                if k_src not in rec:
                    continue
                exp = fnum(rec[k_src])
                if exp is None:
                    continue
                err = abs(got[k_out] - exp)
                diffs[k_src] = {"expected": exp, "got": got[k_out], "err": round(err, 6)}
                if err > 1e-9:
                    ok = False
            checks.append({"symbol": sym, "ok": ok, "diffs": diffs})
    n = sum(1 for c in checks if c.get("ok") is True)
    n_fail = sum(1 for c in checks if c.get("ok") is False)
    return {
        "asof": iso(asof),
        "checked": n + n_fail,
        "passed": n,
        "failed": n_fail,
        "all_zero_error": n_fail == 0 and n > 0,
        "details": checks,
    }


def slim_row(r: dict) -> dict:
    keep = ["symbol", "name", "market", "close", "upper", "middle", "lower",
            "week_line", "month_line", "pos", "pos_label", "above_week", "above_month"]
    o = {k: r[k] for k in keep if k in r}
    if r.get("contract"):
        o["contract"] = r["contract"]
    if r.get("middle_pending"):
        o["middle_pending"] = True
    return o


def build_payload(asof: date, stocks: list[dict], indices: list[dict], ver: dict) -> dict:
    return {
        "date": iso(asof),
        "generated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "timezone": "Asia/Taipei",
        "sources": {
            "TWSE": "MI_INDEX / STOCK_DAY_ALL (official daily close, volume in shares, amount in TWD)",
            "TPEX": "dailyQuotes (official shares and TWD; legacy fallback converts 張/千元)",
            "TAIEX": "TWSE MI_5MINS_HIST",
            "TX": "TAIFEX futDataDown, day session, front month by volume",
            "TXF": "TAIFEX futDataDown day+night rows of the nearest non-expiring month; middle uses full-session VWAP from TAIFEX daily tick file",
            "OTC": "TPEx indexInfo/inx monthly OHLC (official open/high/low/close)",
        },
        "counts": {
            "twse": sum(1 for r in stocks if r["market"] == "TWSE"),
            "tpex": sum(1 for r in stocks if r["market"] == "TPEX"),
            "stocks": len(stocks),
            "indices": len(indices),
        },
        "indices": [slim_row(r) for r in indices],
        "stocks": [slim_row(r) for r in stocks],
        "verification": {
            "checked": ver.get("checked"),
            "passed": ver.get("passed"),
            "failed": ver.get("failed"),
            "all_zero_error": ver.get("all_zero_error"),
            "skipped": ver.get("skipped"),
            "reason": ver.get("reason"),
        },
    }


def payload_equal(a: dict, b: dict) -> bool:
    def norm(x):
        if not x:
            return None
        y = dict(x)
        y.pop("generated_at", None)
        return y
    return norm(a) == norm(b)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compute custom band levels for all TWSE/TPEX names.")
    ap.add_argument("--date", help="As-of ISO date (default: latest trading day, Asia/Taipei)")
    ap.add_argument("--no-cache", action="store_true", help="Refetch even if cache exists")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="Output JSON path")
    args = ap.parse_args(argv)
    use_cache = not args.no_cache
    out = Path(args.out)
    if not out.is_absolute():
        out = (Path.cwd() / out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    warmup()

    # Day files (TWSE/TPEx/tick) are immutable once published and are kept in
    # the Actions cache between runs. Month files grow daily: always refetch.
    cutoff = today_taipei() - timedelta(days=70)
    for folder in (CACHE_TAIEX, CACHE_TX, CACHE_OTC):
        for f in folder.glob("*"):
            f.unlink()
    for folder in (CACHE_TWSE, CACHE_TPEX, CACHE_TXTICK):
        for f in folder.glob("*"):
            try:
                if date.fromisoformat(f.name[:10]) < cutoff:
                    f.unlink()
            except ValueError:
                pass

    explicit = None
    if args.date:
        explicit = datetime.strptime(args.date, "%Y-%m-%d").date()
    asof = resolve_asof(explicit, use_cache=use_cache)
    if asof is None:
        log(f"no official session data for {iso(explicit)} (holiday or not yet published); no-op")
        return 0
    log(f"as-of {iso(asof)}")

    start = window_start(asof)
    log(f"loading market days {iso(start)} → {iso(asof)} (ISO week Mon {iso(week_monday(asof))}, month start {iso(month_start(asof))})")
    if not use_cache:
        for d in daterange(start, asof):
            for cand in (
                CACHE_TWSE / f"{iso(d)}.json",
                CACHE_TWSE / f"{iso(d)}.csv",
                CACHE_TPEX / f"{iso(d)}.json",
            ):
                if cand.exists():
                    cand.unlink()
        for y, m in {(start.year, start.month), (asof.year, asof.month)}:
            for folder, ext in ((CACHE_TAIEX, ".json"), (CACHE_TX, ".csv"), (CACHE_OTC, ".json")):
                cand = folder / f"{y:04d}-{m:02d}{ext}"
                if cand.exists():
                    cand.unlink()

    twse_by_day, tpex_by_day, trading = load_or_fetch_market_days(start, asof, use_cache=use_cache)
    if asof not in twse_by_day:
        log(f"TWSE has no data for {iso(asof)}; no-op")
        return 0
    log(f"trading days loaded: {', '.join(iso(d) for d in trading)}")
    log(f"TWSE names on as-of: {len(twse_by_day[asof])}; TPEX: {len(tpex_by_day.get(asof) or [])}")

    months = {(start.year, start.month), (asof.year, asof.month)}
    taiex_rows = []
    tx_rows = []
    otc_rows = []
    for y, m in sorted(months):
        taiex_rows.extend(fetch_taiex_month(y, m, use_cache=use_cache))
        tx_rows.extend(fetch_tx_month(y, m, use_cache=use_cache))
        otc_rows.extend(fetch_otc_month(y, m, use_cache=use_cache))
    taiex_hist = [r for r in taiex_rows if start <= r["date"] <= asof]
    tx_front = tx_front_by_day(tx_rows)
    tx_hist = []
    for d, rec in sorted(tx_front.items()):
        if start <= d <= asof:
            tx_hist.append({
                "symbol": "TX", "name": "台指期近月", "market": "FUT",
                "date": d, "open": rec["open"], "high": rec["high"], "low": rec["low"],
                "close": rec["close"], "volume": rec.get("volume"), "amount": None,
                "contract": rec.get("contract"),
            })

    twse_hist = history_map(twse_by_day)
    tpex_hist = history_map(tpex_by_day)

    stocks: list[dict] = []
    seen = set()
    for market, hist, asof_rows, use_vwap in (
        ("TWSE", twse_hist, twse_by_day[asof], True),
        ("TPEX", tpex_hist, tpex_by_day.get(asof) or [], True),
    ):
        for row in asof_rows:
            if not keep_security(row):
                continue
            if row["symbol"] in seen:
                continue
            last = dict(row)
            last["date"] = asof
            hist_bars = hist.get(row["symbol"], [last])
            if not any(b["date"] == asof for b in hist_bars):
                hist_bars = hist_bars + [last]
            rec = compute_one(last, hist_bars, asof, use_vwap=use_vwap)
            rec["market"] = market
            stocks.append(rec)
            seen.add(row["symbol"])

    stocks.sort(key=lambda r: r["symbol"])

    indices = []
    taiex_last = next((r for r in reversed(taiex_hist) if r["date"] == asof), None)
    if taiex_last:
        indices.append(compute_one(taiex_last, taiex_hist, asof, use_vwap=False))
    else:
        log("WARN: TAIEX missing as-of bar")
    tx_last = next((r for r in reversed(tx_hist) if r["date"] == asof), None)
    if tx_last:
        rec = compute_one(tx_last, tx_hist, asof, use_vwap=False)
        rec["name"] = "台指期近月" + (f"（{tx_last.get('contract')}）" if tx_last.get("contract") else "")
        rec["contract"] = tx_last.get("contract")
        indices.append(rec)
    else:
        log("WARN: TX missing as-of bar")
    # TX full session (night + day), contract month = nearest not expiring today
    tx_all = []
    for y, m in sorted(months):
        tx_all.extend(fetch_tx_month(y, m, use_cache=True, sessions=("一般", "盤後")))
    fc = tx_full_contract(asof)
    txf_hist = [b for b in tx_full_bars(tx_all, fc) if start <= b["date"] <= asof]
    txf_last = next((b for b in txf_hist if b["date"] == asof), None)
    if txf_last and txf_last["has_night"]:
        vwap = fetch_tx_full_vwap(asof, fc, use_cache=use_cache)
        rec = compute_one(txf_last, txf_hist, asof, use_vwap=False)
        if vwap is not None:
            rec["middle"] = middle_value(txf_last["high"], txf_last["low"], txf_last["close"],
                                         Decimal(str(vwap)), Decimal(1), True)
            rec["pos"] = pos_flag(rec["close"], rec["upper"], rec["middle"], rec["lower"])
            rec["pos_label"] = POS_LABEL[rec["pos"]]
        else:
            rec["middle_pending"] = True
        rec["name"] = f"台指期全盤（{fc}）"
        rec["contract"] = fc
        indices.append(rec)
    else:
        log("WARN: TX full-session bar missing")

    otc_hist = [r for r in otc_rows if start <= r["date"] <= asof]
    otc_last = next((r for r in reversed(otc_hist) if r["date"] == asof), None)
    if otc_last:
        indices.append(compute_one(otc_last, otc_hist, asof, use_vwap=False))
    else:
        log("WARN: OTC index missing as-of bar")

    by_sym = {r["symbol"]: r for r in stocks}
    for r in indices:
        by_sym[r["symbol"]] = r
    ver = verify(by_sym, asof)
    if ver.get("all_zero_error"):
        log(f"verification: PASS {ver['passed']}/{ver['checked']} (0 error)")
    elif ver.get("checked") == 0:
        log("verification: no reference values for this date (normal)")
    elif ver.get("skipped"):
        log(f"verification skipped: {ver.get('reason')}")
    else:
        log(f"verification: FAIL passed={ver.get('passed')} failed={ver.get('failed')}")
        for c in ver.get("details") or []:
            if c.get("ok") is False:
                log(f"  {c}")

    payload = build_payload(asof, stocks, indices, ver)
    existing = None
    if out.exists():
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            existing = None
    if payload_equal(existing, payload):
        log(f"unchanged vs {out} (date {payload['date']}); not rewritten")
        return 0
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"wrote {out} ({out.stat().st_size} bytes, {payload['counts']['stocks']} stocks)")
    return 0 if (ver.get("all_zero_error") or ver.get("skipped") or ver.get("checked") == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
