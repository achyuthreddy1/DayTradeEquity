#!/usr/bin/env bash
# Pre-deploy / post-deploy smoke suite.
#
# Run this ON THE VPS after deploying, not just here: the checks that matter
# most (host timezone, tz database, token, NSE reachability) are exactly the
# ones that pass locally and fail on a fresh server.
#
#   ./predeploy_check.sh
#
# Exits non-zero if anything fails, so it can gate a deploy.
cd "$(dirname "$0")" || exit 2
PY="${PY:-../.venv/bin/python}"
pass=0; fail=0

run() {
  desc="$1"; shift
  out=$("$@" 2>&1); rc=$?
  out=$(echo "$out" | grep -v RuntimeWarning)
  if [ $rc -eq 0 ]; then
    echo "  PASS  $desc"; pass=$((pass+1))
  else
    echo "  FAIL  $desc"; echo "$out" | tail -4; fail=$((fail+1))
  fi
}

echo "=============================================="
echo " PRE-DEPLOY SUITE  --  $(date)"
echo " host TZ: ${TZ:-$(cat /etc/timezone 2>/dev/null || echo unknown)}"
echo "=============================================="

run "STRATEGY V1 FROZEN (hashes + behaviour)"   $PY test_strategy_frozen.py
run "NSE session gate (weekend/holiday/TZ)"    $PY test_session_guard.py
run "Phase 2 instrumentation"                  $PY test_instrumentation.py
run "Telegram notifier (formatting + fail-safe)" $PY test_telegram_notifier.py
run "Telegram config sane (enabled implies configured)" $PY -c "
import os, telegram_notifier as tn
tn._load_env()
requested = tn._parse_bool(os.environ.get('TELEGRAM_ENABLED'), default=False)
n = tn.TelegramNotifier.from_env()
if requested:
    assert n.enabled, 'TELEGRAM_ENABLED=true but bot token/chat id missing -- alerts are silently OFF'
print(f'  requested={requested} effective={n.enabled} configured={n._configured}')
"
run "engine selftest (host timezone)"          $PY paper.py selftest
run "engine selftest (forced UTC host)"        env TZ=UTC $PY paper.py selftest
run "TOTP vs RFC 6238 vectors"                 $PY fyers_totp.py --selftest
run "live Fyers token"                         $PY fyers_auth.py --check
run "universe fetch from NSE (100 midcaps)"    $PY -c "from market_data import liquid_universe; u=liquid_universe(); assert len(u)==100, len(u)"
run "journal integrity (no dup symbol/day)"    $PY -c "
import csv, pathlib
p = pathlib.Path('journal.csv')
if not p.exists():
    print('  no journal yet -- fine before the first session'); raise SystemExit(0)
rows = list(csv.DictReader(p.open()))
keys = {(r['symbol'], r['day']) for r in rows}
assert len(keys) == len(rows), 'duplicate (symbol,day) in journal'
print(f'  {len(rows)} row(s), no duplicates')
"
run "IST clock survives a UTC host"            env TZ=UTC $PY -c "
from paper_config import now_ist
from datetime import datetime, timezone
d = abs((now_ist() - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds() - 5.5*3600)
assert d < 90, f'clock is {d}s off IST'
print('  now_ist() correct despite TZ=UTC')
"
run "tz database resolvable"                   $PY -c "
from zoneinfo import ZoneInfo
ZoneInfo('Asia/Kolkata'); print('  Asia/Kolkata resolves')
"
run "NSE holiday calendar is seeded"           $PY -c "
import market_calendar as mc
from datetime import date
h, src = mc.holidays_for(date.today().year, allow_network=False)
assert h, 'no cached calendar -- run: python3 market_calendar.py --refresh'
print(f'  {len(h)} holidays cached for {date.today().year} [{src}]')
"

echo "=============================================="
echo "  $pass passed, $fail failed"
echo "=============================================="
[ $fail -eq 0 ]
