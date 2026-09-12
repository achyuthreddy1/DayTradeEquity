"""
Can this short actually be placed today?

WHY THIS EXISTS
---------------
The audit's §10 found that broker/exchange tradeability was never modelled
anywhere in the research, and flagged one specific discomfort: NSE's ASM
framework triggers on *unusual volume and volatility*, which is very nearly
a description of this strategy's entry signal (opening-range volume > 5x its
own 20-day average). If ASM systematically catches the trades that qualify,
the realizable trade set is smaller than the backtest's -- and nobody knows
by how much, because it has never been recorded.

So this records status, per signal, per day, from NSE's own published lists:

  T2T (series BE/BZ)  -- compulsory delivery. NO intraday at all. Hard block.
  F&O ban             -- restricts derivatives, not cash shorts, but many
                         brokers curtail MIS on banned names, so it is
                         recorded as a WARNING rather than a hard block.
  ASM / GSM           -- surveillance. Typically 100% margin and frequent
                         intraday restrictions. Recorded with its stage.

HONEST-UNKNOWN CONTRACT
-----------------------
Every lookup can fail -- NSE blocks datacentre IPs, changes URLs, and serves
these files inconsistently. On failure this returns UNKNOWN, never a
cheerful default. A signal whose status could not be checked must not be
counted as tradeable in the report, because that would manufacture exactly
the optimism the audit was written to prevent.

This module is NEVER consulted by the strategy. It annotates; it does not
filter. Whether to act on a block is a decision for after the measurement,
not during it.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

from nifty100_universe import BROWSER_HEADERS, NSE_HOME_URL

CACHE_DIR = Path(__file__).resolve().parent / ".nse_status"

EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
FO_BAN_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
# The archive CSVs for ASM/GSM now 404; NSE serves these as JSON APIs, and
# they key on ISIN rather than symbol -- hence the EQUITY_L ISIN map below.
ASM_URL = "https://www.nseindia.com/api/reportASM"
GSM_URL = "https://www.nseindia.com/api/reportGSM"

UNKNOWN = "unknown"


@dataclass
class Status:
    """None for tradeable means 'could not determine', never 'probably fine'."""
    tradeable: int | None = None
    reject_reason: str = ""
    fno_ban: int | None = None
    asm_stage: str = ""
    gsm_stage: str = ""
    series: str = ""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    s.get(NSE_HOME_URL, timeout=10)      # NSE needs a homepage hit for cookies
    return s


def _fetch_text(url: str, cache_name: str, day: date) -> str | None:
    """Fetch a status file, caching it per day. Returns None on any failure.

    Cached per day because these lists change daily and a stale copy would
    silently mislabel today's signals.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / f"{cache_name}-{day.isoformat()}.csv"
    if cached.exists():
        return cached.read_text()
    try:
        r = _session().get(url, timeout=15)
        r.raise_for_status()
        cached.write_text(r.text)
        return r.text
    except Exception:
        return None


def _equity_list(day: date):
    """(symbol -> series, isin -> symbol) from NSE's master equity list."""
    txt = _fetch_text(EQUITY_LIST_URL, "equity_l", day)
    if not txt:
        return None, None
    series, isin = {}, {}
    for row in csv.DictReader(io.StringIO(txt)):
        sym = (row.get("SYMBOL") or "").strip()
        if not sym:
            continue
        series[sym] = (row.get(" SERIES") or row.get("SERIES") or "").strip()
        code = (row.get(" ISIN NUMBER") or row.get("ISIN NUMBER") or "").strip()
        if code:
            isin[code] = sym
    return (series or None), (isin or None)


def _series_map(day: date) -> dict[str, str] | None:
    return _equity_list(day)[0]


def _fo_ban_set(day: date) -> set[str] | None:
    txt = _fetch_text(FO_BAN_URL, "fo_secban", day)
    if not txt:
        return None
    # The file is a bare list, sometimes numbered "1,SYMBOL" -- take the last
    # non-empty field of each line rather than assuming a header.
    out = set()
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("sr", "date", "f&o")):
            continue
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if parts:
            out.add(parts[-1].upper())
    return out or set()


