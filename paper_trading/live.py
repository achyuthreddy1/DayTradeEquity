"""
LIVE paper trading -- run this during market hours.

    python3 live.py                       # live, today
    python3 live.py --dry-run 2026-09-09  # replay a past day minute-by-minute

WHY THIS FILE EXISTS SEPARATELY FROM paper.py
----------------------------------------------
The historical data path is wrong for live use in two specific ways, both
of which fail SILENTLY rather than raising:

 1. CACHE POISONING. fyers_data_loader caches any fetch whose to_date is
    <= today -- which includes TODAY. The first fetch at 09:30 would cache
    a partial day and every later poll would return that frozen snapshot,
    so the strategy would watch 09:30 prices all afternoon. Fixed here by
    passing use_cache=False for the current day.

 2. PARTIAL-DAY DROPPING. market_data._regularize drops any day with
    fewer than 50% of the 75 expected bars, to filter broken historical
    days. Today has fewer than 37 bars until ~12:30, so today's data would
    be discarded entirely. Fixed here by fetching raw and skipping that
    cleaning.

Both behaviours are CORRECT for backtesting. They are just wrong live.

AUTHORITATIVE RECORD: intraday this only MONITORS and prints. The trade
that gets journalled is produced at end of day by engine.simulate_day --
the same function the backtest and `paper.py verify` use -- so the live
record cannot drift from the validated rules. Live polling gives you the
realism (watching it happen, seeing the real signal rate); the journal
stays authoritative.
"""
from __future__ import annotations

import argparse
import os
import sys
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import journal  # noqa: E402
from engine import Signal, allocate, simulate_day  # noqa: E402
from paper_config import STRATEGY, now_ist, today_ist  # noqa: E402
import instrumentation  # noqa: E402  (Phase 2: observes only, never votes)
import telegram_notifier  # noqa: E402  (observes only, never votes -- see its docstring)

POLL_SECONDS = 60

# The data resolution live.py requests, and how long to let the final bar
# settle past its own close before trusting it. See _settle_final_bar().
BAR_MINUTES = 5
SETTLE_MARGIN_SECONDS = 30


def _loader():
    from fyers_data_loader import FyersDataLoader
    from market_data import load_parent_env
    load_parent_env()
    tok = os.environ.get("FYERS_ACCESS_TOKEN")
    app = os.environ.get("FYERS_APP_ID")
    if not tok or not app:
        raise SystemExit("FYERS_APP_ID / FYERS_ACCESS_TOKEN missing -- run fyers_auth.py")
    return FyersDataLoader(app_id=app, access_token=tok,
                           cache_dir=str(HERE / ".fyers_cache"))


def fetch_today(ld, fyers_symbol: str, day: date, live: bool,
                history: pd.DataFrame | None = None) -> pd.DataFrame:
    """Today's 5-min bars, bypassing BOTH the cache and the partial-day
    filter. Returns a tz-naive, timestamp-indexed OHLCV frame.

    In a dry run the day already exists in the cached history, so it is
    sliced from there rather than re-fetched -- that keeps dry runs fully
    offline and instant, which is what makes them usable for rehearsing
    the process without a live token.
    """
    if history is not None and not history.empty:
        sliced = history[history.index.normalize() == pd.Timestamp(day)]
        if not sliced.empty:
            return sliced
    d = datetime.combine(day, datetime.min.time())
    raw = ld.fetch_intraday(fyers_symbol, d, d, resolution="5",
                            use_cache=not live)
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if getattr(df["timestamp"].dt, "tz", None) is not None:
        df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return df.set_index("timestamp").sort_index()


# The research scripts all fetched this exact range, so it is what sits in
# .fyers_cache. The disk cache is keyed by (symbol, from, to, resolution),
# so ONLY this exact range is a cache hit -- asking for "last 90 days"
# silently misses and re-downloads 3.7 years per symbol.
CACHED_RANGE = (datetime(2023, 1, 1), datetime(2026, 9, 9))
BASELINE_LOOKBACK_DAYS = 45      # calendar days, to cover 20 trading days
# Below this many usable baselines the session is broken, not quiet.
MIN_BASELINE_SYMBOLS = 30


