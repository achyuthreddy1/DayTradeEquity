"""
Does the instrument measure correctly?

test_strategy_frozen.py proves V1 did not change. This proves the thing
measuring it works -- separate claims, separate files.

The slippage arithmetic cannot be checked against a live market until the
next session, so it is checked here against hand-computed values with
injected quotes. Getting the SIGN wrong would be the worst possible bug:
it would turn a cost into a credit and make a dying strategy look healthy.
So sign conventions are asserted explicitly and in both directions.

    python3 test_instrumentation.py
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import execution_log
import instrumentation
import quote_probe
from engine import Signal
from paper_config import STRATEGY as S
from execution_log import SignalRecord
from quote_probe import Quote

FAILURES: list[str] = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   {detail}"))
    if not ok:
        FAILURES.append(name)


def test_slippage_signs():
    print("\n  1. SLIPPAGE SIGNS -- positive must always mean WORSE")
    # A short SELLS into the bid. Bid below the assumed fill = worse.
    check("entry: bid BELOW theoretical -> positive (worse)",
          quote_probe.entry_slip_bps(100.0, 99.9) > 0)
    check("entry: bid ABOVE theoretical -> negative (better)",
          quote_probe.entry_slip_bps(100.0, 100.1) < 0)
    check("entry: 99.90 vs 100.00 is exactly 10 bps",
          abs(quote_probe.entry_slip_bps(100.0, 99.9) - 10.0) < 1e-9,
          f"got {quote_probe.entry_slip_bps(100.0, 99.9)}")
    # Covering a short BUYS the ask. Ask above the assumed exit = worse.
    check("exit: ask ABOVE theoretical -> positive (worse)",
          quote_probe.exit_slip_bps(100.0, 100.1) > 0)
    check("exit: ask BELOW theoretical -> negative (better)",
          quote_probe.exit_slip_bps(100.0, 99.9) < 0)
    check("exit: 100.10 vs 100.00 is exactly 10 bps",
          abs(quote_probe.exit_slip_bps(100.0, 100.1) - 10.0) < 1e-9)
    check("missing/zero inputs return None, never 0.0",
          quote_probe.entry_slip_bps(None, 99.9) is None
          and quote_probe.entry_slip_bps(100.0, 0) is None
          and quote_probe.exit_slip_bps(100.0, None) is None)


def test_quote_derived():
    print("\n  2. QUOTE ARITHMETIC")
    q = Quote(bid=99.9, ask=100.1, ltp=100.0, tick_size=0.05, ok=True)
    check("spread bps = 20 on a 0.20 spread at 100",
          abs(q.spread_bps - 20.0) < 1e-6, f"got {q.spread_bps}")
    check("tick bps = 5 for a 0.05 tick at 100",
          abs(q.tick_bps - 5.0) < 1e-6, f"got {q.tick_bps}")
    penny = Quote(bid=6.90, ask=6.95, ltp=6.90, tick_size=0.01, ok=True)
    check("penny stock tick is ~14.5 bps (the audit's concern)",
          13.0 < penny.tick_bps < 16.0, f"got {penny.tick_bps}")
    empty = Quote()
    check("an empty quote yields None, not a fake zero",
          empty.spread_bps is None and empty.tick_bps is None)
    lc = Quote(bid=90.0, ask=90.05, ltp=90.0, tick_size=0.05,
               lower_ckt=90.0, ok=True)
    check("lower-circuit lock is detected", lc.at_lower_circuit)
    check("a normal quote is not flagged as circuit-locked",
          not Quote(bid=99.9, ask=100.1, ltp=100.0, tick_size=0.05,
                    lower_ckt=80.0, ok=True).at_lower_circuit)


def _bars():
    rows = [("09:15", 100, 105, 98, 102, 1000), ("09:20", 102, 104, 95, 99, 1000),
            ("09:25", 99, 103, 97, 100, 1000), ("09:30", 100, 100, 94, 95, 5000),
            ("09:35", 94, 95.5, 93, 94, 5000)]
    rows += [(t, 94, 95, 92, 93, 1000) for t in
             ["09:40", "09:45", "09:50", "09:55", "10:00", "10:05", "10:10",
              "10:15", "10:20"]]
    idx = [pd.Timestamp(f"2026-01-05 {r[0]}") for r in rows]
    return pd.DataFrame({"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                         "low": [r[3] for r in rows], "close": [r[4] for r in rows],
                         "volume": [r[5] for r in rows]}, index=idx)


def test_record_assembly():
    print("\n  3. RECORD ASSEMBLY -- a quote flows into a measured record")
    sig = Signal("TEST", date(2026, 1, 5), 105.0, 95.0, 10.53, 6.0)
    q = Quote(bid=93.85, ask=93.95, ltp=93.90, tick_size=0.05,
              bid_qty=500, ask_qty=400, lower_ckt=85.0, upper_ckt=105.0, ok=True)
    recs = instrumentation.build_records(
        date(2026, 1, 5), [sig], [sig], {"TEST": _bars()}, [],
        {"TEST": "NSE:TEST-EQ"},
        quotes={"TEST": (q, "2026-01-05T09:35:10", 10.0)})
    r = recs[0]
    check("theoretical entry is the next bar's open (94.0)", r.theo_entry == 94.0,
          f"got {r.theo_entry}")
    check("achievable entry is the bid", r.achievable_entry == 93.85)
    # (94.00 - 93.85) / 94.00 * 10000 = 15.96 bps
    check("entry slippage computed from bid vs theoretical",
          abs(r.entry_slip_bps - 15.96) < 0.05, f"got {r.entry_slip_bps}")
    check("spread and tick recorded", r.decision_spread_bps is not None
          and r.tick_bps is not None)
    check("detection lag recorded", r.detect_lag_s == 10.0)
    check("outcome classified as traded", r.outcome == "traded")

    # a signal with no slot must be logged, and NOT as a trade
    recs2 = instrumentation.build_records(
        date(2026, 1, 5), [sig], [], {"TEST": _bars()}, [], {"TEST": "NSE:TEST-EQ"})
    check("unselected signal is still logged", len(recs2) == 1)
    check("unselected signal is marked not_selected",
          recs2[0].outcome == "not_selected", f"got {recs2[0].outcome}")
    check("unselected signal carries no slippage", recs2[0].entry_slip_bps is None)


def test_storage_roundtrip():
    print("\n  4. STORAGE -- append-only, de-duped, CSV mirrors SQLite")
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "t.db"
        cd = Path(td) / "csv"
        a = SignalRecord(day="2026-01-05", symbol="AAA", outcome="traded",
                         entry_slip_bps=12.0)
        b = SignalRecord(day="2026-01-05", symbol="BBB", outcome="no_trigger")
        execution_log.write([a, b], db, cd)
        df = execution_log.load(db)
        check("both rows stored", len(df) == 2, f"got {len(df)}")

        a2 = SignalRecord(day="2026-01-05", symbol="AAA", outcome="traded",
                          entry_slip_bps=15.0)
        execution_log.write([a2], db, cd)
        df = execution_log.load(db)
        check("re-running a day does not duplicate", len(df) == 2, f"got {len(df)}")
        got = df[df.symbol == "AAA"]["entry_slip_bps"].iloc[0]
        check("a re-run UPDATES rather than keeping the stale row", got == 15.0,
              f"got {got}")

        mirror = cd / "2026-01-05.csv"
        check("CSV mirror written", mirror.exists())
        csv_df = pd.read_csv(mirror)
        check("CSV row count matches SQLite", len(csv_df) == 2)
        check("CSV carries the full schema",
              list(csv_df.columns) == execution_log.COLUMNS)


def test_never_raises():
    print("\n  5. FAIL-SAFE -- instrumentation must never break a session")
    check("a bad symbol returns a Quote, not an exception",
          isinstance(quote_probe.fetch("NSE:NOT_A_REAL_SYMBOL_XYZ-EQ"), Quote))
    bad = quote_probe.fetch("")
    check("an empty symbol reports ok=False with a reason",
          bad.ok is False and bad.error != "")
    sig = Signal("TEST", date(2026, 1, 5), 105.0, 95.0, 10.53, 6.0)
    out = instrumentation.record_day(date(2026, 1, 5), [sig], [sig],
                                     {"TEST": "not a dataframe"}, [],
                                     {"TEST": "NSE:TEST-EQ"})
    check("record_day swallows a malformed frame and returns a list",
          isinstance(out, list))


def test_live_detection_timing():
    """REGRESSION: the probe once fired 40 minutes late.

    classify_outcome carries simulate_day's `len(post) < 10` guard. Using it
    for the live probe suppressed detection until ~10:15, so the captured
    book measured price drift rather than execution slippage. These assert
    the live detector has no such guard.
    """
    print("\n  6. LIVE DETECTION TIMING -- the probe must fire on bar 1")
    sig = Signal("T", date(2026, 1, 5), 105.0, 95.0, 10.5, 6.0)
    full = _bars()
    one_post_bar = full.iloc[:4]          # OR bars + the breakout bar only
    state, bar_ts, theo = instrumentation.detect_breakdown_live(sig, one_post_bar, S)
    check("fires with ONE post-OR bar present", state == "broken",
          f"got {state} -- the 10-bar guard is back")
    check("no theoretical entry yet (next bar has not started)", theo is None)

    old_out, _, _ = instrumentation.classify_outcome(sig, one_post_bar, S)
    check("classify_outcome still suppresses it (so it must not be used live)",
          old_out == "insufficient_bars", f"got {old_out}")

    state2, _, theo2 = instrumentation.detect_breakdown_live(sig, full.iloc[:5], S)
    check("becomes 'entered' once the entry bar exists", state2 == "entered")
    check("theoretical entry is that bar's open", theo2 == 94.0, f"got {theo2}")

    inst = instrumentation.theoretical_entry_instant("2026-01-05 09:30:00")
    check("fill instant is the breakout bar's CLOSE, not its label",
          inst.strftime("%H:%M") == "09:35", f"got {inst}")

    up = full.copy()
    up.iloc[3, up.columns.get_loc("high")] = 107.0
    up.iloc[3, up.columns.get_loc("low")] = 99.0
    st3, _, _ = instrumentation.detect_breakdown_live(sig, up.iloc[:4], S)
    check("an upside break is not probed as a short", st3 == "upside", f"got {st3}")

    flat = full.iloc[:3]
    st4, _, _ = instrumentation.detect_breakdown_live(sig, flat, S)
    check("no post-OR bars yet -> pending", st4 == "pending", f"got {st4}")


def test_mode_separation():
    print("\n  7. REPLAY MUST NOT CONTAMINATE LIVE MEASUREMENT")
    sig = Signal("T", date(2026, 1, 5), 105.0, 95.0, 10.5, 6.0)
    live_recs = instrumentation.build_records(
        date(2026, 1, 5), [sig], [sig], {"T": _bars()}, [], {"T": "NSE:T-EQ"},
        mode="live")
    rep_recs = instrumentation.build_records(
        date(2026, 1, 5), [sig], [sig], {"T": _bars()}, [], {"T": "NSE:T-EQ"},
        mode="replay")
    check("records carry their mode", live_recs[0].mode == "live"
          and rep_recs[0].mode == "replay")
    check("mode is a stored column", "mode" in execution_log.COLUMNS)

    with tempfile.TemporaryDirectory() as td:
        db, cd = Path(td) / "m.db", Path(td) / "c"
        execution_log.write(
            [SignalRecord(day="2026-01-05", symbol="L", mode="live",
                          outcome="traded", entry_slip_bps=8.0),
             SignalRecord(day="2026-01-05", symbol="R", mode="replay",
                          outcome="traded", entry_slip_bps=999.0)], db, cd)
        df = execution_log.load(db)
        live_only = df[df["mode"] == "live"]
        check("replay row is separable", len(live_only) == 1 and len(df) == 2)
        check("live slippage is not polluted by the replay",
              live_only["entry_slip_bps"].mean() == 8.0)


def test_schema_migration():
    print("\n  8. SCHEMA MIGRATION -- an older database must still open")
    import sqlite3
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "old.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE signals (day TEXT, symbol TEXT, "
                    "PRIMARY KEY (day, symbol))")
        con.execute("INSERT INTO signals VALUES ('2026-01-01','OLD')")
        con.commit(); con.close()
        execution_log.write([SignalRecord(day="2026-01-02", symbol="NEW",
                                          mode="live")], db, Path(td) / "c")
        df = execution_log.load(db)
        check("pre-existing row survives migration", len(df) == 2, f"got {len(df)}")
        check("new columns were added", "entry_slip_bps" in df.columns)


def main():
    print("\n" + "=" * 70)
    print(" PHASE 2 INSTRUMENTATION CHECKS")
    print("=" * 70)
    test_slippage_signs()
    test_quote_derived()
    test_record_assembly()
    test_storage_roundtrip()
    test_never_raises()
    test_live_detection_timing()
    test_mode_separation()
    test_schema_migration()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f" {len(FAILURES)} CHECK(S) FAILED")
        for f in FAILURES:
            print(f"   - {f}")
        print("=" * 70 + "\n")
        sys.exit(1)
    print(" ALL INSTRUMENTATION CHECKS PASSED")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
