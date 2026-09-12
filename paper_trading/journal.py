"""
Append-only trade journal and performance report.

The report's job is not to show a number -- it is to tell you whether the
strategy is BEHAVING AS VALIDATED or genuinely breaking. That distinction
is the hardest part of running a forward test by hand: 35% of months are
expected to lose money, so a losing month is normal and says nothing. The
common failure is abandoning a working strategy after a bad month, or
concluding a broken one still works because one month was good.

So every figure is reported against the band the research predicts, and
the verdict is explicitly "within expectation" until the evidence is
strong enough to say otherwise.
"""
from __future__ import annotations

import math
from dataclasses import asdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from paper_config import EXPECTED, STRATEGY

JOURNAL = Path(__file__).resolve().parent / "journal.csv"
SIGNALS_LOG = Path(__file__).resolve().parent / "signals.csv"


def append(trades, path: Path = JOURNAL) -> int:
    """Append trades, skipping any (symbol, day) already recorded so a
    re-run of the same day cannot double-count."""
    if not trades:
        return 0
    new = pd.DataFrame([asdict(t) for t in trades])
    new["day"] = pd.to_datetime(new["day"]).dt.date
    if path.exists():
        old = pd.read_csv(path)
        old["day"] = pd.to_datetime(old["day"]).dt.date
        seen = set(zip(old["symbol"], old["day"]))
        new = new[[(s, d) not in seen for s, d in zip(new["symbol"], new["day"])]]
        if new.empty:
            return 0
        out = pd.concat([old, new], ignore_index=True)
    else:
        out = new
    out.sort_values(["day", "symbol"]).to_csv(path, index=False)
    return len(new)


def load(path: Path = JOURNAL) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    d = pd.read_csv(path)
    d["day"] = pd.to_datetime(d["day"])
    return d


def _t(v: np.ndarray):
    n = len(v)
    if n < 2:
        return np.nan
    sd = v.std(ddof=1)
    return v.mean() / (sd / math.sqrt(n)) if sd > 0 else np.nan


def report(path: Path = JOURNAL) -> None:
    d = load(path)
    if d.empty:
        print("\nNo trades journalled yet. Run:  python3 paper.py run --date YYYY-MM-DD\n")
        return

    cap = STRATEGY.capital
    # Daily P&L in rupees, then compounded into months.
    daily = d.groupby("day")["net_rupees"].sum()
    equity = cap + daily.cumsum()
    monthly = daily.groupby(daily.index.to_period("M")).sum()
    months = len(monthly)
    n = len(d)

    print("\n" + "=" * 78)
    print(f"PAPER TRADING REPORT  --  {d['day'].min().date()} to {d['day'].max().date()}")
    print("=" * 78)
    print(f"  trades            {n}  over {d['day'].nunique()} trading days")
    print(f"  capital           Rs{cap:,.0f}  ->  Rs{equity.iloc[-1]:,.0f}  "
          f"({100*(equity.iloc[-1]/cap-1):+.2f}%)")
    print(f"  total P&L         Rs{daily.sum():+,.0f}")

    net = d["net_pct"].to_numpy()
    print(f"\n  PER TRADE          live          expected        verdict")
    def line(label, live, exp, fmt="{:+.3f}%", tol=None):
        ok = "within expectation"
        if tol is not None and np.isfinite(live):
            ok = "within expectation" if abs(live - exp) <= tol else "OUTSIDE band"
        print(f"  {label:<17} {fmt.format(live):>12}  {fmt.format(exp):>14}   {ok}")
    se = d["net_pct"].std(ddof=1) / math.sqrt(n) if n > 1 else np.nan
    line("net %/trade", net.mean(), EXPECTED.per_trade_net_pct,
         tol=2 * se if np.isfinite(se) else None)
    line("win rate", 100 * (net > 0).mean(), 100 * EXPECTED.win_rate, "{:.1f}%",
         tol=100 * 2 * math.sqrt(0.25 / n))
    line("stop-out rate", 100 * (d["exit_reason"] == "stop").mean(),
         100 * EXPECTED.stop_hit_rate, "{:.1f}%",
         tol=100 * 2 * math.sqrt(0.25 / n))

    print(f"\n  MONTHLY ({months} month{'s' if months != 1 else ''} so far)")
    for m, v in monthly.items():
        pct = 100 * v / cap
        flag = "  <- losing month (35% expected)" if v < 0 else ""
        print(f"     {m}   Rs{v:>+10,.0f}   {pct:>+7.2f}%{flag}")

    if months >= 2:
        mp = 100 * monthly.to_numpy() / cap
        print(f"\n     mean {mp.mean():+.2f}%/mo vs expected {EXPECTED.monthly_mean_pct:+.2f}%")
        print(f"     losing {100*(mp<0).mean():.0f}% of months vs expected "
              f"{100*EXPECTED.monthly_loss_rate:.0f}%")

    # Is the sample big enough to conclude anything yet?
    print("\n" + "-" * 78)
    tt = _t(net)
    need = EXPECTED.months_to_significance
    print(f"  t-stat on live trades: {tt:+.2f}" if np.isfinite(tt) else "  t-stat: n/a")
    if months < need:
        print(f"  ** {months} of ~{need} months needed before live results can "
              f"distinguish skill from luck.")
        print(f"     At {EXPECTED.monthly_mean_pct:+.2f}%/mo and "
              f"{EXPECTED.monthly_sd_pct:.2f}% sd, that is how long t=2 takes.")
        print("     Do NOT tune parameters on what you have seen so far -- the")
        print("     walk-forward showed committing beats re-tuning.")
    worst = 100 * monthly.min() / cap if months else 0
    if months and worst < EXPECTED.worst_month_pct:
        print(f"  !! worst month {worst:+.2f}% is below the backtested worst "
              f"({EXPECTED.worst_month_pct:+.2f}%) -- investigate before continuing.")
    print("-" * 78 + "\n")
