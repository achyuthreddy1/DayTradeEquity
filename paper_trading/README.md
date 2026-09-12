# Paper trading — midcap ORB-short forward test

Forward test of the one strategy that survived the research. **Nothing
here is tunable.** The parameters in
`paper_config.py` are frozen outputs of that research; the point of this
directory is to find out whether they hold up on data that did not exist
when they were chosen.

## Why this exists

Every number below comes from data the strategy was developed on. The walk-forward mitigated that for the two numeric
parameters, but it could not un-choose the universe (midcaps), the
direction (short), or the setup (opening-range breakdown) — all picked
with full-sample knowledge. **Only data that did not exist when the rules
were written can settle it.** That is what this journal accumulates.

## Daily use

```bash
cd paper_trading
source ../.venv/bin/activate

python3 fyers_auth.py --auto     # BEFORE 09:15 -- refresh today's token (headless)
python3 live.py                  # start ~09:25, runs until 15:15, journals at close
python3 paper.py report          # performance vs. what the research predicts
python3 slippage_report.py       # Phase 2: what execution actually cost
```

## The daily token

The Fyers access token **expires every day**. It lives in `paper_trading/.env`
on this line:

```
export FYERS_ACCESS_TOKEN=eyJhbGciOi...
```

Four ways to refresh it, depending on the machine:

```bash
python3 fyers_auth.py --auto         # headless login via TOTP -- the normal one
python3 fyers_auth.py --check        # is the current one still valid?
python3 fyers_auth.py                # full login -- NEEDS A BROWSER
python3 fyers_auth.py --token "eyJ…" # paste one obtained elsewhere
```

**`--auto` is the one to use, including on a headless VPS.** It generates the
2FA code itself from the TOTP secret in `.secrets` and needs no browser, so it
can run unattended from cron. Read the security warning at the top of
`fyers_totp.py` first: `.secrets` holds your PIN and TOTP secret, which is
durable account access, unlike a token that dies each evening. Keep it mode
600 — the loader refuses to run if it is looser.

The other three are fallbacks. `--token` exists for a machine you do not want
`.secrets` on: run the full `python3 fyers_auth.py` on your laptop, copy the
token from its `.env`, and paste it over with
`python3 fyers_auth.py --token "<paste>"`. It also accepts `appId:token` and a
redirect URL containing `access_token=`, since those are the forms Fyers
tooling tends to hand you.

Every path verifies the new token with a real API call, so you find out before
the open rather than at 09:30.

### Refreshing from cron

`--auto` exits non-zero on failure, so it can gate the session:

```cron
30 8 * * 1-5  cd ~/Desktop/Trading/Day\ trading\ India/paper_trading && ../.venv/bin/python fyers_auth.py --auto >> auth.log 2>&1
```

Leave time between the refresh and the open: Fyers throttles repeated login
attempts with a `429` that lasts minutes, so a failure at 09:14 may not have
cleared by 09:15. 08:30 gives you room to notice and intervene.

`--check` exits 1 on a bad token, so it can gate a cron job:

```bash
python3 fyers_auth.py --check && python3 live.py
```

Run it once after the close each day. If you miss days:

```bash
python3 paper.py backfill --from 2026-09-01 --to 2026-09-10
```

`journal.csv` is append-only and de-duplicates on `(symbol, day)`, so
re-running a day cannot double-count.

## Phase 2 — measuring what it costs to trade

**Strategy V1 is frozen.** Signal, parameters, entry, stop, exit and selection
are untouched; `test_strategy_frozen.py` fails if any of them move. What was
added is instrumentation, because the audit found the edge survives or dies on
one unmeasured number: **the execution breakeven is ~9.6 bps per side**, and
nobody has ever recorded an actual fill.

```bash
python3 live.py                  # now also logs every signal it sees
python3 slippage_report.py       # the Phase 2 answer
python3 test_strategy_frozen.py  # proof V1 did not change
python3 test_instrumentation.py  # proof the instrument measures correctly
```

