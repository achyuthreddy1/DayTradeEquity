"""
Is the NSE equity market open on a given date?

WHY THIS EXISTS
---------------
Before this, nothing in the system knew what a holiday was. On a closed day
live.py would fetch ~100 symbols x 20 days of history, wait for 09:30, find
no bars for today, and print:

    "no setup cleared 5.0x relative volume. Nothing to trade today."

which is exactly what a genuinely quiet trading day prints. That is the same
silent-failure class the README already calls out for expired tokens: a
broken condition wearing a normal condition's clothes. A holiday must be
identified from the CALENDAR, never inferred from absent data.

SOURCE
------
NSE's own published trading holiday master:
    https://www.nseindia.com/api/holiday-master?type=trading
segment "CM" (capital market / equities). Fetched live, then cached per year
under .nse_status/. Maintainable for future years with no code change -- NSE
publishes the next year's list each December and the cache refreshes on its
own schedule.

FAIL CLOSED
-----------
If the calendar for the requested year cannot be established -- no network,
no cache, NSE blocking the VPS -- this reports UNKNOWN and the caller must
NOT trade. An unknown day is treated as a day we decline to trade, never as
an open one. Deciding to trade requires positive evidence the market is open.

IST IS DEFINED LOCALLY HERE, ON PURPOSE
---------------------------------------
paper_config.now_ist() exists and is correct, but this module sits in the
infrastructure layer BELOW the frozen strategy and must not depend on it.
The duplication is deliberate and test_session_guard.py asserts the two
clocks agree, so they cannot silently diverge.

THIS MODULE NEVER DECIDES A TRADE. It decides whether a session starts.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / ".nse_status"
HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
SEGMENT = "CM"                      # capital market = cash equities

# Re-fetch this often even when a cached year exists: NSE occasionally adds a
# holiday mid-year (an election day, a state funeral), and a cache pinned for
# twelve months would miss it.
CACHE_MAX_AGE_DAYS = 7

UNKNOWN = "unknown"


@dataclass(frozen=True)
class DayStatus:
    """Why the market is or is not open. `open_` is None when undetermined."""
    day: date
    open_: bool | None
    reason: str
    source: str = ""                # live | cache | -

    @property
    def is_open(self) -> bool:
        """True ONLY on positive evidence. Unknown is never open."""
        return self.open_ is True

    def line(self) -> str:
        if self.open_ is True:
            return f"NSE MARKET OPEN | date={self.day} | reason={self.reason}"
        if self.open_ is False:
            return f"NSE MARKET CLOSED | date={self.day} | reason={self.reason}"
        return f"NSE CALENDAR UNKNOWN | date={self.day} | reason={self.reason}"


def today_ist(now: datetime | None = None) -> date:
    """Today's date in IST, whatever the host's timezone is set to.

    An Oracle VPS defaults to UTC. Between 00:00 and 05:30 IST a UTC host is
    still on YESTERDAY's date, so a naive date.today() would apply Sunday's
    verdict to Monday morning.
    """
    return (now or datetime.now(IST)).astimezone(IST).date()


def _parse_nse_date(text: str) -> date | None:
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def _cache_path(year: int) -> Path:
    return CACHE_DIR / f"nse-holidays-{year}.json"


def _fetch_live(year: int) -> dict[date, str] | None:
    """Pull the holiday master from NSE. None on any failure."""
    try:
        import requests
        from nifty100_universe import BROWSER_HEADERS, NSE_HOME_URL
        s = requests.Session()
        s.headers.update(BROWSER_HEADERS)
        s.get(NSE_HOME_URL, timeout=10)        # NSE needs a cookie warm-up
        r = s.get(HOLIDAY_URL, timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception:
        return None

    rows = payload.get(SEGMENT) if isinstance(payload, dict) else None
    if not rows:
        return None
    out: dict[date, str] = {}
    for row in rows:
        d = _parse_nse_date(str(row.get("tradingDate", "")))
        if d is not None:
            out[d] = str(row.get("description", "holiday")).strip() or "holiday"
    if not out:
        return None
    # The feed carries whichever years NSE currently publishes; keep the year
    # asked for. An empty result for that year means "not published yet",
    # which is a genuine unknown rather than "no holidays".
    return out if any(d.year == year for d in out) else None


def _write_cache(holidays: dict[date, str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    by_year: dict[int, dict[str, str]] = {}
    for d, desc in holidays.items():
        by_year.setdefault(d.year, {})[d.isoformat()] = desc
    for year, entries in by_year.items():
        _cache_path(year).write_text(json.dumps(
            {"fetched": datetime.now(IST).isoformat(timespec="seconds"),
             "holidays": entries}, indent=1))


def _read_cache(year: int) -> tuple[dict[date, str] | None, bool]:
    """(holidays, is_stale). Missing or unreadable cache returns (None, True)."""
    p = _cache_path(year)
    if not p.exists():
        return None, True
    try:
        blob = json.loads(p.read_text())
        entries = blob.get("holidays") or {}
        out = {}
        for k, v in entries.items():
            d = _parse_nse_date(k)
            if d is not None:
                out[d] = v
        fetched = datetime.fromisoformat(blob["fetched"])
        stale = (datetime.now(IST) - fetched) > timedelta(days=CACHE_MAX_AGE_DAYS)
        return (out or None), stale
    except Exception:
        return None, True


def holidays_for(year: int, allow_network: bool = True
                 ) -> tuple[dict[date, str] | None, str]:
    """(holiday map, source). None means the year could not be established."""
    cached, stale = _read_cache(year)
    if cached is not None and not stale:
        return cached, "cache"
    if allow_network:
        live = _fetch_live(year)
        if live is not None:
            _write_cache(live)
            return {d: n for d, n in live.items()}, "live"
    if cached is not None:
        # Stale beats nothing: the list changes rarely, and refusing to trade
        # for a week because NSE is unreachable is its own failure.
        return cached, "cache(stale)"
    return None, "-"


def status(day: date | None = None, allow_network: bool = True) -> DayStatus:
    """Is NSE equity trading open on `day`? Defaults to today in IST."""
    day = day or today_ist()

    # Weekend first: it needs no calendar, so a network failure can never
    # make a Saturday look tradeable.
    if day.weekday() >= 5:
        return DayStatus(day, False,
                         "weekend (" + day.strftime("%A") + ")", "-")

    holidays, source = holidays_for(day.year, allow_network)
    if holidays is None:
        return DayStatus(
            day, None,
            f"NSE holiday calendar for {day.year} unavailable "
            f"(no network and no cache) -- refusing to trade a day we "
            f"cannot verify", "-")

    if day in holidays:
        return DayStatus(day, False, holidays[day], source)
    return DayStatus(day, True, "regular trading day", source)


def refresh(years: list[int] | None = None) -> bool:
    """Force a live fetch and cache write. True if anything was cached."""
    years = years or [today_ist().year, today_ist().year + 1]
    ok = False
    for y in years:
        live = _fetch_live(y)
        if live:
            _write_cache(live)
            ok = True
    return ok


if __name__ == "__main__":
    import sys
    if "--refresh" in sys.argv:
        print("  refreshed" if refresh() else "  refresh FAILED (NSE unreachable)")
        sys.exit(0)
    arg = [a for a in sys.argv[1:] if not a.startswith("-")]
    d = _parse_nse_date(arg[0]) if arg else None
    st = status(d)
    print("  " + st.line() + f"  [source={st.source}]")
    sys.exit(0 if st.is_open else 1)
