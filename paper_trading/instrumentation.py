"""
Phase 2 orchestration: turn a day's signals into measured SignalRecords.

This is the only module live.py talks to. It assembles what the other three
produce -- execution_log (storage), quote_probe (live book), tradeability
(NSE status) -- into one row per qualifying signal.

THE FROZEN-STRATEGY CONTRACT
----------------------------
Nothing here may influence a trading decision. Concretely:

  * engine.py, paper_config.py and the trading path in live.py are not
    modified by this feature. This module imports FROM them, never the
    reverse -- test_strategy_frozen.py asserts that engine.py contains no
    reference to any instrumentation module.
  * The authoritative P&L numbers (gross/cost/net/shares) are COPIED from
    the Trade objects engine.simulate_day returns. They are never
    recomputed here, so instrumentation cannot disagree with the journal.
  * classify_outcome() below inspects bars to explain WHY a signal did not
    trade. It deliberately mirrors simulate_day's scan, which is a drift
    risk -- so test_strategy_frozen.py cross-checks its verdict against the
    real engine on every fixture and every logged day. If they ever
    disagree, the test fails rather than the report quietly lying.

WHY UNTRADED SIGNALS ARE LOGGED
-------------------------------
Tradeability % is the denominator of the whole Phase 2 question. If only
executed trades were recorded, a day where 4 of 5 signals were blocked by
ASM would look identical to a day with one signal -- and the measurement
would be exactly as blind as the backtest it is meant to check.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

import execution_log
import quote_probe
from execution_log import SignalRecord
from paper_config import STRATEGY, now_ist


def classify_outcome(signal, day_df: pd.DataFrame, cfg=STRATEGY):
    """Why did this signal trade or not? Returns (outcome, bar_ts, theo_entry).

    MIRRORS engine.simulate_day's scan for REPORTING ONLY. Kept in lockstep
    by test_strategy_frozen.test_diagnostic_agrees_with_engine, which runs
    both over the same bars and fails on any disagreement.
    """
    t = day_df.index.time
    post = day_df[(t >= cfg.or_end) & (t <= cfg.exit_time)]
    if len(post) < 10:
        return "insufficient_bars", "", None

    hi = post["high"].to_numpy(float)
    lo = post["low"].to_numpy(float)
    op = post["open"].to_numpy(float)
    n = len(post)

    for k in range(n):
        up, dn = hi[k] > signal.or_high, lo[k] < signal.or_low
        if not (up or dn):
            continue
        bar_ts = str(post.index[k])
        if up and dn:
            return "ambiguous_bar", bar_ts, None
        if up:
            return "upside_break", bar_ts, None
        if k + 1 >= n:
            return "no_next_bar", bar_ts, None
        return "traded", bar_ts, float(op[k + 1])
    return "no_trigger", "", None


def detect_breakdown_live(signal, partial_df: pd.DataFrame, cfg=STRATEGY):
    """Has the breakdown happened YET, on a still-growing intraday frame?

    SEPARATE FROM classify_outcome ON PURPOSE. classify_outcome mirrors
    simulate_day exactly, including its `len(post) < 10` data-quality guard.
    That guard is right at end of day (reject a day with too few bars) and
    catastrophically wrong live: a frame at 09:35 has ONE post-OR bar, so the
    guard suppresses detection until ~10:15 -- and a quote captured then
    measures 40 minutes of price drift, not execution slippage.

    So this applies no minimum-bar filter. It answers only "has price broken
    the opening-range low in the bars I have so far, and what is the next
    bar's open if it exists". It decides nothing about trading; it decides
    when to photograph the order book.

    Returns (state, bar_ts, theo_entry) where state is:
      "pending"   -- no break yet
      "broken"    -- broke down; the entry bar has not started (PROBE NOW)
      "entered"   -- broke down and the entry bar exists (probe is already late)
      "upside"    -- broke up; V1 does not trade this
      "ambiguous" -- one bar straddled both sides
    """
    t = partial_df.index.time
    post = partial_df[(t >= cfg.or_end) & (t <= cfg.exit_time)]
    if post.empty:
        return "pending", "", None
    hi = post["high"].to_numpy(float)
    lo = post["low"].to_numpy(float)
    op = post["open"].to_numpy(float)
    for k in range(len(post)):
        up, dn = hi[k] > signal.or_high, lo[k] < signal.or_low
        if not (up or dn):
            continue
        bar_ts = str(post.index[k])
        if up and dn:
            return "ambiguous", bar_ts, None
        if up:
            return "upside", bar_ts, None
        if k + 1 < len(post):
            return "entered", bar_ts, float(op[k + 1])
        return "broken", bar_ts, None
    return "pending", "", None


def theoretical_entry_instant(bar_ts, bar_spacing_s: float = 300.0):
    """When the theoretical fill happens: the breakout bar's CLOSE.

    Bars are start-labelled, so the bar indexed 09:30 covers 09:30-09:35 and
    the next bar's open -- the price V1 assumes -- prints at 09:35:00. Lag
    must be measured from THAT instant, not from the bar's label, or every
    reading is overstated by one bar width.
    """
    try:
        return pd.Timestamp(bar_ts).to_pydatetime() + pd.Timedelta(
            seconds=bar_spacing_s).to_pytimedelta()
    except Exception:
        return None


def build_records(
    day: date,
    signals: list,
    chosen: list,
    frames: dict[str, pd.DataFrame],
    trades: list,
    tickers: dict[str, str],
    statuses: dict | None = None,
    quotes: dict | None = None,
    mode: str = "live",
) -> list[SignalRecord]:
    """One SignalRecord per qualifying signal, executed or not.

    `quotes` maps symbol -> (Quote, captured_at_iso, detect_lag_s) collected
    live during the session; absent for a replay, where entry slippage is
    simply unmeasurable and stays None rather than being invented.
    """
    by_symbol = {t.symbol: t for t in trades}
    chosen_syms = {s.symbol for s in chosen}
    out: list[SignalRecord] = []

    for sig in signals:
        sym = sig.symbol
        rec = SignalRecord(
            day=day.isoformat(),
            symbol=sym,
            mode=mode,
            fyers_symbol=tickers.get(sym, ""),
            rel_volume=round(float(sig.rel_volume), 4),
            or_high=float(sig.or_high),
            or_low=float(sig.or_low),
            or_pct=round(float(sig.or_width_pct), 4),
            selected=int(sym in chosen_syms),
        )

        st = (statuses or {}).get(sym)
        if st is not None:
            rec.tradeable = st.tradeable
            rec.reject_reason = st.reject_reason
            rec.fno_ban = st.fno_ban
            rec.asm_stage = st.asm_stage
            rec.gsm_stage = st.gsm_stage
            rec.series = st.series

        df = frames.get(sym)
        if df is not None and not df.empty:
            outcome, bar_ts, theo = classify_outcome(sig, df)
            rec.outcome = outcome
            rec.breakout_bar_ts = bar_ts
            rec.theo_entry = round(theo, 4) if theo else None
            if outcome == "traded":
                post = df[(df.index.time >= STRATEGY.or_end)
                          & (df.index.time <= STRATEGY.exit_time)]
                if len(post):
                    rec.theo_exit = round(float(post["close"].iloc[-1]), 4)
        if not rec.selected and rec.outcome == "traded":
            rec.outcome = "not_selected"

        q = (quotes or {}).get(sym)
        if q is not None:
            quote, ts, lag = q
            rec.decision_ts = ts
            rec.detect_lag_s = round(lag, 1) if lag is not None else None
            rec.decision_bid = quote.bid
            rec.decision_ask = quote.ask
            rec.decision_ltp = quote.ltp
            rec.decision_bid_qty = quote.bid_qty
            rec.decision_ask_qty = quote.ask_qty
            rec.total_buy_qty = quote.total_buy_qty
            rec.total_sell_qty = quote.total_sell_qty
            rec.tick_size = quote.tick_size
            rec.lower_ckt = quote.lower_ckt
            rec.upper_ckt = quote.upper_ckt
            sp = quote.spread_bps
            rec.decision_spread_bps = round(sp, 2) if sp is not None else None
            tb = quote.tick_bps
            rec.tick_bps = round(tb, 2) if tb is not None else None
            rec.achievable_entry = quote.bid
            slip = quote_probe.entry_slip_bps(rec.theo_entry, quote.bid)
            rec.entry_slip_bps = round(slip, 2) if slip is not None else None
            if quote.at_lower_circuit:
                rec.notes = (rec.notes + "; at lower circuit at decision").strip("; ")

        tr = by_symbol.get(sym)
        if tr is not None:
            # Copied, never recomputed -- the journal stays authoritative.
            rec.sim_entry = tr.entry_price
            rec.sim_entry_ts = tr.entry_time
            rec.sim_exit = tr.exit_price
            rec.sim_exit_reason = tr.exit_reason
            rec.exit_ts = tr.exit_time
            rec.gross_pct = tr.gross_pct
            rec.cost_pct = tr.cost_pct
            rec.net_pct = tr.net_pct
            rec.shares = tr.shares
            rec.ticket_value = tr.ticket_value
        out.append(rec)
    return out


def capture_quote(fyers_symbol: str, breakout_bar_ts=None):
    """Snapshot the book now. Returns (Quote, iso_ts, lag_seconds).

    lag is measured from the THEORETICAL FILL INSTANT (the breakout bar's
    close), so 0 means the photograph was taken exactly when V1 assumes it
    transacted. A large lag makes that row incomparable with the breakeven
    and the report excludes it.
    """
    q = quote_probe.fetch(fyers_symbol)
    now = now_ist()
    lag = None
    if breakout_bar_ts is not None:
        instant = theoretical_entry_instant(breakout_bar_ts)
        if instant is not None:
            lag = (now - instant).total_seconds()
    return q, now.isoformat(timespec="seconds"), lag


def capture_exit_quotes(records: list[SignalRecord], tickers: dict[str, str]) -> None:
    """Fill in the exit half of the round trip, just before the 15:15 exit."""
    for rec in records:
        if rec.outcome != "traded":
            continue
        q = quote_probe.fetch(tickers.get(rec.symbol, ""))
        if not q.ok:
            continue
        rec.exit_bid, rec.exit_ask, rec.exit_ltp = q.bid, q.ask, q.ltp
        rec.exit_quote_ts = now_ist().isoformat(timespec="seconds")
        rec.achievable_exit = q.ask
        slip = quote_probe.exit_slip_bps(rec.theo_exit, q.ask)
        rec.exit_slip_bps = round(slip, 2) if slip is not None else None


def record_day(day, signals, chosen, frames, trades, tickers,
               statuses=None, quotes=None, mode="live") -> list[SignalRecord]:
    """Build and persist. Never raises -- instrumentation cannot break a session."""
    try:
        recs = build_records(day, signals, chosen, frames, trades, tickers,
                             statuses, quotes, mode)
        execution_log.write(recs)
        return recs
    except Exception as exc:
        print(f"  [instrumentation] logging failed: {type(exc).__name__}: "
              f"{str(exc)[:90]}")
        return []
