"""
Nifty 100 constituent list.

Primary source: NSE's public archives CSV. NSE aggressively blocks
non-browser requests and many cloud/datacenter IPs outright, so this
does a warm-up GET against the homepage first to pick up session
cookies, then requests the CSV with a browser-like User-Agent using the
same session. If that still fails (blocked IP, NSE layout change,
network issue), we fall back to a small hardcoded list of liquid
large-cap symbols and print a clear warning so the caller knows the
universe is not the real Nifty 100.

A --symbols-file CLI override (handled in run_backtest.py) always takes
precedence over both of these paths.
"""

from __future__ import annotations

import csv
import io
import sys

import requests

NSE_HOME_URL = "https://www.nseindia.com"
NIFTY100_CSV_URL = "https://archives.nseindia.com/content/indices/ind_nifty100list.csv"
NIFTY50_CSV_URL = "https://archives.nseindia.com/content/indices/ind_nifty50list.csv"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Fallback universe: liquid large-caps used only when the live NSE fetch
# fails. This is NOT a guaranteed accurate/current Nifty 100 list.
FALLBACK_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "SBIN", "BHARTIARTL", "ITC", "KOTAKBANK", "LT", "AXISBANK",
    "BAJFINANCE", "MARUTI", "ASIANPAINT", "HCLTECH", "SUNPHARMA",
    "TITAN", "ULTRACEMCO", "WIPRO", "NESTLEIND", "ONGC", "NTPC",
    "POWERGRID", "M&M", "TATAMOTORS", "TATASTEEL", "ADANIENT",
    "ADANIPORTS", "JSWSTEEL", "COALINDIA", "BAJAJFINSV", "TECHM",
    "INDUSINDBK", "GRASIM", "DRREDDY", "CIPLA", "EICHERMOT", "BPCL",
    "HEROMOTOCO", "DIVISLAB", "BRITANNIA", "SHREECEM", "UPL",
    "HDFCLIFE", "SBILIFE", "APOLLOHOSP",
]

# Trimmed to the ~35 most liquid large-caps for the Nifty 50 fallback --
# same caveat as above: NOT a guaranteed accurate/current Nifty 50 list.
FALLBACK_SYMBOLS_50 = FALLBACK_SYMBOLS[:35]


def _fetch_from_nse(csv_url: str) -> list[str]:
    """Try to pull a live NSE index constituent list."""
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    # Warm-up request: NSE requires a prior homepage hit to set cookies
    # before it will serve the archives CSV.
    session.get(NSE_HOME_URL, timeout=10)

    resp = session.get(csv_url, timeout=10)
    resp.raise_for_status()

    reader = csv.DictReader(io.StringIO(resp.text))
    symbols = [row["Symbol"].strip() for row in reader if row.get("Symbol")]

    if not symbols:
        raise ValueError("Parsed NSE CSV but found no symbols")

    return symbols


def load_symbols_from_file(path: str) -> list[str]:
    """Load a plain-text (one symbol per line) or CSV (column 'Symbol' or
    first column) symbols file supplied via --symbols-file."""
    with open(path, newline="") as f:
        content = f.read().strip()

    if not content:
        raise ValueError(f"Symbols file {path} is empty")

    lines = content.splitlines()
    if "," in lines[0] or lines[0].strip().lower() == "symbol":
        reader = csv.DictReader(io.StringIO(content))
        if reader.fieldnames and "Symbol" in reader.fieldnames:
            return [row["Symbol"].strip() for row in reader if row.get("Symbol")]
        # No header named Symbol -- treat first column of each row as symbol
        f2 = io.StringIO(content)
        return [row[0].strip() for row in csv.reader(f2) if row]

    return [line.strip() for line in lines if line.strip()]


def get_index_symbols(index: str = "nifty100", symbols_file: str | None = None) -> list[str]:
    """Return the trading universe as a list of NSE trading symbols.

    Resolution order:
      1. --symbols-file override, if provided.
      2. Live NSE index constituent CSV (`index`: "nifty50" or "nifty100").
      3. Hardcoded fallback list (with a printed warning).
    """
    if symbols_file:
        symbols = load_symbols_from_file(symbols_file)
        print(f"[universe] Loaded {len(symbols)} symbols from {symbols_file}")
        return symbols

    csv_url, fallback, label = {
        "nifty50": (NIFTY50_CSV_URL, FALLBACK_SYMBOLS_50, "Nifty 50"),
        "nifty100": (NIFTY100_CSV_URL, FALLBACK_SYMBOLS, "Nifty 100"),
    }[index]

    try:
        symbols = _fetch_from_nse(csv_url)
        print(f"[universe] Fetched {len(symbols)} {label} symbols from NSE")
        return symbols
    except Exception as exc:  # noqa: BLE001 - any network/parse failure falls back
        print(
            f"[universe] WARNING: could not fetch live {label} list from NSE "
            f"({exc.__class__.__name__}: {exc}). Falling back to a hardcoded "
            f"list of {len(fallback)} liquid large-cap symbols. This is "
            f"NOT the real, current {label} constituent list -- pass "
            f"--symbols-file for a reliable universe.",
            file=sys.stderr,
        )
        return list(fallback)


def get_nifty100_symbols(symbols_file: str | None = None) -> list[str]:
    return get_index_symbols(index="nifty100", symbols_file=symbols_file)


if __name__ == "__main__":
    syms = get_nifty100_symbols()
    print(f"Universe size: {len(syms)}")
    print(syms[:10])
