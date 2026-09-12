"""
Phase 2 instrumentation store -- what does it actually cost to trade V1?

WHY THIS EXISTS
---------------
The audit found the strategy's survival margin is ~9.6 bps PER SIDE. Every
statistical question about V1 has a reassuring answer; the unmeasured one
is what price you can actually transact at. `journal.csv` cannot answer it,
because it records what the engine SIMULATED -- and the simulation is the
thing under suspicion.

So this logs a different thing: one row per QUALIFYING SIGNAL per day,
whether or not it became a trade, carrying the live quote at the moment the
breakdown was detected next to the price the engine assumed. The gap
between those two numbers is the answer.

STRATEGY V1 IS FROZEN. Nothing here feeds back into a trading decision --
no module in this file is imported by engine.py, and every call site is
wrapped so a probe failure cannot alter or interrupt a session. This
observes; it never votes. test_strategy_frozen.py enforces that.

Storage is SQLite (queryable, survives partial days) with a daily CSV
mirror (greppable, diffable, portable). Both are append-only and de-dupe
on (day, symbol) so a re-run cannot double-count -- same contract as
journal.py.
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "execution_log.db"
CSV_DIR = HERE / "execution_log"


@dataclass
class SignalRecord:
    """One qualifying signal, executed or not.

    Field groups, in the order they become known:
      identity/setup   -> known at 09:30 when the signal qualifies
      tradeability     -> known at 09:30 (NSE status lists)
      decision quote   -> captured when the breakdown is DETECTED live
      fills            -> known after the entry bar / at 15:15
      outcome          -> resolved at end of day
    """
    # --- identity & setup (09:30) ---
    day: str = ""
    symbol: str = ""
    # "live" rows are the only ones a slippage measurement may use. A replay
    # has no order book, so its rows exist for logging coverage only and are
    # excluded from the report by default -- mixing them in would silently
    # dilute the tradeability and slippage denominators.
    mode: str = "live"
    fyers_symbol: str = ""
    rel_volume: float | None = None
    or_high: float | None = None
    or_low: float | None = None
    or_pct: float | None = None
    selected: int = 0                  # 1 = had a slot, 0 = skipped (no slot)

    # --- tradeability (09:30); None = genuinely unknown, never guessed ---
    tradeable: int | None = None       # 1 yes, 0 no, None unknown
    reject_reason: str = ""            # "" when tradeable or unknown
    fno_ban: int | None = None
    asm_stage: str = ""                # "" = not listed, "unknown" = lookup failed
    gsm_stage: str = ""
    series: str = ""                   # EQ / BE / BZ -- BE and BZ are T2T (no intraday)

    # --- live quote at the moment the breakdown was detected ---
    decision_ts: str = ""
    decision_bid: float | None = None
    decision_ask: float | None = None
    decision_ltp: float | None = None
    decision_spread_bps: float | None = None
    decision_bid_qty: int | None = None
    decision_ask_qty: int | None = None
    total_buy_qty: int | None = None
    total_sell_qty: int | None = None
    tick_size: float | None = None
    tick_bps: float | None = None      # one tick as bps of price -- the floor on slippage
    lower_ckt: float | None = None
    upper_ckt: float | None = None
    detect_lag_s: float | None = None  # bar close -> quote capture

    # --- entry ---
    breakout_bar_ts: str = ""
    theo_entry: float | None = None    # next bar's open -- what the backtest assumes
    sim_entry: float | None = None     # what engine.simulate_day actually used
    sim_entry_ts: str = ""
    achievable_entry: float | None = None   # a short SELLS into the bid
    entry_slip_bps: float | None = None     # + = worse than the backtest assumed

    # --- exit ---
    exit_ts: str = ""                  # the SIMULATED exit time (from the trade)
    exit_quote_ts: str = ""            # when the exit book was actually photographed
    theo_exit: float | None = None
    sim_exit: float | None = None
    sim_exit_reason: str = ""
    exit_bid: float | None = None
    exit_ask: float | None = None
    exit_ltp: float | None = None
    achievable_exit: float | None = None    # covering a short LIFTS the ask
    exit_slip_bps: float | None = None

    # --- outcome & simulated P&L (copied from the trade, never recomputed) ---
    outcome: str = ""                  # traded | no_trigger | upside_break |
                                       # ambiguous_bar | no_next_bar | not_selected |
                                       # rejected | share_price_exceeds_ticket
    gross_pct: float | None = None
    cost_pct: float | None = None
    net_pct: float | None = None
    shares: int | None = None
    ticket_value: float | None = None
    notes: str = ""


COLUMNS = [f.name for f in fields(SignalRecord)]

_SQL_TYPES = {"day": "TEXT", "symbol": "TEXT"}


def _sql_type(name: str) -> str:
    f = {x.name: x for x in fields(SignalRecord)}[name]
    if f.type in ("int | None", "int"):
        return "INTEGER"
    if f.type in ("float | None", "float"):
        return "REAL"
    return "TEXT"


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    cols = ",\n  ".join(f"{c} {_sql_type(c)}" for c in COLUMNS)
    con.execute(f"CREATE TABLE IF NOT EXISTS signals (\n  {cols},\n"
                "  PRIMARY KEY (day, symbol)\n)")
    # Migrate a database written before a column existed, rather than failing
    # on the next insert with an arity mismatch.
    have = {r[1] for r in con.execute("PRAGMA table_info(signals)")}
    for c in COLUMNS:
        if c not in have:
            con.execute(f"ALTER TABLE signals ADD COLUMN {c} {_sql_type(c)}")
    # Re-running a day must not double-count, and must not silently keep a
    # worse earlier row either: a later run with a real quote should win.
    con.commit()
    return con


def write(records: list[SignalRecord], db_path: Path = DB_PATH,
          csv_dir: Path = CSV_DIR) -> int:
    """Upsert records into SQLite and mirror the day to CSV. Returns rows written."""
    if not records:
        return 0
    con = connect(db_path)
    ph = ",".join("?" * len(COLUMNS))
    rows = [tuple(asdict(r)[c] for c in COLUMNS) for r in records]
    con.executemany(
        f"INSERT INTO signals ({','.join(COLUMNS)}) VALUES ({ph}) "
        f"ON CONFLICT(day, symbol) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in COLUMNS if c not in ("day", "symbol")),
        rows,
    )
    con.commit()

    days = sorted({r.day for r in records})
    for d in days:
        cur = con.execute("SELECT " + ",".join(COLUMNS)
                          + " FROM signals WHERE day=? ORDER BY symbol", (d,))
        day_rows = cur.fetchall()
        csv_dir.mkdir(parents=True, exist_ok=True)
        with (csv_dir / f"{d}.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(COLUMNS)
            w.writerows(day_rows)
    con.close()
    return len(rows)


def load(db_path: Path = DB_PATH):
    """All logged signals as a DataFrame (empty frame when nothing logged)."""
    import pandas as pd
    if not db_path.exists():
        return pd.DataFrame(columns=COLUMNS)
    con = connect(db_path)
    df = pd.read_sql_query("SELECT * FROM signals ORDER BY day, symbol", con)
    con.close()
    return df


def summarise_day(records: list[SignalRecord]) -> str:
    """One-line console summary for the end of a live session."""
    n = len(records)
    traded = sum(1 for r in records if r.outcome == "traded")
    quoted = sum(1 for r in records if r.entry_slip_bps is not None)
    blocked = sum(1 for r in records if r.tradeable == 0)
    slips = [r.entry_slip_bps for r in records if r.entry_slip_bps is not None]
    msg = (f"  instrumentation: {n} signal(s), {traded} traded, "
           f"{blocked} broker-blocked, {quoted} with a live quote")
    if slips:
        msg += f", median entry slip {sorted(slips)[len(slips)//2]:+.1f} bps"
    return msg