Every QUALIFYING SIGNAL is recorded, traded or not, to `execution_log.db`
(SQLite) with a daily CSV mirror in `execution_log/`. Untraded and blocked
signals are logged deliberately: tradeability % is the denominator of the
whole question, and logging only executed trades would be exactly as blind
as the backtest it is meant to check.

Each row carries the live book at the moment the breakdown was detected
(bid / ask / LTP / depth / tick size / circuit limits), the theoretical
next-bar-open the backtest assumes, the engine's simulated fill, and the gap
between them in basis points. Sign convention: **positive always means worse
than the backtest assumed**, on both legs, so entry and exit add to a
round-trip figure directly comparable with the 19.2 bps breakeven.

NSE surveillance status (T2T series, ASM, GSM, F&O ban) is stamped per signal
from NSE's own lists. A lookup that fails records **unknown**, never "fine" —
a signal whose status could not be checked is excluded from tradeability %
rather than counted as tradeable. Replays skip the lookup entirely, because
NSE publishes only today's lists and labelling a past day with them would
invent history.

### Reading the report

Benchmark against **+0.193%/trade**, the audit's fully-adjusted figure — not
the research's +0.343% or +0.233%. The paper engine is deliberately stricter
than the backtest (ticket-aware costs, 0.15% stop slippage, whole shares), so
judging it against the research headline makes correct behaviour look broken.

The pre-registered kill criterion is in the report: **stop if mean entry
slippage exceeds 10 bps/side over 40 trades.** Below ~30 quoted fills the
report says the mean is noise rather than printing a confident number.

### What instrumentation may never do

It observes; it never votes. `engine.py`, `paper_config.py` and `journal.py`
contain no reference to any instrumentation module, and the freeze test
asserts that. Every probe is wrapped so a failed quote or an unreachable NSE
list prints a line and the session continues — a measurement problem must
never become a trading problem.

## Market holidays and weekends

The system now refuses to start on a non-trading day. Before this, a holiday
reached 09:30, found no bars, and printed *"no setup cleared 5.0x relative
volume. Nothing to trade today"* — identical to a genuinely quiet market, and
only after spending ~100 symbols × 20 days of API calls.

```bash
python3 session_guard.py                 # check today; the exit code is the answer
python3 session_guard.py --exec live.py  # run the session only if open
python3 market_calendar.py --refresh     # seed/refresh the NSE calendar
python3 market_calendar.py 2026-09-14    # ask about a specific date
```

Holidays come from NSE's own published master (`holiday-master?type=trading`,
segment CM), cached per year under `.nse_status/` and refreshed weekly, so
future years need no code change. Weekends are decided arithmetically and need
no calendar at all — an NSE outage can never make a Saturday look tradeable.

**It fails closed.** If the calendar for the year cannot be established, the
guard reports UNKNOWN and refuses to trade rather than assuming the market is
open. `is_open` is true only on positive evidence.

| Exit | Meaning | systemd |
|---|---|---|
| 0 | open, session ran | success |
| 10 | weekend | success |
| 11 | NSE holiday | success |
| 12 | calendar unknown, **did not trade** | **failure — alerts** |
| 13 | open but `live.py` failed | failure — alerts |

`live.py` calls the same check itself, so running it directly is also gated.
Replays (`--dry-run`) are exempt by design. Deployment units are in
[`deploy/`](deploy/).

## Telegram alerting

Off by default. Turn it on in `paper_trading/.env`:

```
export TELEGRAM_ENABLED=true
export TELEGRAM_BOT_TOKEN=<from @BotFather>
export TELEGRAM_CHAT_ID=<message the bot once, then GET .../getUpdates>
```

Three kinds of message, all sent only for a **live** session (never for
`--dry-run` replays):

- **End-of-day summary**, after `journal.append()` has already persisted
  the day's trades — sent whether or not anything traded, so a quiet day
  and a dead bot don't look the same on a VPS you aren't watching. Shows
  each trade's entry/exit/exit-reason/net P&L and the day total.
- **No-trade notice** for weekend (code 10) and NSE holiday (code 11) —
  plain "No trades today — weekend (Saturday)" / "— NSE holiday: Diwali"
  style messages, not framed as alerts, since these are clean and
  expected. Fired from `session_guard.py` (the path systemd actually
  takes), with the same check inside `live.py` as a defense-in-depth
  fallback for someone running it directly.
