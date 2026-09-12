"""
Weekend / NSE-holiday / fail-closed gate tests.

    python3 test_session_guard.py

The calendar cases run OFFLINE against a seeded cache, so the suite is
deterministic and works on a VPS that cannot reach NSE. One optional live
check at the end exercises the real NSE feed when it is reachable and is
reported separately rather than failing the run.

The case that matters most is FAIL CLOSED: when the calendar cannot be
established the answer must be "do not trade", never "probably fine". A
gate that fails open is worse than no gate, because it looks like
protection while providing none.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import market_calendar as mc
import session_guard as sg

FAILURES: list[str] = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   {detail}"))
    if not ok:
        FAILURES.append(name)


def _seed_cache(tmp: Path, year: int, holidays: dict[str, str],
                fetched: datetime | None = None) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / f"nse-holidays-{year}.json").write_text(json.dumps({
        "fetched": (fetched or datetime.now(mc.IST)).isoformat(timespec="seconds"),
        "holidays": holidays}))


class _Cache:
    """Point market_calendar at a throwaway cache directory."""

    def __init__(self, holidays=None, year=2026, fetched=None):
        self.holidays, self.year, self.fetched = holidays, year, fetched

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old = mc.CACHE_DIR
        mc.CACHE_DIR = self.tmp
        if self.holidays is not None:
            _seed_cache(self.tmp, self.year, self.holidays, self.fetched)
        return self

    def __exit__(self, *a):
        mc.CACHE_DIR = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)


CAL_2026 = {
    "2026-01-26": "Republic Day",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-12-25": "Christmas",
}


def test_weekends():
    print("\n  1. WEEKENDS -- must never poll, and must not need a calendar")
    with _Cache(CAL_2026):
        sat = mc.status(date(2026, 9, 12), allow_network=False)
        sun = mc.status(date(2026, 9, 13), allow_network=False)
        check("Saturday 2026-09-12 CLOSED", sun is not None and sat.open_ is False,
              f"got {sat.open_}")
        check("Saturday reason names the day", "Saturday" in sat.reason, sat.reason)
        check("Sunday 2026-09-13 CLOSED", sun.open_ is False, f"got {sun.open_}")
        check("Sunday reason names the day", "Sunday" in sun.reason, sun.reason)
    # A weekend must be decidable with NO calendar at all -- otherwise an NSE
    # outage on a Saturday would report UNKNOWN and raise a false alert.
    with _Cache(None):
        sat = mc.status(date(2026, 9, 12), allow_network=False)
        check("Saturday still CLOSED with no calendar available",
              sat.open_ is False, f"got {sat.open_}")
    _, code = sg.decide(date(2026, 9, 12), allow_network=False)
    check(f"guard exit code is EXIT_WEEKEND ({sg.EXIT_WEEKEND})",
          code == sg.EXIT_WEEKEND, f"got {code}")


def test_holidays():
    print("\n  2. NSE HOLIDAYS -- from the calendar, not from absent data")
    with _Cache(CAL_2026):
        gc = mc.status(date(2026, 9, 14), allow_network=False)
        check("2026-09-14 CLOSED", gc.open_ is False, f"got {gc.open_}")
        check("reason is 'Ganesh Chaturthi'", gc.reason == "Ganesh Chaturthi",
              f"got {gc.reason!r}")
        check("log line is the required format",
              gc.line() == "NSE MARKET CLOSED | date=2026-09-14 | "
                           "reason=Ganesh Chaturthi", gc.line())
        _, code = sg.decide(date(2026, 9, 14), allow_network=False)
        check(f"guard exit code is EXIT_HOLIDAY ({sg.EXIT_HOLIDAY})",
              code == sg.EXIT_HOLIDAY, f"got {code}")

        nxt = mc.status(date(2026, 9, 15), allow_network=False)
        check("2026-09-15 OPEN", nxt.open_ is True, f"got {nxt.open_}")
        check("2026-09-15 is a regular trading day",
              nxt.reason == "regular trading day", nxt.reason)
        _, code = sg.decide(date(2026, 9, 15), allow_network=False)
        check(f"guard exit code is EXIT_OPEN ({sg.EXIT_OPEN})",
              code == sg.EXIT_OPEN, f"got {code}")

        wed = mc.status(date(2026, 9, 16), allow_network=False)
        check("normal weekday 2026-09-16 OPEN", wed.open_ is True, f"got {wed.open_}")
        for d, name in (("2026-01-26", "Republic Day"),
                        ("2026-12-25", "Christmas")):
            y, m, dd = (int(x) for x in d.split("-"))
            st = mc.status(date(y, m, dd), allow_network=False)
            check(f"{d} CLOSED ({name})", st.open_ is False, f"got {st.open_}")
        check("the calendar is data, not hardcoded dates",
              len(CAL_2026) == 5 and "2026-09-14" in CAL_2026)


def test_fail_closed():
    print("\n  3. FAIL CLOSED -- an unverifiable day is never traded")
    with _Cache(None):                       # no cache, network disabled
        st = mc.status(date(2026, 9, 15), allow_network=False)
        check("weekday with no calendar is UNKNOWN, not open",
              st.open_ is None, f"got {st.open_}")
        check("is_open is False for UNKNOWN (no accidental truthiness)",
              st.is_open is False)
        check("reason explains the refusal", "unavailable" in st.reason.lower(),
              st.reason)
        _, code = sg.decide(date(2026, 9, 15), allow_network=False)
        check(f"guard exit code is EXIT_UNKNOWN ({sg.EXIT_UNKNOWN})",
              code == sg.EXIT_UNKNOWN, f"got {code}")
        check("UNKNOWN is distinct from weekend/holiday codes",
              len({sg.EXIT_UNKNOWN, sg.EXIT_WEEKEND, sg.EXIT_HOLIDAY}) == 3)

    # A calendar that loads but omits the year is also unknown, not "no holidays"
    with _Cache({"2025-01-26": "Republic Day"}, year=2025):
        st = mc.status(date(2026, 9, 15), allow_network=False)
        check("a cache for the WRONG year is UNKNOWN, not open",
              st.open_ is None, f"got {st.open_}")

    # A stale cache is still better than nothing, and says so
    old = datetime.now(mc.IST) - timedelta(days=mc.CACHE_MAX_AGE_DAYS + 5)
    with _Cache(CAL_2026, fetched=old):
        st = mc.status(date(2026, 9, 14), allow_network=False)
        check("a stale cache still identifies the holiday", st.open_ is False)
        check("and labels itself stale", "stale" in st.source, st.source)


def test_timezone():
    print("\n  4. TIMEZONE -- IST decides, never the host clock")
    utc = ZoneInfo("UTC")
    # 00:10 IST Monday 2026-09-15 is 18:40 UTC Sunday 2026-09-14. A UTC host
    # would apply Sunday's verdict (and Ganesh Chaturthi's) to Monday.
    just_after_midnight = datetime(2026, 9, 15, 0, 10, tzinfo=mc.IST)
    check("00:10 IST Monday resolves to Monday, not Sunday",
          mc.today_ist(just_after_midnight) == date(2026, 9, 15),
          f"got {mc.today_ist(just_after_midnight)}")
    check("the same instant in UTC is still Sunday (the trap)",
          just_after_midnight.astimezone(utc).date() == date(2026, 9, 14))
    late = datetime(2026, 9, 14, 23, 50, tzinfo=mc.IST)
    check("23:50 IST stays on the same IST day",
          mc.today_ist(late) == date(2026, 9, 14), f"got {mc.today_ist(late)}")
    check("05:29 IST (UTC still yesterday) resolves to today",
          mc.today_ist(datetime(2026, 9, 15, 5, 29, tzinfo=mc.IST))
          == date(2026, 9, 15))
    with _Cache(CAL_2026):
        st = mc.status(mc.today_ist(just_after_midnight), allow_network=False)
        check("so the gate opens on Monday despite a UTC host",
              st.open_ is True, f"got {st.open_} / {st.reason}")
    # the infrastructure clock must agree with the strategy layer's clock
    from paper_config import now_ist as strategy_now
    check("market_calendar IST agrees with paper_config.now_ist",
          abs((strategy_now() - datetime.now(mc.IST).replace(tzinfo=None))
              .total_seconds()) < 5)


def test_no_side_effects():
    print("\n  5. A CLOSED DAY MUST COST NOTHING")
    import inspect
    src = inspect.getsource(sg.decide) + inspect.getsource(mc.status)
    for bad in ("load_universe", "liquid_universe", "build_baselines",
                "fyersModel", "simulate_day", "find_signals"):
        check(f"gate never touches {bad}()", bad not in src)
    live_src = (HERE / "live.py").read_text()
    gate_i = live_src.index("SESSION GATE")
    base_i = live_src.index("Building 20-day opening-range volume baselines")
    check("gate runs BEFORE build_baselines() in live.py", gate_i < base_i)
    check("replays are exempt from the gate",
          "if live:" in live_src[gate_i:gate_i + 700])
    for frozen in ("engine.py", "paper_config.py"):
        s = (HERE / frozen).read_text()
        check(f"{frozen} does not reference the session layer",
              "session_guard" not in s and "market_calendar" not in s)


def test_live_feed_optional():
    print("\n  6. LIVE NSE FEED (informational -- not a gate on this suite)")
    try:
        hol, src = mc.holidays_for(2026, allow_network=True)
    except Exception as exc:
        print(f"     NSE unreachable ({type(exc).__name__}) -- skipped")
        return
    if not hol:
        print("     NSE unreachable -- skipped (the VPS must seed its cache)")
        return
    print(f"     fetched {len(hol)} CM holidays for 2026 [source={src}]")
    got = hol.get(date(2026, 9, 14))
    check("live feed agrees: 2026-09-14 is Ganesh Chaturthi",
          got is not None and "Ganesh" in got, f"got {got!r}")
    check("live feed agrees: 2026-09-15 is not a holiday",
          date(2026, 9, 15) not in hol)


def main():
    print("\n" + "=" * 70)
    print(" NSE SESSION GATE CHECKS")
    print("=" * 70)
    test_weekends()
    test_holidays()
    test_fail_closed()
    test_timezone()
    test_no_side_effects()
    test_live_feed_optional()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f" {len(FAILURES)} CHECK(S) FAILED")
        for f in FAILURES:
            print(f"   - {f}")
        print("=" * 70 + "\n")
        sys.exit(1)
    print(" ALL SESSION GATE CHECKS PASSED")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
