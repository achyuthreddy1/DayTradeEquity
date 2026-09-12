"""
Fyers API v3 data access layer -- free alternative to Dhan's paid Data API
subscription, usable with just a Fyers trading account.

Handles:
  - Daily-cached NSE capital-market instrument master download + symbol ->
    Fyers ticker mapping (e.g. "RELIANCE" -> "NSE:RELIANCE-EQ")
  - Paginated intraday (1-min) candle fetch, chunked into <=100-day windows
    (Fyers' documented limit per request for intraday resolutions)
  - Daily candle fetch, chunked into <=365-day windows
  - Inter-request delay to stay under rate limits

Auth: requires a daily-refreshed access token -- see fyers_auth.py.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from fyers_apiv3 import fyersModel

INSTRUMENT_MASTER_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"

MAX_INTRADAY_WINDOW_DAYS = 100
MAX_DAILY_WINDOW_DAYS = 365
REQUEST_DELAY_SECONDS = 0.35
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 1.5


def _now_ist() -> datetime:
    """Date-stamped cache keys must roll over on the MARKET's day, not the
    server's -- a UTC host is still on yesterday's date until 05:30 IST."""
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)


class FyersDataLoader:
    def __init__(
        self,
        app_id: str,
        access_token: str,
        cache_dir: str = ".fyers_cache",
        request_delay: float = REQUEST_DELAY_SECONDS,
    ):
        self.app_id = app_id
        self.access_token = access_token
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.request_delay = request_delay

        self.fyers = fyersModel.FyersModel(
            client_id=app_id, is_async=False, token=access_token, log_path=""
        )

        self._symbol_map: dict[str, str] | None = None

    # ------------------------------------------------------------------
    # Instrument master
    # ------------------------------------------------------------------
    def _instrument_master_path(self) -> Path:
        today = _now_ist().strftime("%Y-%m-%d")
        return self.cache_dir / f"NSE_CM-{today}.csv"

    def _download_instrument_master(self) -> Path:
        cache_path = self._instrument_master_path()
        if cache_path.exists():
            return cache_path

        print("[fyers] Downloading fresh NSE instrument master (daily cache miss)...")
        resp = requests.get(INSTRUMENT_MASTER_URL, timeout=60)
        resp.raise_for_status()
        cache_path.write_bytes(resp.content)
        return cache_path

    def build_symbol_map(self, symbols: list[str] | None = None) -> dict[str, str]:
        """Build {NSE trading symbol: Fyers ticker} for NSE cash equity,
        e.g. {"RELIANCE": "NSE:RELIANCE-EQ"}.

        The file has no header; column 9 is the Fyers ticker and column 13
        is the plain trading symbol (verified against the live file).
        """
        path = self._download_instrument_master()

        df = pd.read_csv(
            path,
            header=None,
            usecols=[9, 13],
            names=["fyers_symbol", "trading_symbol"],
            low_memory=False,
        )

        # Only cash-equity series (suffix "-EQ"); this excludes trade-to-trade
        # ("-BE") and other non-EQ series that can share a trading symbol.
        df = df[df["fyers_symbol"].str.endswith("-EQ")]

        if symbols is not None:
            df = df[df["trading_symbol"].isin(symbols)]

        symbol_map = dict(zip(df["trading_symbol"], df["fyers_symbol"]))
        self._symbol_map = symbol_map
        return symbol_map

    # ------------------------------------------------------------------
    # Candle response -> DataFrame
    # ------------------------------------------------------------------
    @staticmethod
    def _candles_to_df(response: dict) -> pd.DataFrame:
        candles = response.get("candles") if response else None
        if not candles:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame(candles, columns=["epoch", "open", "high", "low", "close", "volume"])
        # Fyers epochs are true UTC Unix timestamps; convert to IST wall-clock
        # (NSE session is 09:15-15:30 IST) and drop the tz to match the naive
        # timestamp convention used elsewhere in this codebase.
        df["timestamp"] = (
            pd.to_datetime(df["epoch"], unit="s", utc=True)
            .dt.tz_convert("Asia/Kolkata")
            .dt.tz_localize(None)
        )
        df = df.drop(columns="epoch")
        return df.sort_values("timestamp").reset_index(drop=True)

    def _fetch_history(self, symbol: str, from_date: datetime, to_date: datetime, resolution: str) -> dict:
        payload = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": from_date.strftime("%Y-%m-%d"),
            "range_to": to_date.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.fyers.history(data=payload)
                if response.get("s") == "error":
                    message = str(response.get("message", "")).lower()
                    if "token" in message or "auth" in message:
                        # Not transient (expired/invalid access token) -- retrying
                        # won't help, so fail fast with the real error visible.
                        raise RuntimeError(f"Fyers auth error for {symbol}: {response}")
                    raise RuntimeError(f"Fyers history error for {symbol}: {response}")
                time.sleep(self.request_delay)
                return response
            except RuntimeError as exc:
                if "auth error" in str(exc):
                    raise
                last_exc = exc
            except Exception as exc:  # noqa: BLE001
                last_exc = exc

            if attempt < MAX_RETRIES:
                backoff = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                print(
                    f"[fyers] Request failed ({last_exc.__class__.__name__}: {last_exc}), "
                    f"retrying in {backoff:.1f}s (attempt {attempt}/{MAX_RETRIES})..."
                )
                time.sleep(backoff)

        raise RuntimeError(f"Fyers history request for {symbol} failed after {MAX_RETRIES} attempts") from last_exc

    # ------------------------------------------------------------------
    # On-disk cache: fetched candle data doesn't change once a trading day
    # is over, so repeated backtest runs over the same date range (e.g.
    # sweeping strategy parameters) shouldn't have to re-hit the API at
    # all. Keyed on symbol/resolution/date-range. Callers almost always
    # pass to_date=today, so today IS included in the cache (otherwise
    # caching would never engage in practice) -- the tradeoff is that if
    # you run this again later the same day, a partial current session
    # already cached that morning won't refresh until the next calendar
    # day. Pass use_cache=False to force a live refetch.
    # ------------------------------------------------------------------
    def _candle_cache_path(self, symbol: str, from_date: datetime, to_date: datetime, resolution: str) -> Path:
        safe_symbol = symbol.replace(":", "_").replace("&", "_")
        cache_dir = self.cache_dir / "candles"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"{safe_symbol}_{resolution}_{from_date:%Y%m%d}_{to_date:%Y%m%d}.pkl"

    # ------------------------------------------------------------------
    # Intraday (1-min) candles, paginated into <=100-day chunks
    # ------------------------------------------------------------------
    def fetch_intraday(
        self,
        symbol: str,
        from_date: datetime,
        to_date: datetime,
        resolution: str = "1",
        use_cache: bool = True,
    ) -> pd.DataFrame:
        is_cacheable = use_cache and to_date.date() <= _now_ist().date()
        cache_path = self._candle_cache_path(symbol, from_date, to_date, resolution) if is_cacheable else None
        if cache_path is not None and cache_path.exists():
            return pd.read_pickle(cache_path)

        chunks: list[pd.DataFrame] = []
        window_start = from_date

        while window_start <= to_date:
            window_end = min(window_start + timedelta(days=MAX_INTRADAY_WINDOW_DAYS - 1), to_date)
            response = self._fetch_history(symbol, window_start, window_end, resolution)
            chunks.append(self._candles_to_df(response))
            window_start = window_end + timedelta(days=1)

        chunks = [c for c in chunks if not c.empty]
        if not chunks:
            result = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        else:
            result = pd.concat(chunks, ignore_index=True)
            result = result.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)

        if cache_path is not None:
            result.to_pickle(cache_path)
        return result

    # ------------------------------------------------------------------
    # Daily candles
    # ------------------------------------------------------------------
    def fetch_daily(self, symbol: str, from_date: datetime, to_date: datetime) -> pd.DataFrame:
        chunks: list[pd.DataFrame] = []
        window_start = from_date

        while window_start <= to_date:
            window_end = min(window_start + timedelta(days=MAX_DAILY_WINDOW_DAYS - 1), to_date)
            response = self._fetch_history(symbol, window_start, window_end, "D")
            chunks.append(self._candles_to_df(response))
            window_start = window_end + timedelta(days=1)

        chunks = [c for c in chunks if not c.empty]
        if not chunks:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        result = pd.concat(chunks, ignore_index=True)
        return result.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)

    # ------------------------------------------------------------------
    # Convenience: fetch intraday for a whole symbol universe
    # ------------------------------------------------------------------
    def fetch_universe_intraday(
        self,
        symbols: list[str],
        from_date: datetime,
        to_date: datetime,
    ) -> dict[str, pd.DataFrame]:
        if self._symbol_map is None:
            self.build_symbol_map(symbols)

        result: dict[str, pd.DataFrame] = {}
        for i, symbol in enumerate(symbols, start=1):
            fyers_symbol = self._symbol_map.get(symbol)
            if fyers_symbol is None:
                print(f"[fyers] WARNING: no Fyers ticker found for {symbol}, skipping")
                continue
            print(f"[fyers] ({i}/{len(symbols)}) Fetching intraday data for {symbol}...")
            try:
                df = self.fetch_intraday(fyers_symbol, from_date, to_date)
                if df.empty:
                    print(f"[fyers] WARNING: no intraday data returned for {symbol}")
                    continue
                result[symbol] = df
            except Exception as exc:  # noqa: BLE001
                print(f"[fyers] ERROR fetching {symbol}: {exc}, skipping")
        return result
