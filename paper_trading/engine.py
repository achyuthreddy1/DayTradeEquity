"""
Signal detection and trade simulation -- the SINGLE source of truth.

This module is used by BOTH the historical verification and the daily
paper-trading run. That is deliberate and important: if paper trading
reimplemented the rules, it would be forward-testing a different strategy
than the one that was validated, and the validation would not transfer.
`verify` in paper.py replays history through this exact code and checks it
reproduces the research numbers, so any drift is caught immediately.

Look-ahead safety, which is what makes a forward test meaningful:
  * the relative-volume baseline uses only PRIOR days' opening ranges
  * the signal is detected from a completed 5-min bar
  * entry is the NEXT bar's open, never the signal bar's close
  * the stop is evaluated bar by bar, never with knowledge of the day's end
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Iterable

import numpy as np
import pandas as pd

from paper_config import StrategyConfig, round_trip_cost_pct


@dataclass
class Signal:
    """A qualifying setup found during the opening range."""
    symbol: str
    day: date
    or_high: float
    or_low: float
    or_width_pct: float
    rel_volume: float


@dataclass
class Trade:
    symbol: str
    day: date
    direction: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    exit_reason: str          # "stop" | "time"
    stop_price: float
    rel_volume: float
    bars_held: int
    shares: int
    ticket_value: float
    gross_pct: float
    cost_pct: float
    net_pct: float
    net_rupees: float


def opening_range(day_df: pd.DataFrame, cfg: StrategyConfig):
    """(or_high, or_low, or_volume) from bars in [or_start, or_end)."""
    t = day_df.index.time
    m = (t >= cfg.or_start) & (t < cfg.or_end)
    if m.sum() < 2:
        return None
    od = day_df[m]
    return float(od["high"].max()), float(od["low"].min()), float(od["volume"].sum())


def build_or_tables(
    data: dict[str, pd.DataFrame], cfg: StrategyConfig
) -> dict[str, pd.DataFrame]:
    """Per-symbol table of daily opening ranges, computed ONCE.

    Built with a single groupby per symbol rather than re-filtering the
    prior 20 days for every day examined. That is not a cosmetic
    difference: the naive form costs ~1.8M dataframe filters when replaying
    3.7 years across 100 symbols (hours), versus one pass here.

    Columns: or_high, or_low, or_volume, and `base` -- the trailing
    `rel_volume_lookback`-day mean OR volume, SHIFTED by one day so a row's
    baseline uses strictly earlier days. That shift is what keeps the
    signal look-ahead-free.
    """
    out: dict[str, pd.DataFrame] = {}
    for sym, df in data.items():
        t = df.index.time
        orb = df[(t >= cfg.or_start) & (t < cfg.or_end)]
        if orb.empty:
            continue
        g = orb.groupby(orb.index.normalize())
        tab = pd.DataFrame({
            "or_high": g["high"].max(),
            "or_low": g["low"].min(),
            "or_volume": g["volume"].sum(),
            "bars": g.size(),
        })
        tab = tab[tab["bars"] >= 2]
        tab["base"] = (tab["or_volume"].rolling(cfg.rel_volume_lookback)
                       .mean().shift(1))
        out[sym] = tab
    return out


def find_signals(
    data: dict[str, pd.DataFrame], target_day: date, cfg: StrategyConfig,
    or_tables: dict[str, pd.DataFrame] | None = None,
) -> list[Signal]:
    """Qualifying setups for `target_day`, using only data up to that day.

    The relative-volume baseline comes from each symbol's own PRIOR opening
    ranges (the shifted rolling mean in `build_or_tables`), so nothing at
    or after 09:30 on the target day affects whether a setup qualifies.
    """
    tables = or_tables if or_tables is not None else build_or_tables(data, cfg)
    key = pd.Timestamp(target_day)
    out: list[Signal] = []
    for sym, tab in tables.items():
        if key not in tab.index:
            continue
        row = tab.loc[key]
        base = row["base"]
        if not np.isfinite(base) or base <= 0:
            continue
        or_high, or_low = float(row["or_high"]), float(row["or_low"])
        if or_high <= or_low:       # frozen/untraded open: not a valid ORB
            continue
        rel = float(row["or_volume"]) / float(base)
        if rel <= cfg.rel_volume_min:
            continue
        out.append(Signal(sym, target_day, or_high, or_low,
                          100.0 * (or_high - or_low) / or_low, rel))
    return out


def simulate_day(
    signal: Signal, day_df: pd.DataFrame, cfg: StrategyConfig, ticket_value: float
) -> Trade | None:
    """Walk the post-opening-range bars and return the resulting trade.

    Returns None when the setup never triggers, triggers to the upside
    (long side is not traded), or triggers on an ambiguous bar.
    """
    t = day_df.index.time
    post = day_df[(t >= cfg.or_end) & (t <= cfg.exit_time)]
    if len(post) < 10:
        return None

    hi = post["high"].to_numpy(float)
    lo = post["low"].to_numpy(float)
    op = post["open"].to_numpy(float)
    cl = post["close"].to_numpy(float)
    idx = post.index
    n = len(post)

    for k in range(n):
        up, dn = hi[k] > signal.or_high, lo[k] < signal.or_low
        if not (up or dn):
            continue
        if up and dn:
            return None          # intrabar order unknowable
        if up:
            return None          # short only
        if k + 1 >= n:
            return None          # no bar left to enter on
        entry = float(op[k + 1])
        if entry <= 0:
            return None

        stop = entry * (1 + cfg.stop_pct / 100.0)
        exit_price, exit_reason, exit_i = float(cl[-1]), "time", n - 1
        for j in range(k + 1, n):
            if hi[j] >= stop:
                # fill at the worse of the stop and the bar's open (a bar
                # that gapped through fills at the gap), then stop slippage
                fill = max(stop, float(op[j])) * (1 + cfg.stop_slippage_pct / 100.0)
                exit_price, exit_reason, exit_i = fill, "stop", j
                break

        gross = 100.0 * (entry - exit_price) / entry      # short
        shares = int(ticket_value // entry)
        if shares < 1:
            return None          # share price exceeds the ticket
        actual = shares * entry
        cost = round_trip_cost_pct(actual)
        net = gross - cost
        return Trade(
            symbol=signal.symbol, day=signal.day, direction="short",
            entry_time=str(idx[k + 1].time()), entry_price=round(entry, 2),
            exit_time=str(idx[exit_i].time()), exit_price=round(exit_price, 2),
            exit_reason=exit_reason, stop_price=round(stop, 2),
            rel_volume=round(signal.rel_volume, 2), bars_held=exit_i - k,
            shares=shares, ticket_value=round(actual, 2),
            gross_pct=round(gross, 4), cost_pct=round(cost, 4),
            net_pct=round(net, 4), net_rupees=round(actual * net / 100.0, 2),
        )
    return None


def allocate(signals: list[Signal], cfg: StrategyConfig) -> float:
    """Ticket size per position for a day with `len(signals)` setups.

    Under full deployment the whole account is split across however many
    signals exist (capped at max_concurrent), so capital is not left idle
    on single-signal days. The cost of that is concentration: on a
    one-signal day the entire account sits in one midcap short.
    """
    k = min(len(signals), cfg.max_concurrent)
    return cfg.capital / k if k else 0.0


def run_day(
    data: dict[str, pd.DataFrame], target_day: date, cfg: StrategyConfig,
    rng: np.random.Generator | None = None,
    or_tables: dict[str, pd.DataFrame] | None = None,
) -> tuple[list[Trade], list[Signal]]:
    """All trades for one day, plus every signal found (including skipped).

    When more signals fire than there are slots, the surplus is chosen at
    random -- NOT by ranking. rank_study.py tested ten ranking rules
    (trailing profit factor, relative volume, opening-range width, price)
    and every one fell inside the 95% band of random picking, so there is
    no evidence any selection rule beats chance. Random keeps the live
    behaviour honest rather than implying skill that was not measured.
    """
    signals = find_signals(data, target_day, cfg, or_tables)
    if not signals:
        return [], []
    ticket = allocate(signals, cfg)
    chosen = signals
    if len(signals) > cfg.max_concurrent:
        rng = rng or np.random.default_rng()
        pick = rng.permutation(len(signals))[:cfg.max_concurrent]
        chosen = [signals[i] for i in sorted(pick)]

    trades: list[Trade] = []
    for s in chosen:
        df = data[s.symbol]
        day_df = df[df.index.normalize() == pd.Timestamp(target_day)]
        tr = simulate_day(s, day_df, cfg, ticket)
        if tr:
            trades.append(tr)
    return trades, signals