- **Failure alerts** for exactly the two things in the exit-code table
  above that are supposed to page a human: calendar `UNKNOWN` (code 12,
  from `session_guard.py`) and `live.py` dying for any reason (code 13, or
  caught directly in `live.py`'s own token/data-failure branch).

`telegram_notifier.py` is observation-only, the same guarantee
`instrumentation.py` documents for itself: it cannot affect the exit code,
the journal, or what gets traded, and `engine.py` / `paper_config.py` /
`journal.py` never import it (enforced by the ISOLATION check in
`test_strategy_frozen.py`). A missing token, a Telegram outage, or any
other failure here is caught and printed, never raised — see
`test_telegram_notifier.py`.

## Before (and after) deploying to a server

```bash
./predeploy_check.sh
```

Eight checks, ~90s. **Run it on the VPS too, not just here** — the ones that
matter most (host timezone, tz database, token, NSE reachability) are exactly
the ones that pass on your laptop and fail on a fresh server. Exits non-zero,
so it can gate a deploy.

The session clock is read in IST explicitly (`paper_config.now_ist`), so the
host's own timezone does not matter. Do not "simplify" those calls back to
`datetime.now()`: on a UTC server that makes live.py wait for the 09:30
opening range until 15:00 IST and journal at 20:45, while still printing
plausible output the whole time.

## Check the engine before trusting it

```bash
python3 paper.py selftest
```

23 deterministic checks on synthetic bars — no market data, no network,
runs in milliseconds. Asserts the rules that decide a trade: entry is the
*next* bar's open (not the signal bar's close), the stop sits 2% *above*
entry for a short, a stop fill is never better than the stop level, an
upside break is not traded, the cost model switches between flat ₹20
and the 0.03% rate at the right ticket size, and — importantly — that a
**total data failure raises rather than returning an empty universe**.
**Run it after any edit to `engine.py` or `market_data.py`.**

### The failure mode that check exists for

With an expired token, the data layer originally skipped all 100 symbols
one by one and returned `{}`. The run then found no signals and printed
*"no setup cleared 5.0x relative volume. Nothing to trade today."* — so a
dead token looked exactly like a quiet market. Unattended, you would see a
plausible message every day and believe the strategy was running. It now
raises, and `live.py` additionally refuses to start with fewer than 30
usable baselines, saying explicitly that it is a data problem rather than
an absence of signals.

This replaces the old `verify`, which replayed 3.7 years against the
research code. That comparison has already run and **PASSED** (research
n=837 mean +0.3214% vs paper n=832 mean +0.3233%). The 5-trade gap was
explained: signal detection is identical, but the research code — when a
breakout lands on the day's *final* bar — falls back to using that bar's
close as the entry, and since that bar is also the exit it fabricates a
~0% trade. The paper engine declines instead, because you cannot fill at
the next bar's open when there is no next bar. Those 5 trades totalled
0.01 percentage points. **The paper engine is the more correct of the
two.** The research tree and its 898MB cache are now gone, so that replay
cannot be re-run — which is fine, its job is done.

## Evidence behind these numbers

The research tree was removed to keep this deployable on a small VPS. It is
archived at `~/trading-research-archive-20260911.tar.gz` (13MB) — unpack it
if you ever need the full derivation.

Headline findings the frozen config rests on:

| Finding | Result |
|---|---|
| In-sample edge (2% stop, next-bar fills) | +0.343%/trade, t=+5.03, n=837 |
| **Honest walk-forward OOS** | **+0.233%/trade, t=+3.01** (adaptive), **+0.348%, t=+2.97** (commit-once) |
| Stop chosen without hindsight | selected in **4 of 5** walk-forward folds |
| Parameter robustness | all 20 cutoff × year cells positive |
| Long side | +0.110%, t=+1.26 — dead, with or without stops |
| Ranking rules (10 tested) | none beat random picking |
| Scalping (5–15 min) | move < cost — closed arithmetically |
| Engine vs research replay | n=837 +0.3214% vs n=832 +0.3233% — PASS |

## The strategy (frozen)