def _stage_map(url: str, cache_name: str, day: date,
               isin_to_sym: dict[str, str] | None) -> dict[str, str] | None:
    """symbol -> surveillance stage, resolved through ISIN.

    Returns None when the list could not be fetched OR when the ISIN map is
    missing -- without the map the JSON cannot be tied to a symbol, and
    guessing "not listed" would be exactly the false reassurance this module
    exists to avoid.
    """
    import json
    if isin_to_sym is None:
        return None
    txt = _fetch_text(url, cache_name, day)
    if not txt:
        return None
    try:
        payload = json.loads(txt)
    except Exception:
        return None

    rows = []
    if isinstance(payload, dict):                 # ASM: longterm + shortterm
        for section in ("longterm", "shortterm"):
            block = payload.get(section) or {}
            for r in (block.get("data") or []):
                rows.append((r, section))
    elif isinstance(payload, list):               # GSM: flat list
        rows = [(r, "") for r in payload]

    out: dict[str, str] = {}
    for r, section in rows:
        sym = isin_to_sym.get((r.get("isin") or "").strip())
        if not sym:
            continue
        stage = (r.get("asmSurvIndicator") or r.get("gsmStage") or "listed").strip()
        label = f"{section} {stage}".strip() if section else stage
        out[sym] = label if sym not in out else f"{out[sym]}, {label}"
    return out


def lookup(symbols: list[str], day: date | None = None) -> dict[str, Status]:
    """Status for each symbol. Unavailable lists yield UNKNOWN, not False."""
    day = day or date.today()
    series, isin_to_sym = _equity_list(day)
    bans = _fo_ban_set(day)
    asm = _stage_map(ASM_URL, "asm", day, isin_to_sym)
    gsm = _stage_map(GSM_URL, "gsm", day, isin_to_sym)

    out: dict[str, Status] = {}
    for sym in symbols:
        u = sym.upper()
        st = Status()
        st.series = (series or {}).get(u, UNKNOWN if series is None else "")
        st.fno_ban = None if bans is None else int(u in bans)
        st.asm_stage = UNKNOWN if asm is None else asm.get(u, "")
        st.gsm_stage = UNKNOWN if gsm is None else gsm.get(u, "")

        reasons = []
        hard_block = False
        if st.series in ("BE", "BZ"):
            reasons.append(f"T2T series {st.series} (no intraday)")
            hard_block = True
        if st.gsm_stage and st.gsm_stage != UNKNOWN:
            reasons.append(f"GSM {st.gsm_stage}")
            hard_block = True
        if st.asm_stage and st.asm_stage != UNKNOWN:
            reasons.append(f"ASM {st.asm_stage}")
        if st.fno_ban == 1:
            reasons.append("F&O ban (broker MIS may be curtailed)")

        st.reject_reason = "; ".join(reasons)
        unknown_inputs = (series is None or bans is None
                          or asm is None or gsm is None)
        if hard_block:
            st.tradeable = 0
        elif unknown_inputs:
            st.tradeable = None          # honest unknown -- do NOT assume yes
        else:
            st.tradeable = 1
        out[sym] = st
    return out


def describe_availability(day: date | None = None) -> str:
    """Which NSE lists could actually be reached -- print this at session start."""
    day = day or date.today()
    bits = []
    _, isin_to_sym = _equity_list(day)
    for label, fn in (("series/T2T", lambda: _series_map(day)),
                      ("F&O ban", lambda: _fo_ban_set(day)),
                      ("ASM", lambda: _stage_map(ASM_URL, "asm", day, isin_to_sym)),
                      ("GSM", lambda: _stage_map(GSM_URL, "gsm", day, isin_to_sym))):
        try:
            ok = fn() is not None
        except Exception:
            ok = False
        bits.append(f"{label}={'ok' if ok else 'UNAVAILABLE'}")
    return "  NSE status lists: " + ", ".join(bits)
