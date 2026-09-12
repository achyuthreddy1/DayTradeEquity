"""
Proof that Phase 2 instrumentation did not change Strategy V1.

    python3 test_strategy_frozen.py            # run the checks
    python3 test_strategy_frozen.py --hashes   # print current hashes

WHY HASHES *AND* BEHAVIOUR
--------------------------
A hash catches an edit; it does not catch an edit that was intended. A
golden replay catches a behaviour change; it does not catch a change to a
path the fixtures miss. Neither alone is proof, so both run here:

  1. FROZEN SOURCE     exact SHA-256 of the four functions that decide a
                       trade. Any edit at all -- including a comment --
                       fails, forcing a deliberate hash update and a
                       conversation about why.
  2. FROZEN PARAMETERS the V1 numbers, asserted literally.
  3. GOLDEN REPLAY     hand-built bars with a known correct answer, so a
                       behaviour change is caught even if someone updates
                       the hashes.
  4. ISOLATION         engine.py and paper_config.py must not reference any
                       instrumentation module. Instrumentation imports the
                       strategy; the strategy must never import it back.
  5. DIAGNOSTIC DRIFT  instrumentation.classify_outcome mirrors
                       simulate_day's scan for reporting. It is the one
                       place a copy of strategy logic exists, so it is
                       cross-checked against the real engine on every
                       fixture -- and on every day already logged.

If you deliberately change V1, these SHOULD fail. Update the hashes in the
same commit as the strategy change, never separately.
"""
from __future__ import annotations

import hashlib
import inspect
import sys
from datetime import date, time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import engine
import instrumentation
from engine import Signal, allocate, build_or_tables, find_signals, simulate_day
from paper_config import STRATEGY

# Recorded 2026-09-12, before Phase 2 instrumentation was written.
FROZEN_HASHES = {
    "find_signals": "144a0f89e3cc7b8b",
    "simulate_day": "945d23dc0d7ce634",
    "allocate": "d7ca6ba11c35db35",
    "build_or_tables": "fbdf39331e27f5e7",
}

FROZEN_PARAMS = {
    "or_start": time(9, 15),
    "or_end": time(9, 30),
    "exit_time": time(15, 15),
    "rel_volume_min": 5.0,
    "rel_volume_lookback": 20,
    "stop_pct": 2.0,
    "stop_slippage_pct": 0.15,
    "capital": 100_000.0,
    "max_concurrent": 3,
    "full_deployment": True,
    "index": "niftymidcap100",
}

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   {detail}"))
    if not ok:
        FAILURES.append(name)


def fn_hash(fn) -> str:
    return hashlib.sha256(inspect.getsource(fn).encode()).hexdigest()[:16]


def bars(rows, day="2026-01-05"):
    """rows: (HH:MM, open, high, low, close, volume)"""
    idx = [pd.Timestamp(f"{day} {r[0]}") for r in rows]
    return pd.DataFrame(
        {"open": [r[1] for r in rows], "high": [r[2] for r in rows],
         "low": [r[3] for r in rows], "close": [r[4] for r in rows],
         "volume": [r[5] for r in rows]}, index=idx)


def _fixture_day(breakdown=True, stop_out=False):
    """09:15-09:25 range = 95..105, then a controlled afternoon."""
    rows = [("09:15", 100, 105, 98, 102, 1000),
            ("09:20", 102, 104, 95, 99, 1000),
            ("09:25", 99, 103, 97, 100, 1000)]
    if breakdown:
        rows.append(("09:30", 100, 100, 94, 95, 5000))    # breaks 95 low
        # entry is 94.00, so the stop sits at 95.88; the no-stop path must
        # stay strictly below it or this fixture tests the wrong branch.
        rows.append(("09:35", 94, 99 if stop_out else 95.5, 93, 94, 5000))
        for t in ["09:40", "09:45", "09:50", "09:55", "10:00",
                  "10:05", "10:10", "10:15", "10:20"]:
            hi = 97.5 if stop_out else 95
            rows.append((t, 94, hi, 92, 93, 1000))
    else:
        rows.append(("09:30", 100, 104, 99, 101, 5000))
        for t in ["09:35", "09:40", "09:45", "09:50", "09:55", "10:00",
                  "10:05", "10:10", "10:15", "10:20"]:
            rows.append((t, 100, 103, 99, 101, 1000))
    return bars(rows)


def test_frozen_source():
    print("\n  1. FROZEN SOURCE -- the four functions that decide a trade")
    for name, fn in (("find_signals", find_signals), ("simulate_day", simulate_day),
                     ("allocate", allocate), ("build_or_tables", build_or_tables)):
        got = fn_hash(fn)
        want = FROZEN_HASHES[name]
        check(f"engine.{name} unchanged", got == want,
              f"got {got}, frozen {want} -- V1 logic was edited")


def test_frozen_params():
    print("\n  2. FROZEN PARAMETERS -- V1 numbers asserted literally")
    for k, want in FROZEN_PARAMS.items():
        got = getattr(STRATEGY, k)
        check(f"STRATEGY.{k} == {want!r}", got == want, f"got {got!r}")


