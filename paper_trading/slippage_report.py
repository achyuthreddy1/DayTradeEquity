"""
Phase 2 report -- is the ~9.6 bps/side execution breakeven achievable?

    python3 slippage_report.py              # everything logged so far
    python3 slippage_report.py --from 2026-09-15 --to 2026-09-30
    python3 slippage_report.py --csv        # machine-readable summary row

Reads execution_log.db and answers the three questions Phase 2 exists for:

  1. TRADEABILITY   what fraction of qualifying signals could actually be
                    shorted -- the denominator nobody has ever measured.
  2. SLIPPAGE       mean / median / p95 entry, exit and round-trip, in bps,
                    against the audit's 19.2 bps round-trip breakeven.
  3. ADJUSTED       the simulated edge minus the slippage actually observed.
     EXPECTANCY     This is the number that decides the strategy.

BENCHMARKS ARE THE AUDIT'S, NOT THE README'S. The paper engine is stricter
than the research (ticket-aware costs, 0.15% stop slippage, whole shares),
so judging it against +0.343% or +0.233% makes correct behaviour look
broken. The reference here is +0.193%/trade fully adjusted.

A note on reading this early: with fewer than ~30 quoted trades the
slippage mean is too noisy to act on. The report says so rather than
printing a confident-looking number.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import execution_log

# From the audit. Do not re-derive these from live data -- they are the
# fixed yardstick the measurement is being held against.
BREAKEVEN_RT_BPS = 19.2          # ~9.6 bps/side
ADJUSTED_EDGE_PCT = 0.193        # fully-adjusted expected net %/trade
MIN_N_FOR_CONFIDENCE = 30
# A quote taken well after the theoretical fill instant measures price drift,
# not slippage. With 60s polling a healthy capture lands inside a minute.
MAX_LAG_S = 90.0


def _pct(v):
    return "n/a" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:+.3f}%"


def _bps(v):
    return "n/a" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:+.1f}"


def _stats(series: pd.Series) -> dict:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return {"n": 0}
    return {"n": len(s), "mean": s.mean(), "median": s.median(),
            "p95": s.quantile(0.95), "min": s.min(), "max": s.max()}


def _line(label: str, st: dict) -> str:
    if not st.get("n"):
        return f"  {label:<26}{'no data yet':>12}"
    return (f"  {label:<26}{st['n']:>6}{_bps(st['mean']):>10}{_bps(st['median']):>10}"
            f"{_bps(st['p95']):>10}{_bps(st['max']):>10}")


def report(df: pd.DataFrame) -> None:
    if df.empty:
        print("\n  No signals logged yet. Run live.py during market hours.\n")
        return

    days = df["day"].nunique()
    print("\n" + "=" * 78)
    print(f"PHASE 2 EXECUTION REPORT   {df['day'].min()} .. {df['day'].max()}"
          f"   ({days} session{'s' if days != 1 else ''})")
    print("=" * 78)

    # ---------------- 1. tradeability ----------------
    n = len(df)
    traded = (df["outcome"] == "traded").sum()
    print(f"\n  SIGNALS                {n} qualifying, over {days} session(s)"
          f"  ({n/days:.2f}/day)")
    print("\n  OUTCOME BREAKDOWN")
    for outcome, cnt in df["outcome"].value_counts().items():
        print(f"     {str(outcome):<28}{cnt:>5}  {100*cnt/n:>5.1f}%")

    known = df[df["tradeable"].notna()]
    blocked = (known["tradeable"] == 0).sum()
    unknown = df["tradeable"].isna().sum()
    print("\n  TRADEABILITY")
    if len(known):
        print(f"     checked                  {len(known):>5}  "
              f"({100*len(known)/n:.0f}% of signals)")
        print(f"     tradeable                {len(known)-blocked:>5}  "
              f"{100*(len(known)-blocked)/len(known):>5.1f}%   <-- the denominator")
        print(f"     blocked                  {blocked:>5}  "
              f"{100*blocked/len(known):>5.1f}%")
    else:
        print("     none checked -- NSE lists unreachable, or every session was a")
        print("     replay (historical ASM/GSM/ban status is not published)")
    if unknown:
        print(f"     UNKNOWN (not counted)    {unknown:>5}  "
              f"status could not be determined")
    reasons = df.loc[df["reject_reason"].astype(str).str.len() > 0, "reject_reason"]
    if len(reasons):
        print("     reasons seen:")
        for r, c in reasons.value_counts().items():
            print(f"        {c:>3}x  {r}")

    # ---------------- 2. slippage ----------------
    print("\n  SLIPPAGE (bps, positive = WORSE than the backtest assumed)")
    print(f"  {'':<26}{'n':>6}{'mean':>10}{'median':>10}{'p95':>10}{'worst':>10}")
    # Stale captures are excluded from the slippage statistics: comparing a
    # quote photographed minutes late against the theoretical fill measures
    # how far price moved, which is not what the breakeven is about.
    lag = pd.to_numeric(df.get("detect_lag_s"), errors="coerce")
    fresh = df[(lag.isna()) | (lag.abs() <= MAX_LAG_S)]
    stale_n = int((lag.abs() > MAX_LAG_S).sum())
    e = _stats(fresh["entry_slip_bps"])
    x = _stats(fresh["exit_slip_bps"])
    rt = pd.to_numeric(fresh["entry_slip_bps"], errors="coerce").fillna(0) + \
        pd.to_numeric(fresh["exit_slip_bps"], errors="coerce").fillna(0)
    rt = rt[pd.to_numeric(fresh["entry_slip_bps"], errors="coerce").notna()]
    r = _stats(rt)
    print(_line("entry (sell into bid)", e))
    print(_line("exit (lift the ask)", x))
    print(_line("ROUND TRIP", r))
    sp = _stats(fresh["decision_spread_bps"])
    tk = _stats(fresh["tick_bps"])
    lg = _stats(df["detect_lag_s"])
    print(_line("quoted spread", sp))
    print(_line("one tick", tk))
    if lg.get("n"):
        print(f"  {'capture lag (seconds)':<26}{lg['n']:>6}{lg['mean']:>10.1f}"
              f"{lg['median']:>10.1f}{lg['p95']:>10.1f}{lg['max']:>10.1f}")
    if stale_n:
        print(f"  ** {stale_n} capture(s) later than {MAX_LAG_S:.0f}s EXCLUDED "
              f"-- those measure drift, not slippage")

    # ---------------- 3. verdict vs breakeven ----------------
    print(f"\n  vs AUDIT BREAKEVEN ({BREAKEVEN_RT_BPS:.1f} bps round trip / "
          f"{BREAKEVEN_RT_BPS/2:.1f} per side)")
    if not r.get("n"):
        print("     no quoted fills yet -- this is the measurement Phase 2 exists for")
    else:
        m = r["mean"]
        margin = BREAKEVEN_RT_BPS - m
        print(f"     observed mean round trip {m:>+8.1f} bps")
        print(f"     margin vs breakeven      {margin:>+8.1f} bps"
              f"   {'SURVIVES' if margin > 0 else 'EDGE IS GONE'}")
        print(f"     share of trades over breakeven: "
              f"{100*(rt > BREAKEVEN_RT_BPS).mean():.0f}%")
        if r["n"] < MIN_N_FOR_CONFIDENCE:
            print(f"     ** only {r['n']} quoted fill(s). Below ~{MIN_N_FOR_CONFIDENCE} "
                  f"this mean is noise -- do not act on it yet.")

    # ---------------- 4. adjusted expectancy ----------------
    print("\n  EXPECTANCY")
    t = fresh[fresh["outcome"] == "traded"]
    net = pd.to_numeric(t["net_pct"], errors="coerce").dropna()
    if net.empty:
        print("     no completed paper trades yet")
    else:
        print(f"     simulated net (journal)  {_pct(net.mean()):>10}  n={len(net)}")
        slip_pct = rt.reindex(t.index).fillna(np.nan) / 100.0   # bps -> %
        adj = net - slip_pct.reindex(net.index).fillna(0)
        measured = slip_pct.notna().sum()
        print(f"     measured slippage drag   "
              f"{_pct(-slip_pct.mean() if measured else None):>10}  "
              f"({measured} of {len(net)} trades quoted)")
        if measured:
            print(f"     ADJUSTED EXPECTANCY      {_pct(adj.mean()):>10}"
                  f"   <-- the real number")
        else:
            # Printing the simulated figure under an "adjusted" label would be
            # a lie of exactly the kind this report exists to catch.
            print(f"     ADJUSTED EXPECTANCY      {'n/a':>10}"
                  f"   nothing quoted yet -- NOT the same as +0")
        print(f"     audit reference          {ADJUSTED_EDGE_PCT:>+9.3f}%"
              f"   fully-adjusted expectation")
        if measured >= MIN_N_FOR_CONFIDENCE:
            verdict = ("AHEAD of" if adj.mean() > ADJUSTED_EDGE_PCT
                       else "BEHIND" if adj.mean() > 0 else "NEGATIVE vs")
            print(f"     -> running {verdict} the audit reference")
        else:
            print(f"     ** {measured} quoted trade(s); need ~{MIN_N_FOR_CONFIDENCE} "
                  f"before this is meaningful")

    # ---------------- 5. kill criterion ----------------
    print("\n  PRE-REGISTERED KILL CRITERION")
    print(f"     stop if mean entry slippage > 10.0 bps/side over 40 trades")
    if e.get("n"):
        status = ("BREACHED" if e["mean"] > 10.0 and e["n"] >= 40
                  else "watching" if e["n"] < 40 else "clear")
        print(f"     current: {e['mean']:+.1f} bps/side over {e['n']} trade(s)"
              f"   [{status}]")
    else:
        print("     current: no quoted entries yet")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 2 execution / slippage report")
    ap.add_argument("--from", dest="from_date", help="YYYY-MM-DD")
    ap.add_argument("--to", dest="to_date", help="YYYY-MM-DD")
    ap.add_argument("--csv", action="store_true", help="one summary row, machine-readable")
    ap.add_argument("--include-replay", action="store_true",
                    help="include replayed sessions (they have no order book)")
    a = ap.parse_args()

    df = execution_log.load()
    if not df.empty and "mode" in df.columns and not a.include_replay:
        dropped = int((df["mode"] != "live").sum())
        df = df[df["mode"] == "live"]
        if dropped:
            print(f"\n  ({dropped} replay row(s) excluded -- a replay has no order "
                  f"book. Use --include-replay to see them.)")
    if a.from_date:
        df = df[df["day"] >= a.from_date]
    if a.to_date:
        df = df[df["day"] <= a.to_date]

    if a.csv:
        e = _stats(df["entry_slip_bps"]) if not df.empty else {"n": 0}
        known = df[df["tradeable"].notna()] if not df.empty else df
        print("signals,days,traded,tradeable_pct,entry_slip_mean_bps,entry_slip_median_bps,n_quoted")
        print(f"{len(df)},{df['day'].nunique() if not df.empty else 0},"
              f"{(df['outcome']=='traded').sum() if not df.empty else 0},"
              f"{100*(known['tradeable']==1).mean() if len(known) else ''},"
              f"{e.get('mean','')},{e.get('median','')},{e.get('n',0)}")
        return
    report(df)


if __name__ == "__main__":
    main()
