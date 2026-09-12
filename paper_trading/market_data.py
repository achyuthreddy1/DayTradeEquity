"""
Market data for paper trading -- self-contained.

Vendored from the research code (pdh_breakout/data_loader.py and
strategy_lab/fetch_data.py, both archived at
~/trading-research-archive-20260911.tar.gz) so this directory has no dependency on the research tree,
which has been removed. Only the Fyers path is kept; the
CSV source and every research-only helper are dropped.

Cleaning behaviour is preserved EXACTLY as validated -- session filter,
day-quality drop, gap regularisation -- because the strategy was measured
on data shaped this way and changing it would silently change the rules.

NOTE the deliberate asymmetry with live.py: the `MIN_BARS_FRAC_TO_KEEP_DAY`
drop below is correct for HISTORY (it filters broken days) but wrong for
TODAY (a partial session has few bars and would be discarded until
midday). live.py therefore fetches the current day raw and skips this
path. Do not "unify" the two.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
BAR_MINUTES = 5
EXPECTED_BARS_PER_DAY = 75                 # (15:30 - 09:15) / 5 min
MIN_BARS_FRAC_TO_KEEP_DAY = 0.5            # drop a day below this fraction

MIDCAP100_CSV_URL = "https://archives.nseindia.com/content/indices/ind_niftymidcap100list.csv"


class DataLoadError(RuntimeError):
    pass


@dataclass
class InstrumentConfig:
    symbol: str                 # internal symbol
    fyers_symbol: str           # e.g. "NSE:HDFCBANK-EQ"
    instrument_type: str = "equity_intraday"
    has_volume: bool = True
    lot_size: int = 1


def load_parent_env() -> None:
    """Load .env from this directory or the project root into os.environ."""
    for env_path in (HERE / ".env", HERE.parent / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            line = line.removeprefix("export ").strip()
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        return


def liquid_universe(index: str = "niftymidcap100") -> list[InstrumentConfig]:
    """Current index constituents as cash-equity instruments.

    Fetched live from NSE so the universe tracks reconstitutions (NSE
    rebalances twice a year) instead of going stale against a hardcoded
    list. A few tickers do not follow the plain "<SYM>-EQ" pattern and
    simply fail to load; load_universe skips them with a warning.
    """
    if index == "niftymidcap100":
        from nifty100_universe import _fetch_from_nse
        symbols = _fetch_from_nse(MIDCAP100_CSV_URL)
        print(f"[universe] Fetched {len(symbols)} Nifty Midcap 100 symbols from NSE")
    else:
        from nifty100_universe import get_index_symbols
        symbols = get_index_symbols(index=index)
    return [InstrumentConfig(symbol=s, fyers_symbol=f"NSE:{s}-EQ") for s in symbols]


def _to_indexed_ohlcv(df: pd.DataFrame, ts_col: str = "timestamp") -> pd.DataFrame:
    df = df.rename(columns={c: c.lower() for c in df.columns})
    missing = {"open", "high", "low", "close", "volume"} - set(df.columns)
    if missing:
        raise DataLoadError(f"missing required columns {missing}; have {list(df.columns)}")
    df[ts_col] = pd.to_datetime(df[ts_col])
    if df[ts_col].dt.tz is not None:
        # A tz-aware source is almost always UTC; convert to IST wall-clock
        # rather than silently truncating the offset.
        df[ts_col] = df[ts_col].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df = df.set_index(ts_col)[["open", "high", "low", "close", "volume"]].sort_index()
    return df[~df.index.duplicated(keep="last")]


def _session_filter(df: pd.DataFrame) -> pd.DataFrame:
    t = df.index.time
    return df[(t >= SESSION_OPEN) & (t < SESSION_CLOSE)]


def _regularize(df: pd.DataFrame, symbol: str, verbose: bool = False) -> pd.DataFrame:
    """Drop low-quality days, reindex survivors onto the full 5-min grid,
    and fill small intraday gaps as "no trade happened" (O=H=L=C=last
    close, volume 0) rather than letting price appear to teleport."""
    if df.empty:
        return df
    out_days, dropped = [], []
    for d, day_df in df.groupby(df.index.date):
        if len(day_df) < MIN_BARS_FRAC_TO_KEEP_DAY * EXPECTED_BARS_PER_DAY:
            dropped.append((d, len(day_df)))
            continue
        grid = pd.date_range(datetime.combine(d, SESSION_OPEN),
                             periods=EXPECTED_BARS_PER_DAY, freq=f"{BAR_MINUTES}min")
        reg = day_df.reindex(grid)
        gaps = reg["close"].isna()
        if gaps.any():
            reg["close"] = reg["close"].ffill()
            for col in ("open", "high", "low"):
                reg[col] = reg[col].where(~gaps, reg["close"])
            reg["volume"] = reg["volume"].where(~gaps, 0.0)
        out_days.append(reg)
    if dropped and verbose:
        print(f"[market_data] {symbol}: dropped {len(dropped)} low-quality day(s)")
    if not out_days:
        return df.iloc[0:0]
    result = pd.concat(out_days)
    result.index.name = "timestamp"
    return result


class FyersDataSource:
    def __init__(self, app_id: str, access_token: str, cache_dir: str | Path = None):
        from fyers_data_loader import FyersDataLoader
        self._loader = FyersDataLoader(
            app_id=app_id, access_token=access_token,
            cache_dir=str(cache_dir or (HERE / ".fyers_cache")))

    def load_5min(self, inst: InstrumentConfig,
                  from_date: datetime, to_date: datetime) -> pd.DataFrame:
        raw = self._loader.fetch_intraday(inst.fyers_symbol, from_date, to_date,
                                          resolution="5")
        if raw.empty:
            raise DataLoadError(f"Fyers returned no data for {inst.symbol}")
        return _regularize(_session_filter(_to_indexed_ohlcv(raw)), inst.symbol)


def load_universe(universe: list[InstrumentConfig], from_date: datetime,
                  to_date: datetime, fyers_app_id: str = None,
                  fyers_access_token: str = None) -> dict[str, pd.DataFrame]:
    """{symbol: 5-min OHLCV}. One bad symbol is skipped with a warning
    rather than aborting the run -- a multi-instrument session should not
    die because a single ticker failed."""
    if not fyers_app_id or not fyers_access_token:
        raise DataLoadError("Fyers credentials missing -- run fyers_auth.py")
    src = FyersDataSource(fyers_app_id, fyers_access_token)
    out: dict[str, pd.DataFrame] = {}
    failures: list[str] = []
    for inst in universe:
        try:
            df = src.load_5min(inst, from_date, to_date)
            if not df.empty:
                out[inst.symbol] = df
            else:
                failures.append(f"{inst.symbol}: empty")
        except Exception as exc:
            failures.append(f"{inst.symbol}: {str(exc)[:70]}")

    # A TOTAL failure must raise, never return {} quietly. Returning an
    # empty universe makes an expired token look exactly like a quiet
    # market -- the caller finds no signals and reports "nothing to trade
    # today", so a broken session is indistinguishable from a real one.
    # That is the single worst failure mode for an unattended paper run.
    if not out and failures:
        raise DataLoadError(
            f"all {len(failures)} symbols failed to load. First error: "
            f"{failures[0]}")
    if failures:
        print(f"[market_data] {len(failures)} of {len(universe)} symbols "
              f"unavailable (e.g. {failures[0][:90]})")
    return out