def build_baselines(day: date) -> dict[str, float]:
    """Trailing 20-day mean opening-range volume per symbol, from COMPLETED
    prior days only.

    Fetch strategy matters here. A compact recent window is preferred: its
    to_date is in the past so it is cacheable, meaning the first run of the
    day costs ~100 API calls and every re-run that day is instant. If that
    fails -- expired token, no network -- it falls back to the broad range
    the research already cached, so a dry run still works offline.
    """
    from market_data import liquid_universe, load_parent_env, load_universe
    load_parent_env()
    uni = liquid_universe(STRATEGY.index)
    kw = dict(fyers_app_id=os.environ.get("FYERS_APP_ID"),
              fyers_access_token=os.environ.get("FYERS_ACCESS_TOKEN"))
    start = datetime.combine(day, datetime.min.time()) - timedelta(days=BASELINE_LOOKBACK_DAYS)
    end = datetime.combine(day, datetime.min.time()) - timedelta(days=1)
    try:
        data = load_universe(uni, start, end, **kw)
        if not data:
            raise RuntimeError("no data returned for the recent window")
    except Exception as exc:
        print(f"  recent-window fetch failed ({type(exc).__name__}: "
              f"{str(exc)[:80]})")
        print(f"  falling back to the cached range "
              f"{CACHED_RANGE[0].date()}..{CACHED_RANGE[1].date()}")
        data = load_universe(uni, *CACHED_RANGE, **kw)
    out: dict[str, float] = {}
    for sym, df in data.items():
        # Baseline must see PRIOR days only. `data` itself is left whole so
        # it can also serve the target day's bars to a dry run.
        prior = df[df.index < pd.Timestamp(day)]
        t = prior.index.time
        orb = prior[(t >= STRATEGY.or_start) & (t < STRATEGY.or_end)]
        vols = orb.groupby(orb.index.normalize())["volume"].sum()
        if len(vols) >= STRATEGY.rel_volume_lookback:
            out[sym] = float(vols.tail(STRATEGY.rel_volume_lookback).mean())
    if len(out) < MIN_BASELINE_SYMBOLS:
        raise RuntimeError(
            f"only {len(out)} symbols have a usable {STRATEGY.rel_volume_lookback}-day "
            f"baseline (need at least {MIN_BASELINE_SYMBOLS}). This is a DATA problem, "
            "not a quiet market -- do not read it as 'no signals today'.")
    return out, {i.symbol: i.fyers_symbol for i in uni}, data


def scan_opening_range(ld, tickers, baselines, day, live,
                       history: dict | None = None) -> tuple[list[Signal], dict]:
    """Opening ranges for every symbol, and the qualifying signals."""
    sigs, frames, errs = [], {}, []
    for sym, fy in tickers.items():
        base = baselines.get(sym)
        if not base or base <= 0:
            continue
        try:
            df = fetch_today(ld, fy, day, live,
                             (history or {}).get(sym))
        except Exception as exc:                      # one bad symbol must
            errs.append(f"{sym}: {str(exc)[:60]}")    # not kill the session
            continue
        if df.empty:
            continue
        frames[sym] = df
        t = df.index.time
        orb = df[(t >= STRATEGY.or_start) & (t < STRATEGY.or_end)]
        if len(orb) < 3:                              # OR not complete yet
            continue
        hi, lo = float(orb["high"].max()), float(orb["low"].min())
        vol = float(orb["volume"].sum())
        if hi <= lo:
            continue
        rel = vol / base
        if rel <= STRATEGY.rel_volume_min:
            continue
        sigs.append(Signal(sym, day, hi, lo, 100.0 * (hi - lo) / lo, rel))
    if errs:
        print(f"     ({len(errs)} symbol(s) unavailable, e.g. {errs[0]})")
    return sigs, frames