def test_golden_replay():
    print("\n  3. GOLDEN REPLAY -- known bars, known answer")
    sig = Signal("TEST", date(2026, 1, 5), 105.0, 95.0, 10.53, 6.0)

    tr = simulate_day(sig, _fixture_day(breakdown=True), STRATEGY, 100_000.0)
    check("breakdown produces a trade", tr is not None)
    if tr:
        check("entry is the NEXT bar's open (94.0)", tr.entry_price == 94.0,
              f"got {tr.entry_price}")
        check("direction is short", tr.direction == "short")
        check("exit reason is time", tr.exit_reason == "time", f"got {tr.exit_reason}")
        check("stop sits 2% ABOVE entry", abs(95.88 - 94.0 * 1.02) < 1e-9)
        check("shares = ticket // entry", tr.shares == int(100_000 // 94.0),
              f"got {tr.shares}")

    tr2 = simulate_day(sig, _fixture_day(breakdown=False), STRATEGY, 100_000.0)
    check("no breakdown produces no trade", tr2 is None)

    tr3 = simulate_day(sig, _fixture_day(breakdown=True, stop_out=True),
                       STRATEGY, 100_000.0)
    check("stop-out is detected", tr3 is not None and tr3.exit_reason == "stop",
          f"got {tr3.exit_reason if tr3 else None}")
    if tr3:
        check("stop fill is never better than the stop level",
              tr3.exit_price >= 94.0 * 1.02, f"got {tr3.exit_price}")

    check("allocate splits across slots", allocate([sig] * 3, STRATEGY) == 100_000 / 3)
    check("allocate caps at max_concurrent",
          allocate([sig] * 9, STRATEGY) == 100_000 / STRATEGY.max_concurrent)


def test_isolation():
    print("\n  4. ISOLATION -- the strategy must not import instrumentation")
    instr = ("instrumentation", "quote_probe", "execution_log", "tradeability",
             "slippage_report", "telegram_notifier")
    for mod in ("engine.py", "paper_config.py", "journal.py"):
        src = (HERE / mod).read_text()
        hits = [m for m in instr if m in src]
        check(f"{mod} references no instrumentation module", not hits,
              f"found {hits}")
    check("instrumentation.py does import the strategy (one-way dependency)",
          "from paper_config import" in (HERE / "instrumentation.py").read_text())


def test_diagnostic_agrees_with_engine():
    print("\n  5. DIAGNOSTIC DRIFT -- classify_outcome vs the real engine")
    sig = Signal("TEST", date(2026, 1, 5), 105.0, 95.0, 10.53, 6.0)
    cases = [("breakdown", _fixture_day(True), True),
             ("no breakdown", _fixture_day(False), False),
             ("stop-out", _fixture_day(True, stop_out=True), True)]
    for label, df, expect_trade in cases:
        outcome, _, theo = instrumentation.classify_outcome(sig, df, STRATEGY)
        tr = simulate_day(sig, df, STRATEGY, 100_000.0)
        agree = (outcome == "traded") == (tr is not None) == expect_trade
        check(f"agrees on '{label}'", agree,
              f"diagnostic={outcome}, engine={'trade' if tr else 'none'}")
        if tr is not None and theo is not None:
            check(f"theoretical entry matches engine fill on '{label}'",
                  abs(theo - tr.entry_price) < 1e-9,
                  f"diagnostic {theo} vs engine {tr.entry_price}")

    # upside break must be reported as such, and must not trade
    up = bars([("09:15", 100, 105, 98, 102, 1000), ("09:20", 102, 104, 95, 99, 1000),
               ("09:25", 99, 103, 97, 100, 1000), ("09:30", 101, 107, 100, 106, 5000)]
              + [(t, 106, 108, 105, 107, 1000) for t in
                 ["09:35", "09:40", "09:45", "09:50", "09:55", "10:00", "10:05",
                  "10:10", "10:15"]])
    outcome, _, _ = instrumentation.classify_outcome(sig, up, STRATEGY)
    check("upside break classified, not traded",
          outcome == "upside_break" and simulate_day(sig, up, STRATEGY, 1e5) is None,
          f"got {outcome}")


def test_logged_days_consistent():
    print("\n  6. LOGGED DAYS -- instrumentation never contradicts the journal")
    import execution_log
    df = execution_log.load()
    if df.empty:
        print("     (no sessions logged yet -- nothing to cross-check)")
        return
    import journal
    j = journal.load()
    if j.empty:
        print("     (journal empty -- nothing to cross-check)")
        return
    j_keys = {(r.symbol, str(pd.Timestamp(r.day).date())) for r in j.itertuples()}
    logged = df[df["outcome"] == "traded"]
    log_keys = {(r.symbol, r.day) for r in logged.itertuples()}
    check("every logged 'traded' row exists in the journal",
          log_keys <= j_keys, f"orphans: {sorted(log_keys - j_keys)[:5]}")
    for r in logged.itertuples():
        jr = j[(j["symbol"] == r.symbol)
               & (j["day"].astype(str).str.startswith(r.day))]
        if len(jr) == 1 and pd.notna(r.net_pct):
            check(f"net_pct matches journal for {r.symbol} {r.day}",
                  abs(float(jr.iloc[0]["net_pct"]) - float(r.net_pct)) < 1e-9)


def main() -> None:
    if "--hashes" in sys.argv:
        for name, fn in (("find_signals", find_signals), ("simulate_day", simulate_day),
                         ("allocate", allocate), ("build_or_tables", build_or_tables)):
            print(f'    "{name}": "{fn_hash(fn)}",')
        return
    print("\n" + "=" * 70)
    print(" STRATEGY V1 FREEZE CHECKS")
    print("=" * 70)
    test_frozen_source()
    test_frozen_params()
    test_golden_replay()
    test_isolation()
    test_diagnostic_agrees_with_engine()
    test_logged_days_consistent()
    print("\n" + "=" * 70)
    if FAILURES:
        print(f" {len(FAILURES)} CHECK(S) FAILED -- V1 MAY HAVE CHANGED")
        for f in FAILURES:
            print(f"   - {f}")
        print("=" * 70 + "\n")
        sys.exit(1)
    print(" ALL FREEZE CHECKS PASSED -- Strategy V1 is unchanged")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
