"""
Live bid/ask/LTP capture and slippage arithmetic.

THE ONE NUMBER THIS EXISTS TO PRODUCE
-------------------------------------
The backtest assumes a short fills at the NEXT BAR'S OPEN. In reality a
short SELLS, which means hitting the BID that exists at the moment you
decide. The difference between those two prices, in basis points, is the
entry slippage the audit could not measure. Covering the short LIFTS THE
ASK, giving the exit half.

    entry_slip_bps = (theo_entry - bid) / theo_entry * 10_000
    exit_slip_bps  = (ask - theo_exit)  / theo_exit  * 10_000

Positive means WORSE than the backtest assumed, on both sides, so the two
add to a round-trip figure directly comparable with the audit's ~19 bps
round-trip (~9.6 bps/side) breakeven.

This is a conservative reading and is meant to be: it charges the full
spread rather than assuming a passive fill at the mid. A marketable order
into a high-volume breakdown -- which is exactly what this signal is -- has
no business assuming mid.

Fyers `depth()` rather than `quotes()`: depth carries the full L2 book plus
`lower_ckt`/`upper_ckt` (so a circuit lock is visible) and `tick_Size` (the
arithmetic floor on any fill). quotes() returns bid=0 outside market hours.

EVERY FUNCTION HERE RETURNS None RATHER THAN RAISING. Instrumentation must
never be able to interrupt or alter a trading session.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

_FY = None


@dataclass
class Quote:
    bid: float | None = None
    ask: float | None = None
    ltp: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    total_buy_qty: int | None = None
    total_sell_qty: int | None = None
    tick_size: float | None = None
    lower_ckt: float | None = None
    upper_ckt: float | None = None
    ok: bool = False
    error: str = ""

    @property
    def spread_bps(self) -> float | None:
        if not self.bid or not self.ask or self.bid <= 0 or self.ask <= 0:
            return None
        mid = (self.bid + self.ask) / 2
        return 10_000.0 * (self.ask - self.bid) / mid if mid > 0 else None

    @property
    def tick_bps(self) -> float | None:
        ref = self.ltp or self.ask or self.bid
        if not self.tick_size or not ref or ref <= 0:
            return None
        return 10_000.0 * self.tick_size / ref

    @property
    def at_lower_circuit(self) -> bool:
        return bool(self.lower_ckt and self.ltp and
                    abs(self.ltp - self.lower_ckt) < (self.tick_size or 0.01) / 2)


def _client():
    """Cached FyersModel. Returns None if credentials are missing."""
    global _FY
    if _FY is not None:
        return _FY
    try:
        from fyers_apiv3 import fyersModel
        from market_data import load_parent_env
        load_parent_env()
        app_id = os.environ.get("FYERS_APP_ID")
        token = os.environ.get("FYERS_ACCESS_TOKEN")
        if not app_id or not token:
            return None
        _FY = fyersModel.FyersModel(client_id=app_id, is_async=False,
                                    token=token, log_path="")
        return _FY
    except Exception:
        return None


def fetch(fyers_symbol: str) -> Quote:
    """Live L2 snapshot for one symbol. Never raises."""
    fy = _client()
    if fy is None:
        return Quote(error="no fyers client")
    try:
        resp = fy.depth({"symbol": fyers_symbol, "ohlcv_flag": "1"})
    except Exception as exc:
        return Quote(error=f"{type(exc).__name__}: {str(exc)[:60]}")
    if not isinstance(resp, dict) or resp.get("s") != "ok":
        return Quote(error=str(resp)[:80])
    d = resp.get("d") or {}
    if not isinstance(d, dict) or not d:
        return Quote(error="empty depth")
    book = d.get(fyers_symbol) or next(iter(d.values()))
    if not isinstance(book, dict):
        return Quote(error="unparseable depth")

    def _top(side):
        lst = book.get(side) or []
        if isinstance(lst, list) and lst and isinstance(lst[0], dict):
            return lst[0].get("price"), lst[0].get("volume")
        return None, None

    bid, bid_qty = _top("bids")
    ask, ask_qty = _top("ask")
    return Quote(
        bid=float(bid) if bid else None,
        ask=float(ask) if ask else None,
        ltp=float(book.get("ltp")) if book.get("ltp") else None,
        bid_qty=int(bid_qty) if bid_qty else None,
        ask_qty=int(ask_qty) if ask_qty else None,
        total_buy_qty=int(book.get("totalbuyqty") or 0) or None,
        total_sell_qty=int(book.get("totalsellqty") or 0) or None,
        tick_size=float(book.get("tick_Size")) if book.get("tick_Size") else None,
        lower_ckt=float(book.get("lower_ckt")) if book.get("lower_ckt") else None,
        upper_ckt=float(book.get("upper_ckt")) if book.get("upper_ckt") else None,
        ok=True,
    )


def entry_slip_bps(theo_entry: float | None, bid: float | None) -> float | None:
    """A short sells into the bid. Positive = filled worse than the backtest."""
    if not theo_entry or not bid or theo_entry <= 0 or bid <= 0:
        return None
    return 10_000.0 * (theo_entry - bid) / theo_entry


def exit_slip_bps(theo_exit: float | None, ask: float | None) -> float | None:
    """Covering a short lifts the ask. Positive = paid more than the backtest."""
    if not theo_exit or not ask or theo_exit <= 0 or ask <= 0:
        return None
    return 10_000.0 * (ask - theo_exit) / theo_exit
