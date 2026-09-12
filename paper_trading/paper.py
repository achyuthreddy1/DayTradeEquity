"""
Paper-trading CLI for the midcap ORB-short strategy.

    python3 paper.py verify                    # prove the engine matches research
    python3 paper.py run                       # today (or --date YYYY-MM-DD)
    python3 paper.py backfill --from 2026-09-01 --to 2026-09-10
    python3 paper.py report

WHY A FORWARD TEST AT ALL: every number in the research (archived at
~/trading-research-archive-20260911.tar.gz) comes
from data the strategy was developed on. The walk-forward mitigated that
for two numeric parameters but could not un-choose the universe, the
direction, or the setup itself -- all picked with full-sample knowledge.
Only data that did not exist when the rules were written can settle it.
That is what this journal accumulates.

RUNNING IT: the end-of-day mode below is the backbone -- after the close,
replay the day's 5-min bars and record what the rules would have done. For
a strategy that is fully mechanical on 5-min bars this is equivalent to
live execution for validation purposes, and far more robust (a missed day
can be backfilled; a crashed live process loses the day). What it does NOT
test is your own execution -- see the README.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import journal  # noqa: E402
from paper_config import EXPECTED, STRATEGY, round_trip_cost_pct, today_ist  # noqa: E402
from engine import (Signal as Signal_, build_or_tables, find_signals,  # noqa: E402
                    run_day, simulate_day)
from market_data import liquid_universe, load_parent_env, load_universe  # noqa: E402


def load_data(from_date: datetime, to_date: datetime):
    load_parent_env()
    return load_universe(
        liquid_universe(STRATEGY.index), from_date, to_date,
        fyers_app_id=os.environ.get("FYERS_APP_ID"),
        fyers_access_token=os.environ.get("FYERS_ACCESS_TOKEN"),
    )


def cmd_selftest(args) -> None:
    """Deterministic engine check on synthetic bars -- no market data, no
    network, runs in milliseconds.

    Replaces the old `verify`, which compared against the research code
    over 3.7 years of cached bars. That comparison has already been run
    and PASSED (research n=837 mean +0.3214% vs paper n=832 mean +0.3233%;
    the 5-trade gap was the research code fabricating ~0% trades when a
    breakout landed on the day's final bar -- see README). The research
    tree and its 898MB cache are gone, so this asserts the same invariants
    on fixtures instead: the rules that decide a trade, not a replay.
    """
    from datetime import time as _t
    import pandas as _pd

    def bars(day: str, rows):
        """rows: (HH:MM, o, h, l, c, v)"""
        idx = [_pd.Timestamp(f"{day} {r[0]}") for r in rows]
        return _pd.DataFrame(
            {"open": [r[1] for r in rows], "high": [r[2] for r in rows],
             "low": [r[3] for r in rows], "close": [r[4] for r in rows],
             "volume": [r[5] for r in rows]}, index=idx)

    cfg = STRATEGY
    fails = []

    def check(name, got, want):
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              + ("" if ok else f"   got {got!r}, want {want!r}"))
        if not ok:
            fails.append(name)

    # Opening range 09:15-09:25 -> high 105, low 95. Then a breakdown.
    day = "2026-01-05"
    rows = [("09:15", 100, 105, 98, 102, 1000),
            ("09:20", 102, 104, 95, 100, 1000),
            ("09:25", 100, 103, 99, 101, 1000)]
    # post-OR: bar breaks below 95, next bar opens at 94 -> entry 94
    rows += [("09:30", 101, 102, 94, 96, 500),      # breakdown bar
             ("09:35", 94, 95, 90, 91, 500)]        # entry bar (open 94)
    rows += [(f"{9+(35+5*i)//60:02d}:{(35+5*i)%60:02d}", 91, 92, 90, 91, 100)
             for i in range(1, 70)]
    df = bars(day, rows)
    df = df[[t <= _t(15, 15) for t in df.index.time]]

    from engine import opening_range
    orr = opening_range(df, cfg)
    check("opening range high/low", (orr[0], orr[1]), (105.0, 95.0))

    sig = Signal_("X", _pd.Timestamp(day).date(), 105.0, 95.0, 10.5, 9.9)
    tr = simulate_day(sig, df, cfg, 100_000)
    check("entry is NEXT bar's open (not the signal bar's close)",
          tr.entry_price, 94.0)
    check("short direction recorded", tr.direction, "short")
    check("stop placed 2% ABOVE entry for a short",
          round(tr.stop_price, 2), round(94.0 * 1.02, 2))
    check("exit reason is time (stop never hit)", tr.exit_reason, "time")
    check("profitable short is positive", tr.gross_pct > 0, True)

    # Upside break must NOT trade (long side is dead).
    up = bars(day, [("09:15", 100, 105, 95, 102, 1000),
                    ("09:20", 102, 104, 99, 100, 1000),
                    ("09:25", 100, 103, 99, 101, 1000),
                    ("09:30", 101, 110, 100, 109, 500),
                    ("09:35", 109, 111, 108, 110, 500)]
                   + [("09:40", 110, 111, 109, 110, 100)] * 1)
    up = up[~up.index.duplicated()]
    check("upside break is not traded", simulate_day(sig, up, cfg, 100_000), None)

    # Stop must trigger, and fill no better than the stop level.
    st = bars("2026-01-06",
              [("09:15", 100, 105, 95, 102, 1000), ("09:20", 102, 104, 99, 100, 1000),
               ("09:25", 100, 103, 99, 101, 1000), ("09:30", 101, 102, 94, 96, 500),
               ("09:35", 94, 99, 93, 98, 500)]
              + [(f"{9+(40+5*i)//60:02d}:{(40+5*i)%60:02d}", 98, 99, 97, 98, 100)
                 for i in range(60)])
    st = st[[t <= _t(15, 15) for t in st.index.time]]
    sig2 = Signal_("X", _pd.Timestamp("2026-01-06").date(), 105.0, 95.0, 10.5, 9.9)
    tr2 = simulate_day(sig2, st, cfg, 100_000)
    check("stop triggers when the bar high reaches it", tr2.exit_reason, "stop")
    check("stop fill is no better than the stop level",
          tr2.exit_price >= 94.0 * 1.02, True)

    # Cost model must depend on ticket size (the Rs20-vs-0.03% rule).
    check("Rs1 lakh ticket uses flat Rs20 brokerage",
          round(round_trip_cost_pct(100_000), 3), 0.082)
    check("Rs33k ticket falls under the 0.03% rate",
          round(round_trip_cost_pct(33_333), 3), 0.106)

    # Data-layer invariant. A total load failure MUST raise: returning an
    # empty universe quietly makes an expired token indistinguishable from
    # a market with no signals, which is the worst failure mode for an
    # unattended run -- it reports "nothing to trade today" forever.
    import market_data as _md
    _real = _md.FyersDataSource

    class _Dead:
        def __init__(self, *a, **k): pass
        def load_5min(self, inst, f, t):
            raise _md.DataLoadError("simulated auth failure")

    class _Half(_Dead):
        def load_5min(self, inst, f, t):
            if inst.symbol.endswith(("0", "1", "2", "3", "4")):
                idx = _pd.date_range("2026-01-01 09:15", periods=75, freq="5min")
                return _pd.DataFrame(
                    {c: 100.0 for c in ["open", "high", "low", "close"]}
                    | {"volume": 1000.0}, index=idx)
            raise _md.DataLoadError("simulated per-symbol failure")

    uni = [_md.InstrumentConfig(symbol=f"S{i}", fyers_symbol=f"NSE:S{i}-EQ")
           for i in range(20)]
    try:
        _md.FyersDataSource = _Dead
        raised = False
        try:
            _md.load_universe(uni, _pd.Timestamp("2026-01-01"),
                              _pd.Timestamp("2026-01-02"),
                              fyers_app_id="x", fyers_access_token="y")
        except _md.DataLoadError:
            raised = True
        check("total data failure RAISES (never a quiet empty universe)",
              raised, True)

        _md.FyersDataSource = _Half
        got = _md.load_universe(uni, _pd.Timestamp("2026-01-01"),
                                _pd.Timestamp("2026-01-02"),
                                fyers_app_id="x", fyers_access_token="y")
        check("partial failure still returns data (one bad ticker is survivable)",
              len(got), 10)
    finally:
        _md.FyersDataSource = _real

    # Every module must at least IMPORT. Cheap, but it is the check that was
    # missing when a bad edit to fyers_data_loader's imports left the live
    # data path broken while every check above still passed -- nothing here
    # reaches that module, so "ALL CHECKS PASSED" meant nothing about it.
    import importlib
    for mod in ("paper_config", "engine", "journal", "market_data",
                "fyers_data_loader", "fyers_auth", "fyers_totp",
                "nifty100_universe", "live"):
        try:
            importlib.import_module(mod)
            check(f"module imports: {mod}", True, True)
        except Exception as exc:
            check(f"module imports: {mod}", f"{type(exc).__name__}: {exc}", True)

    # The session clock must be IST regardless of the host's timezone, or a
    # UTC VPS waits for the 09:30 opening range until 15:00 IST.
    from paper_config import now_ist
    from datetime import datetime, timezone
    drift = abs((now_ist() - datetime.now(timezone.utc).replace(tzinfo=None)
                 ).total_seconds() - 5.5 * 3600)
    check("session clock is IST, not the host's timezone", drift < 90, True)

    print(f"\n  {'ALL CHECKS PASSED' if not fails else str(len(fails)) + ' CHECK(S) FAILED'}")
    if fails:
        raise SystemExit(1)


def cmd_run(args) -> None:
    day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else today_ist()
    print(f"\nPAPER RUN -- {day}")
    # Need lookback history for the relative-volume baseline.
    data = load_data(datetime.combine(day, datetime.min.time()) - timedelta(days=90),
                     datetime.combine(day, datetime.min.time()) + timedelta(days=1))
    rng = np.random.default_rng(int(day.strftime("%Y%m%d")))  # reproducible
    trades, signals = run_day(data, day, STRATEGY, rng)

    if not signals:
        print("  no qualifying setups (opening-range volume never exceeded "
              f"{STRATEGY.rel_volume_min}x)\n")
        return
    print(f"  {len(signals)} qualifying signal(s); "
          f"{min(len(signals), STRATEGY.max_concurrent)} slot(s) available")
    for s in signals:
        print(f"     {s.symbol:<12} rel-vol {s.rel_volume:>5.1f}x  "
              f"OR {s.or_low:.2f}-{s.or_high:.2f} ({s.or_width_pct:.2f}%)")
    if not trades:
        print("  no setup broke DOWN through the opening-range low -- no trades.\n")
        return
    print(f"\n  {len(trades)} trade(s):")
    for t in trades:
        print(f"     SHORT {t.symbol:<12} {t.shares:>4} sh @ {t.entry_price:>9.2f} "
              f"({t.entry_time})  ->  {t.exit_price:>9.2f} ({t.exit_time}, {t.exit_reason})"
              f"   {t.net_pct:>+7.3f}%  Rs{t.net_rupees:>+9,.0f}")
    added = journal.append(trades)
    print(f"\n  journalled {added} new trade(s) "
          f"({len(trades)-added} already recorded)\n")


def cmd_backfill(args) -> None:
    d0 = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    d1 = datetime.strptime(args.to_date, "%Y-%m-%d").date()
    print(f"\nBACKFILL {d0} -> {d1}")
    data = load_data(datetime.combine(d0, datetime.min.time()) - timedelta(days=90),
                     datetime.combine(d1, datetime.min.time()) + timedelta(days=1))
    total = 0
    day = d0
    while day <= d1:
        rng = np.random.default_rng(int(day.strftime("%Y%m%d")))
        trades, sigs = run_day(data, day, STRATEGY, rng)
        if trades:
            added = journal.append(trades)
            total += added
            pnl = sum(t.net_rupees for t in trades)
            print(f"  {day}  {len(sigs)} signal(s)  {len(trades)} trade(s)  "
                  f"Rs{pnl:+,.0f}")
        day += timedelta(days=1)
    print(f"\n  {total} trade(s) journalled\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Paper trade the midcap ORB-short strategy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="deterministic engine checks (no data needed)")
    r = sub.add_parser("run", help="run one day and journal the result")
    r.add_argument("--date", help="YYYY-MM-DD (default: today)")
    b = sub.add_parser("backfill", help="run a date range")
    b.add_argument("--from", dest="from_date", required=True)
    b.add_argument("--to", dest="to_date", required=True)
    sub.add_parser("report", help="performance vs. the validated expectation")
    a = ap.parse_args()
    {"selftest": cmd_selftest, "run": cmd_run, "backfill": cmd_backfill,
     "report": lambda _: journal.report()}[a.cmd](a)


if __name__ == "__main__":
    main()