def status_line(sym: str, df: pd.DataFrame, s: Signal, cfg) -> str:
    """What this setup is doing right now."""
    t = df.index.time
    post = df[(t >= cfg.or_end) & (t <= cfg.exit_time)]
    if post.empty:
        return "waiting for the open range to break"
    lo, hi = post["low"].to_numpy(), post["high"].to_numpy()
    op = post["open"].to_numpy()
    for k in range(len(post)):
        if hi[k] > s.or_high and lo[k] < s.or_low:
            return "ambiguous bar (both sides) -- skipped"
        if hi[k] > s.or_high:
            return "broke UP -- not traded (long side is dead)"
        if lo[k] < s.or_low:
            if k + 1 >= len(post):
                return f"BROKE DOWN at {post.index[k].time()} -- entry on next bar's open"
            entry = float(op[k + 1])
            stop = entry * (1 + cfg.stop_pct / 100)
            last = float(post["close"].iloc[-1])
            for j in range(k + 1, len(post)):
                if hi[j] >= stop:
                    return (f"SHORT @{entry:.2f} -> STOPPED @{post.index[j].time()} "
                            f"({-cfg.stop_pct:.1f}% + slip)")
            return (f"SHORT @{entry:.2f}  now {last:.2f}  "
                    f"({100*(entry-last)/entry:+.2f}%)  stop {stop:.2f}")
    return f"inside range (low {post['low'].min():.2f} vs OR low {s.or_low:.2f})"


