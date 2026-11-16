"""US equity market (NYSE/Nasdaq) full-day holidays and session helpers."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def _easter(y: int) -> date:
    a = y % 19; b = y // 100; c = y % 100
    d = b // 4; e = b % 4; f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4; k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(y, month, day)


def _nth(y, m, wd, n):
    d = date(y, m, 1)
    d += timedelta(days=(wd - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last(y, m, wd):
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - wd) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def holidays(y: int) -> set[date]:
    hs = set()
    ny = date(y, 1, 1)
    if ny.weekday() != 5:          # Sat New Year is not observed on the Friday before
        hs.add(_observed(ny))
    hs.add(_nth(y, 1, 0, 3))       # MLK
    hs.add(_nth(y, 2, 0, 3))       # Presidents
    hs.add(_easter(y) - timedelta(days=2))  # Good Friday
    hs.add(_last(y, 5, 0))         # Memorial
    if y >= 2022:
        hs.add(_observed(date(y, 6, 19)))   # Juneteenth
    hs.add(_observed(date(y, 7, 4)))
    hs.add(_nth(y, 9, 0, 1))       # Labor
    hs.add(_nth(y, 11, 3, 4))      # Thanksgiving
    hs.add(_observed(date(y, 12, 25)))
    return hs


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def next_trading_day(d: date) -> date:
    x = d + timedelta(days=1)
    while not is_trading_day(x):
        x += timedelta(days=1)
    return x


def prev_trading_day(d: date) -> date:
    x = d - timedelta(days=1)
    while not is_trading_day(x):
        x -= timedelta(days=1)
    return x


def last_completed_session(now: datetime | None = None, settle_min: int = 30) -> date:
    """Latest US session whose regular close (16:00 ET) + settle_min has passed."""
    now = (now or datetime.now(NY)).astimezone(NY)
    d = now.date()
    if is_trading_day(d) and (now.hour, now.minute) >= (16, settle_min):
        return d
    return prev_trading_day(d)