| | |
|---|---|
| Universe | Nifty Midcap 100 |
| Filter | opening-range (09:15–09:30) volume > **5×** its own trailing 20-day mean |
| Direction | **short only** (long side is dead: t=+1.27) |
| Entry | break **below** the opening-range low, filled at the **next 5-min bar's open** |
| Stop | **2.0%** fixed |
| Exit | stop, else 15:15 |
| Capital | ₹1,00,000, **3 concurrent positions**, fully deployed |
| Surplus signals | chosen at **random** — see below |

**Why random selection.** `rank_study.py` tested ten ways to pick the
day's best signal (trailing profit factor, relative volume, opening-range
width, share price). Every one landed inside the 95% band of random
picking. There is no evidence any rule beats chance, so the engine picks
at random rather than implying skill that was never measured. The seed is
the date, so runs are reproducible.

## Reading the report

The report compares live results against the band the research predicts,
because the hardest part of a forward test is telling a normal bad month
from a broken strategy:

- **35% of months are expected to lose money.** A losing month is not
  evidence of failure and should not trigger any change.
- **~25 months** are needed before live results can distinguish skill from
  luck (at +2.12%/mo and 4.52% sd, that is when t=2).
- The report flags **"OUTSIDE band"** only when a statistic is more than
  2 standard errors from expectation, and warns if the worst month breaks
  the backtested worst (−7.70%).

**Do not tune parameters on what you see here.** The walk-forward found
that re-selecting parameters each period *underperformed* committing to
one choice (+0.233%/trade vs +0.348%). If you decide a change is
genuinely needed, start a NEW test with a new start date — editing
mid-run silently converts the forward test back into an in-sample fit.

## What this does and does not test

**Does:** whether the edge persists on unseen data; the real signal rate;
operational discipline of running it daily.

**Does not:** your actual execution. Fills here are the modelled
next-bar-open, and the research showed execution is make-or-break
(measured drag 0.102%/trade on entry; at worst-in-bar fills the edge goes
negative). Stop fills assume 0.15% slippage. Real fills will differ.

**Also not modelled:** the F&O ban list and upper-circuit restrictions,
which block shorting on some names and will reduce the realisable trade
count below what this journal records. Treat the journalled trade count as
an upper bound.

## Files

| File | Purpose |
|---|---|
| `paper_config.py` | Frozen parameters + the expectation bands the report checks against |
| `engine.py` | Signal detection + trade simulation — the single source of truth, shared by `verify` and `run` |
| `journal.py` | Append-only trade log and the expectation-aware report |
| `paper.py` | CLI: `verify` / `run` / `backfill` / `report` |
| `journal.csv` | The accumulating trade record (created on first run) |
| `instrumentation.py` | Phase 2: assembles one measured record per signal — the only module `live.py` calls |
| `quote_probe.py` | Phase 2: live bid/ask/LTP/depth/circuits, and the slippage arithmetic |
| `tradeability.py` | Phase 2: NSE T2T / ASM / GSM / F&O-ban status, honest-unknown on failure |
| `execution_log.py` | Phase 2: SQLite store + daily CSV mirror, append-only and de-duped |
| `slippage_report.py` | Phase 2: tradeability %, slippage percentiles, adjusted expectancy |
| `execution_log.db` | Every qualifying signal ever seen, traded or not (created on first run) |
| `test_strategy_frozen.py` | Proof V1 is unchanged: source hashes, literal parameters, golden replay |
| `test_instrumentation.py` | Proof the instrument measures correctly, especially slippage SIGNS |
| `market_calendar.py` | NSE holiday calendar (live + cached), IST date resolution |
| `session_guard.py` | The gate: weekend + holiday + fail-closed, with exit codes |
| `test_session_guard.py` | Weekend / holiday / timezone / fail-closed tests |
| `telegram_notifier.py` | Observation-only Telegram alerts: daily summary + failure pages |
| `test_telegram_notifier.py` | Fail-safe/formatting/redaction tests, no real network call |
| `deploy/` | systemd units and VPS install notes |
| `predeploy_check.sh` | 12-check smoke suite; run it on the VPS too |
