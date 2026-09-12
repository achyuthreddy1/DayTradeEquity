"""
The gate between systemd and the trading session.

    systemd / cron
        |
        v
    session_guard.py        <-- weekend + NSE holiday + fail-closed
        |
        v
    live.py                 <-- unchanged trading path
        |
        v
    engine.py / paper_config.py   (FROZEN -- never touched by this layer)

USAGE

    python3 session_guard.py                 # check only; exit code is the answer
    python3 session_guard.py --exec live.py  # check, then run live.py if open
    python3 session_guard.py --date 2026-09-14

EXIT CODES -- the point of them is that systemd can tell "not today" apart
from "something broke", so a holiday never raises a false alert:

    0   open        -- proceed (and --exec has run the child)
    10  weekend     -- clean, expected, not a failure
    11  holiday     -- clean, expected, not a failure
    12  UNKNOWN     -- calendar could not be established. THIS IS A FAILURE.
                       Trading is refused, and it should alert, because the
                       cause (NSE unreachable from the VPS) is exactly the
                       risk the audit flagged and needs a human.
    13  child failed-- the market was open and live.py itself exited non-zero

Codes 10 and 11 belong in systemd's SuccessExitStatus. Code 12 must NOT.

Codes 12 and 13 also fire a best-effort Telegram alert (see _alert() below and
telegram_notifier.py) -- weekend/holiday never do. The alert is observation
only: it cannot change `code`, and a broken/unconfigured notifier degrades to
a silent no-op, never a crash of the gate itself.

WHY A SEPARATE PROCESS RATHER THAN A CHECK INSIDE live.py
---------------------------------------------------------
Both, actually. The guard runs first so a closed day costs zero API calls --
live.py's build_baselines() fetches ~100 symbols x 20 days before it looks at
anything, so gating has to happen before that function is reached. live.py
also calls the same check itself as defence in depth, for the case where
someone runs it directly.

This module contains no strategy logic and imports none. It lazily imports
telegram_notifier for alerting only (see _alert()) -- that import can never
fail the gate itself, and the notifier has no path back into `decide()`.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import market_calendar

EXIT_OPEN = 0
EXIT_WEEKEND = 10
EXIT_HOLIDAY = 11
EXIT_UNKNOWN = 12
EXIT_CHILD_FAILED = 13


def _alert(subject: str, detail: str) -> None:
    """Best-effort Telegram alert. Imported lazily and wrapped in its own
    try/except so a broken notifier can never turn a clean gate decision
    into a crash -- this function's only job is to observe, same guarantee
    telegram_notifier.py documents for itself. Never raises."""
    try:
        import telegram_notifier
        notifier = telegram_notifier.TelegramNotifier.from_env()
        notifier.notify(telegram_notifier.format_alert_message(subject, detail))
    except Exception as exc:
        print(f"  [telegram] alert failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def _alert_unknown_day(st) -> None:
    _alert(f"Session gate: calendar unknown for {st.day}",
           "NSE holiday calendar could not be established -- refusing to trade "
           "rather than assuming the market is open. Seed it with "
           "market_calendar.py --refresh on a machine that can reach NSE.")


def _notify_no_trade(st) -> None:
    """Weekend/holiday are clean, expected outcomes (exit 10/11) -- not
    failures, so this is a plain informational message, not an alert. Same
    best-effort, never-raises contract as _alert(). Exists so a quiet
    market and a bot that never ran don't look identical from the outside."""
    try:
        import telegram_notifier
        notifier = telegram_notifier.TelegramNotifier.from_env()
        notifier.notify(telegram_notifier.format_no_trade_message(st.day, st.reason))
    except Exception as exc:
        print(f"  [telegram] notify failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def _alert_child_failed(cmd: list[str], detail: str) -> None:
    _alert(f"Session gate: child process failed ({' '.join(cmd)})", detail)


def decide(day: date | None = None, allow_network: bool = True):
    """(DayStatus, exit_code). Never raises."""
    try:
        st = market_calendar.status(day, allow_network=allow_network)
    except Exception as exc:
        # Any unexpected failure is an unknown day, and an unknown day is a
        # day we do not trade.
        st = market_calendar.DayStatus(
            day or market_calendar.today_ist(), None,
            f"calendar check raised {type(exc).__name__}: {str(exc)[:80]}", "-")
    if st.open_ is True:
        return st, EXIT_OPEN
    if st.open_ is None:
        return st, EXIT_UNKNOWN
    return st, (EXIT_WEEKEND if "weekend" in st.reason.lower() else EXIT_HOLIDAY)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="NSE session gate: weekend + holiday + fail-closed")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today in IST)")
    ap.add_argument("--exec", dest="exec_cmd", nargs=argparse.REMAINDER,
                    help="command to run when the market is open "
                         "(everything after this flag)")
    ap.add_argument("--offline", action="store_true",
                    help="do not reach NSE; use cache only")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    day = None
    if a.date:
        day = market_calendar._parse_nse_date(a.date)
        if day is None:
            print(f"  bad --date {a.date!r}; expected YYYY-MM-DD", file=sys.stderr)
            return EXIT_UNKNOWN

    st, code = decide(day, allow_network=not a.offline)
    if not a.quiet:
        print("  " + st.line() + (f"  [source={st.source}]" if st.source != "-" else ""))

    if code != EXIT_OPEN:
        if code == EXIT_UNKNOWN:
            if not a.quiet:
                print("  Refusing to start: a day we cannot verify is not traded.\n"
                      "  Seed the calendar on a machine that can reach NSE:\n"
                      "      python3 market_calendar.py --refresh", file=sys.stderr)
            # UNKNOWN means NSE was unreachable from this host, which is the
            # exact risk this gate exists to fail closed on, and it needs a
            # human -- this is an ALERT. Best-effort: never raises, never
            # affects `code`.
            _alert_unknown_day(st)
        elif code in (EXIT_WEEKEND, EXIT_HOLIDAY):
            # Clean, expected outcomes -- a plain "no trades today" notice,
            # not an alert, so a quiet weekend and a dead bot don't look the
            # same on a VPS nobody is watching. Best-effort, never raises.
            _notify_no_trade(st)
        return code

    if not a.exec_cmd:
        return EXIT_OPEN

    cmd = list(a.exec_cmd)
    if cmd and cmd[0].endswith(".py"):
        cmd = [sys.executable] + cmd
    if not a.quiet:
        print(f"  starting: {' '.join(cmd)}")
    try:
        if subprocess.call(cmd, cwd=HERE) == 0:
            return EXIT_OPEN
        # live.py already sends its own Telegram alert for the failure modes
        # it recognises (expired token, data problem); this is the
        # catch-all for anything that killed the child without going
        # through one of those paths -- a human should still hear about it.
        _alert_child_failed(cmd, "exited non-zero")
        return EXIT_CHILD_FAILED
    except Exception as exc:
        print(f"  failed to start {cmd!r}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        _alert_child_failed(cmd, f"{type(exc).__name__}: {exc}")
        return EXIT_CHILD_FAILED


if __name__ == "__main__":
    sys.exit(main())
