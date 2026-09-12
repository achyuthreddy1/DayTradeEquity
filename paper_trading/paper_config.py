"""
FROZEN configuration for the paper-trading forward test.

These parameters are the output of the research now archived in
~/trading-research-archive-20260911.tar.gz (strategy_lab/README.md inside it), and are
deliberately NOT tunable knobs. The walk-forward study found that
re-selecting parameters each period UNDERPERFORMED committing to one
choice and leaving it alone (+0.233%/trade adaptive vs +0.348%
commit-once), because the selection criterion is too noisy to re-tune on.

**DO NOT TUNE THESE DURING THE PAPER TEST.** Changing a parameter after
seeing live results destroys the entire point of a forward test: the
result stops being out-of-sample the moment the strategy is adjusted to
fit what it just saw. If a change seems necessary, start a NEW test with
a new start date rather than editing this file mid-run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """Wall-clock time in the MARKET's timezone, as a naive datetime.

    The session times below are IST, and so are bar timestamps once
    market_data normalises them. `datetime.now()` reads the SERVER's clock,
    which on a VPS is usually UTC -- there, live.py would sit waiting for the
    09:30 opening range until 15:00 IST and journal at 20:45 IST, while still
    printing plausible output. Reading the clock in IST explicitly makes the
    deployment timezone irrelevant.
    """
    return datetime.now(IST).replace(tzinfo=None)


def today_ist() -> date:
    """Today's date in IST -- see now_ist()."""
    return now_ist().date()


@dataclass(frozen=True)
class StrategyConfig:
    # --- entry rules (validated; full evidence in the archive) -----------
    index: str = "niftymidcap100"
    or_start: time = time(9, 15)
    or_end: time = time(9, 30)          # opening range is [09:15, 09:30)
    exit_time: time = time(15, 15)
    rel_volume_min: float = 5.0         # OR volume vs own trailing 20-day mean
    rel_volume_lookback: int = 20
    direction: str = "short"            # long side is statistically dead (t=+1.27)
    stop_pct: float = 2.0               # 2.0% fixed; chosen over 1.5%/0.5xOR
                                        # because it is hit less often (21.6%
                                        # vs 30%) so stop slippage touches
                                        # fewer trades and decays slowest

    # --- money management -------------------------------------------------
    capital: float = 100_000.0
    max_concurrent: int = 3             # returns plateau past 3 (rarely >3
                                        # signals/day); 3 slots capture 96%
                                        # of signals at sd 4.52% vs 5.55%
                                        # for a single concentrated position
    full_deployment: bool = True        # split capital across the day's
                                        # available signals, capped at
                                        # max_concurrent, rather than leaving
                                        # capital idle on quiet days

    # --- execution assumptions (for the simulated fill) -------------------
    entry_fill: str = "next_bar_open"   # matches the validated measurement
    stop_slippage_pct: float = 0.15     # realistic; the research showed the
                                        # return benefit of a stop crosses
                                        # over around 0.20% for a 2% stop


@dataclass(frozen=True)
class Expectation:
    """What the validated research says a month should look like.

    Used by the report to tell you whether live results are BEHAVING AS
    VALIDATED or genuinely breaking -- the single most useful thing during
    a forward test, because 35% of months are expected to lose money and
    without a reference a normal losing month looks like failure.

    Figures are the Rs1 lakh / 3-slot fully-deployed simulation, with a
    ~10% haircut applied for the curve-fitting premium the walk-forward
    measured (honest OOS ran ~10% below in-sample).
    """
    monthly_mean_pct: float = 2.12       # +2.36% in-sample, haircut ~10%
    monthly_sd_pct: float = 4.52
    monthly_loss_rate: float = 0.35      # 35% of months expected negative
    monthly_p05_pct: float = -4.28       # 5th percentile month
    worst_month_pct: float = -7.70
    per_trade_net_pct: float = 0.233     # honest walk-forward OOS
    per_trade_sd_pct: float = 1.98
    trades_per_month: float = 17.6       # at 3 slots
    win_rate: float = 0.551
    stop_hit_rate: float = 0.216
    months_to_significance: int = 25      # at t=2 on the monthly series


STRATEGY = StrategyConfig()
EXPECTED = Expectation()


def round_trip_cost_pct(ticket_value: float) -> float:
    """Explicit round-trip cost as % of turnover, for a given ticket size.

    Brokerage is Rs20 per executed order OR 0.03%, WHICHEVER IS LOWER, so
    the effective rate depends on ticket size -- this is why a Rs33k ticket
    pays 0.106% while a Rs1 lakh ticket pays 0.082%. Getting this wrong was
    a real error early in the research (the Rs200k-based 0.06% was applied
    to a Rs10k account, understating cost by nearly half).

    Components: brokerage x2 legs, STT 0.025% on the SELL leg only
    (intraday), NSE exchange charge 0.00297%/leg, SEBI 0.0001%/leg, stamp
    duty 0.003% on the BUY leg, GST 18% on (brokerage + exchange + SEBI).
    Slippage is NOT included here -- entry slippage is already inside the
    measured next-bar-open fill, and stop slippage is applied separately.
    """
    if ticket_value <= 0:
        raise ValueError(f"ticket_value must be positive, got {ticket_value}")
    brokerage = 2 * min(20.0, 0.0003 * ticket_value) / ticket_value * 100
    exchange = 0.00297 * 2
    sebi = 0.0001 * 2
    gst = 0.18 * (brokerage + exchange + sebi)
    return brokerage + 0.025 + exchange + sebi + 0.003 + gst