def _settle_final_bar(ld, cfg, chosen, frames, tickers, day, history,
                      poll: int) -> None:
    """Wait for the bar stamped cfg.exit_time to close, then refresh once.

    engine.simulate_day prices a time exit at the CLOSE of that bar
    (`cl[-1]`), and a 5-minute bar stamped 15:15 is not complete until
    15:20. The polling loop stops the instant the clock reads 15:15:00, so
    breaking out there hands the engine a bar a few seconds old: the
    journalled exit is whatever tick happened to land first, not the close
    the backtest computes. That is a systematic live-vs-backtest gap on
    every time exit -- the majority of trades -- in a forward test whose
    entire purpose is comparing live net% against a narrow expected band.
    It is invisible day to day because the journal and the instrumentation
    are built from the same frame, so they agree with each other while
    both disagree with the backtest.

    The exit-quote probe runs AFTER this wait, not before it. Fyers labels
    a bar by the START of its interval, so the bar stamped 15:15 covers
    15:15-15:20 and the fill it implies happens at 15:20. Snapshotting the
    book at 15:15 would price a trade five minutes before it occurs -- the
    same error as POLICYBZR's two-hour-late probe on 2026-09-17, just
    smaller and on every single time exit.
    """
    target = (datetime.combine(day, cfg.exit_time)
              + timedelta(minutes=BAR_MINUTES, seconds=SETTLE_MARGIN_SECONDS))
    if now_ist() < target:
        print(f"  waiting until {target.strftime('%H:%M:%S')} for the "
              f"{cfg.exit_time.strftime('%H:%M')} bar to close before journalling")
        while True:
            left = (target - now_ist()).total_seconds()
            if left <= 0:
                break
            _time.sleep(min(poll, max(1.0, left)))
    for s in chosen:
        try:
            frames[s.symbol] = fetch_today(ld, tickers[s.symbol], day, True,
                                           history.get(s.symbol))
        except Exception as exc:
            # Keep the last good frame rather than losing the day: a stale
            # final bar is a small price error, no frame is no trade record.
            print(f"     ! {s.symbol} final refresh failed "
                  f"({type(exc).__name__}) -- journalling the last good frame")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", metavar="YYYY-MM-DD",
                    help="replay a past day instead of trading live")
    ap.add_argument("--poll", type=int, default=POLL_SECONDS)
    a = ap.parse_args()

    live = a.dry_run is None
    day = today_ist() if live else datetime.strptime(a.dry_run, "%Y-%m-%d").date()
    cfg = STRATEGY

    print(f"\n{'LIVE' if live else 'DRY RUN'} paper trading -- {day}")

    # --- SESSION GATE (infrastructure, not strategy) -------------------
    # Runs BEFORE build_baselines(), which costs ~100 symbols x 20 days of
    # API calls. A closed day must cost nothing and must announce itself:
    # without this, a holiday reaches 09:30, finds no bars, and prints
    # "Nothing to trade today" -- identical to a genuinely quiet market.
    # Replays are exempt: they deliberately re-run past dates.
    if live:
        import session_guard
        st, code = session_guard.decide(day)
        if code != session_guard.EXIT_OPEN:
            print("  " + st.line())
            if code == session_guard.EXIT_UNKNOWN:
                print("  Refusing to start: a day we cannot verify is not traded.")
                # UNKNOWN means NSE was unreachable from this host, which is
                # worth waking a human for -- this is an ALERT.
                notifier = telegram_notifier.TelegramNotifier.from_env()
                notifier.notify(telegram_notifier.format_alert_message(
                    f"Session gate: calendar unknown for {day}",
                    "NSE holiday calendar could not be established -- refusing to "
                    "trade rather than assuming the market is open. Seed it with "
                    "market_calendar.py --refresh on a machine that can reach NSE."))
            elif code in (session_guard.EXIT_WEEKEND, session_guard.EXIT_HOLIDAY):
                # Clean, expected outcome -- a plain "no trades today" notice,
                # not an alert. In normal deployment this branch is never
                # reached (session_guard.py --exec never starts live.py on a
                # closed day), but it fires when live.py is run directly.
                notifier = telegram_notifier.TelegramNotifier.from_env()
                notifier.notify(telegram_notifier.format_no_trade_message(day, st.reason))
            print("  No polling, no signals, no records written.\n")
            raise SystemExit(code)
        print("  " + st.line())
    # -------------------------------------------------------------------

    print("Building 20-day opening-range volume baselines...")
    try:
        baselines, tickers, history = build_baselines(day)
    except Exception as exc:
        m = str(exc).lower()
        if ("authenticate" in m or "-16" in m or "failed to load" in m
                or "usable" in m):
            if live:
                notifier = telegram_notifier.TelegramNotifier.from_env()
                notifier.notify(telegram_notifier.format_alert_message(
                    f"Session aborted -- data problem ({day})",
                    f"Could not load market data: {str(exc)[:200]}\n\n"
                    "The Fyers access token expires daily -- likely needs "
                    "fyers_auth.py --auto rerun."))
            raise SystemExit(
                f"\n  Could not load market data: {str(exc)[:120]}\n\n"
                "  The Fyers access token expires DAILY. Refresh it with:\n"
                "      python3 fyers_auth.py\n"
                "  then re-run this script.\n\n"
                "  (Stopping rather than continuing -- an empty universe would\n"
                "   otherwise look identical to a day with no signals.)\n") from None
        if live:
            notifier = telegram_notifier.TelegramNotifier.from_env()
            notifier.notify(telegram_notifier.format_alert_message(
                f"Session aborted -- unexpected error ({day})",
                f"{type(exc).__name__}: {str(exc)[:200]}"))
        raise
    print(f"  baselines for {len(baselines)} symbols")

    ld = _loader()
    signals, frames, ticket, chosen = [], {}, 0.0, []
    statuses, quotes = {}, {}          # Phase 2 instrumentation only

    while True:
        now = now_ist().time() if live else cfg.exit_time
        if live and now < cfg.or_end:
            print(f"  {now.strftime('%H:%M:%S')}  waiting for the opening range "
                  f"to complete at {cfg.or_end}...")
            _time.sleep(a.poll)
            continue

        if not signals:
            print(f"\n  scanning {len(tickers)} symbols for qualifying opening ranges...")
            signals, frames = scan_opening_range(ld, tickers, baselines,
                                                 day, live, history)
            if not signals:
                print(f"  no setup cleared {cfg.rel_volume_min}x relative volume. "
                      "Nothing to trade today.\n")
                if live:
                    notifier = telegram_notifier.TelegramNotifier.from_env()
                    notifier.notify(telegram_notifier.format_session_message(day, 0, 0, []))
                return
            ticket = allocate(signals, cfg)
            rng = np.random.default_rng(int(day.strftime("%Y%m%d")))
            chosen = signals
            if len(signals) > cfg.max_concurrent:
                pick = sorted(rng.permutation(len(signals))[:cfg.max_concurrent])
                chosen = [signals[i] for i in pick]
            print(f"  {len(signals)} qualifying signal(s); trading "
                  f"{len(chosen)} at Rs{ticket:,.0f} each")
            for s in signals:
                mark = "TRADING" if s in chosen else "skipped (no slot)"
                print(f"     {s.symbol:<12} rel-vol {s.rel_volume:>5.1f}x  "
                      f"OR {s.or_low:.2f}-{s.or_high:.2f}   {mark}")
            print()

            # --- Phase 2 instrumentation (no effect on what is traded) ---
            try:
                import tradeability
                if not live:
                    # NSE publishes only TODAY's surveillance lists. Labelling a
                    # replayed day with them would invent history -- exactly the
                    # false reassurance this instrumentation exists to prevent.
                    print("  NSE status lists: skipped (replay -- "
                          "historical ASM/GSM/ban status is not published)")
                    raise RuntimeError("replay: status unknowable")
                print(tradeability.describe_availability(day))
                statuses = tradeability.lookup([s.symbol for s in signals], day)
                blocked = [f"{k} ({v.reject_reason})"
                           for k, v in statuses.items() if v.tradeable == 0]
                for b in blocked:
                    print(f"     BLOCKED  {b}")
            except RuntimeError:
                statuses = {}          # honest unknown, recorded as such
            except Exception as exc:
                print(f"  [instrumentation] status lookup failed: "
                      f"{type(exc).__name__}: {str(exc)[:70]}")

        for s in chosen:
            try:
                frames[s.symbol] = fetch_today(ld, tickers[s.symbol], day,
                                               live, history.get(s.symbol))
            except Exception as exc:
                print(f"     ! {s.symbol} refresh failed: {exc}")
                continue
            print(f"  {now_ist().strftime('%H:%M:%S') if live else '--:--:--'}  "
                  f"{s.symbol:<12} {status_line(s.symbol, frames[s.symbol], s, cfg)}")

            # --- Phase 2: snapshot the live book the FIRST time this signal
            # breaks down. This is the measurement the audit asked for; it
            # reads the market and writes nothing back into the decision. ---
            if live and s.symbol not in quotes:
                try:
                    # detect_breakdown_live, NOT classify_outcome: the latter
                    # carries simulate_day's 10-bar minimum, which would delay
                    # the probe to ~10:15 and turn 40 minutes of price drift
                    # into a fake slippage reading.
                    state, bar_ts, _ = instrumentation.detect_breakdown_live(
                        s, frames[s.symbol], cfg)
                    if state in ("broken", "entered"):
                        quotes[s.symbol] = instrumentation.capture_quote(
                            tickers[s.symbol], bar_ts)
                        q, _qts, _lag = quotes[s.symbol]
                        sp = q.spread_bps
                        print(f"               ^ book: bid {q.bid} / ask {q.ask}"
                              + (f"  spread {sp:.1f} bps" if sp is not None else "")
                              + (f"  lag {_lag:+.0f}s" if _lag is not None else "")
                              + ("  LATE" if _lag is not None and _lag > 90 else "")
                              + (f"  [{q.error}]" if not q.ok else ""))
                        # Persist immediately: a crash at 14:00 must not lose
                        # the one measurement the session existed to take.
                        try:
                            import execution_log
                            execution_log.write(instrumentation.build_records(
                                day, signals, chosen, frames, [], tickers,
                                statuses, quotes, mode="live"))
                        except Exception:
                            pass
                except Exception as exc:
                    print(f"  [instrumentation] quote probe failed: "
                          f"{type(exc).__name__}: {str(exc)[:60]}")

        done = (not live) or now_ist().time() >= cfg.exit_time
        if done:
            if live:
                # Settle FIRST, then probe. The engine's time exit is the
                # close of the bar labelled cfg.exit_time, which Fyers
                # labels by interval start -- so the fill lands at 15:20,
                # not 15:15. Probing the book at 15:15 would measure it
                # ~5 minutes before the trade it is supposed to price.
                _settle_final_bar(ld, cfg, chosen, frames, tickers, day,
                                  history, a.poll)
                try:
                    exit_recs = instrumentation.build_records(
                        day, signals, chosen, frames, [], tickers,
                        statuses, quotes, mode="live")
                    instrumentation.capture_exit_quotes(exit_recs, tickers)
                    quotes['__exit__'] = exit_recs
                except Exception as exc:
                    print(f"  [instrumentation] exit probe failed: "
                          f"{type(exc).__name__}: {str(exc)[:60]}")
            break
        _time.sleep(a.poll)

    # End of day: the AUTHORITATIVE record comes from the same engine the
    # backtest uses, never from the intraday monitoring above.
    print("\n  session over -- journalling via the shared engine")
    trades = []
    for s in chosen:
        df = frames.get(s.symbol)
        if df is None or df.empty:
            continue
        tr = simulate_day(s, df, cfg, ticket)
        if tr:
            trades.append(tr)
            print(f"     SHORT {tr.symbol:<12} {tr.shares:>4} sh @{tr.entry_price:>9.2f} "
                  f"-> {tr.exit_price:>9.2f} ({tr.exit_reason})  "
                  f"{tr.net_pct:>+7.3f}%  Rs{tr.net_rupees:>+9,.0f}")
    # --- Phase 2: record EVERY qualifying signal, executed or not. ---
    # Unselected signals still hold their 09:30 frame, so refresh them here
    # (after the close, purely for measurement) -- otherwise the opportunity
    # set looks smaller than it was and tradeability % is computed on a
    # denominator that quietly excludes everything we did not trade.
    try:
        for s in signals:
            if s.symbol in frames and s.symbol not in {c.symbol for c in chosen}:
                try:
                    frames[s.symbol] = fetch_today(ld, tickers[s.symbol], day,
                                                   live, history.get(s.symbol))
                except Exception:
                    pass
        recs = instrumentation.build_records(
            day, signals, chosen, frames, trades, tickers, statuses, quotes,
            mode="live" if live else "replay")
        import quote_probe as _qp
        for prior in quotes.get("__exit__", []):
            for r in recs:
                if r.symbol != prior.symbol:
                    continue
                r.exit_bid, r.exit_ask = prior.exit_bid, prior.exit_ask
                r.exit_ltp = prior.exit_ltp
                r.achievable_exit = prior.achievable_exit
                r.exit_quote_ts = prior.exit_quote_ts
                # Recompute, do NOT copy: the intraday value was measured
                # against a theo_exit taken before the final bar existed,
                # and r.theo_exit has since been recomputed from the full
                # frame. Copying would pair a fresh price with a stale one.
                _sl = _qp.exit_slip_bps(r.theo_exit, prior.exit_ask)
                r.exit_slip_bps = round(_sl, 2) if _sl is not None else None
        import execution_log
        execution_log.write(recs)
        print(execution_log.summarise_day(recs))
    except Exception as exc:
        print(f"  [instrumentation] day log failed: "
              f"{type(exc).__name__}: {str(exc)[:90]}")

    if not trades:
        print("     no setup broke down -- no trades today")
        if live:
            notifier = telegram_notifier.TelegramNotifier.from_env()
            notifier.notify(telegram_notifier.format_session_message(
                day, len(signals), len(chosen), []))
    else:
        # Telegram notification strictly AFTER journal.append() has
        # persisted the trades -- a Telegram outage or bad token can never
        # turn an already-journalled trade into something that looks
        # unrecorded. See telegram_notifier.py's failure-semantics note.
        added = journal.append(trades)
        print(f"\n  journalled {added} trade(s). Run:  python3 paper.py report")
        if live:
            notifier = telegram_notifier.TelegramNotifier.from_env()
            sent = notifier.notify(telegram_notifier.format_session_message(
                day, len(signals), len(chosen), trades))
            if notifier.enabled and not sent:
                print("  [telegram] notification failed -- trades remain fully "
                      "journalled; see the output above for details.")
    print()


if __name__ == "__main__":
    main()
