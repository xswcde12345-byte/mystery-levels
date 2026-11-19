#!/usr/bin/env python3
"""Cheap gate for scheduled runs: print run=false when data/latest.json already
holds today's final data (all four index cards, OTC stocks present, and the
full-session middle line no longer pending). Otherwise run=true."""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


def decide() -> tuple[bool, str]:
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    if now.weekday() >= 5:
        return False, "weekend"
    if now.hour < 13:
        return False, "before the TW close (US-index runs only)"
    try:
        j = json.load(open("data/latest.json", encoding="utf-8"))
    except Exception as e:
        return True, f"no readable latest.json ({e})"
    if j.get("date") != now.date().isoformat():
        return True, f"latest.json is {j.get('date')}, today is {now.date()}"
    idx = {r.get("symbol"): r for r in j.get("indices") or []}
    missing = [s for s in ("TAIEX", "TX", "TXF", "OTC") if s not in idx]
    if missing:
        return True, f"today's cards missing: {missing}"
    if any(r.get("middle_pending") for r in idx.values()):
        return True, "full-session middle line still pending"
    if not (j.get("counts") or {}).get("tpex"):
        return True, "OTC stocks missing"
    try:
        gen = datetime.fromisoformat(j.get("generated_at"))
    except Exception:
        return True, "no generated_at"
    # Same-day exchange figures can still be revised in the early evening;
    # keep refreshing until one run has completed at/after 19:00 Taipei.
    if gen.astimezone(ZoneInfo("Asia/Taipei")).hour < 19:
        return True, f"generated {gen:%H:%M}, refresh until a post-19:00 run"
    return False, "today's data already final"


def decide_us() -> tuple[bool, str]:
    """US indices are independent of the TW session: run when us.json is older
    than the latest finished US session (S&P 500 / Nasdaq Composite / PHLX Semiconductor)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import us_calendar as uc
    want = uc.last_completed_session().isoformat()
    try:
        j = json.load(open("data/us.json", encoding="utf-8"))
    except Exception as e:
        return True, f"no readable us.json ({e})"
    have = {r.get("symbol"): r.get("date") for r in j.get("indices") or []}
    stale = [s for s in ("SPX", "IXIC", "SOX") if (have.get(s) or "") < want]
    if stale:
        return True, f"US session {want} missing for {stale}"
    return False, f"US data already has {want}"


def decide_fear() -> tuple[bool, str]:
    """台股恐慌度：最近一個（已收盤的）交易日還沒寫進 data/fear/tw_ind.csv 就跑。"""
    from datetime import timedelta
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    d = now.date() if now.hour >= 14 else now.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    try:
        last = open("data/fear/tw_ind.csv").read().strip().splitlines()[-1].split(",")[0]
    except Exception as e:
        return True, f"no tw_ind.csv ({e})"
    if last >= d.isoformat():
        return False, f"tw_ind.csv already has {last}"
    return True, f"tw_ind.csv last {last}, want {d}"


def decide_fear_us() -> tuple[bool, str]:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import us_calendar as uc
    want = uc.last_completed_session().isoformat()
    try:
        have = min(json.load(open(f"data/fear/{k}.json", encoding="utf-8"))["asof"] for k in ("us", "sox"))
    except Exception as e:
        return True, f"no fear us/sox json ({e})"
    return (have < want), f"fear us/sox asof {have}, want {want}"


run, why = decide()
run_fear, why_fear = decide_fear()
run_fear_us, why_fear_us = decide_fear_us()
print(f"gate: run_fear={str(run_fear).lower()} ({why_fear})")
print(f"gate: run_fear_us={str(run_fear_us).lower()} ({why_fear_us})")
run_us, why_us = decide_us()
print(f"gate: run={str(run).lower()} ({why})")
print(f"gate: run_us={str(run_us).lower()} ({why_us})")
out = os.environ.get("GITHUB_OUTPUT")
if out:
    with open(out, "a") as f:
        f.write(f"run={str(run).lower()}\n")
        f.write(f"run_us={str(run_us).lower()}\n")
        f.write(f"run_fear={str(run_fear).lower()}\n")
        f.write(f"run_fear_us={str(run_fear_us).lower()}\n")
sys.exit(0)
