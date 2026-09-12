# VPS deployment (Oracle)

    systemd timer
        |
        v
    session_guard.py      weekend + NSE holiday + fail-closed
        |
        v
    live.py               unchanged trading path
        |
        v
    engine.py / paper_config.py    FROZEN

## Install

    sudo cp *.service *.timer /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now paper-trading-token.timer paper-trading.timer

Edit `User=`, `WorkingDirectory=` and the two absolute paths in the unit
files first if the repo does not live at the same path on the VPS.

## Seed the holiday calendar before the first run

    ../.venv/bin/python market_calendar.py --refresh

If the VPS cannot reach NSE, the guard reports UNKNOWN and refuses to trade
(exit 12) rather than guessing. Copy `.nse_status/nse-holidays-*.json` from a
machine that can reach NSE if that happens.

## Exit codes

| Code | Meaning | systemd treats as |
|---|---|---|
| 0 | Market open, session ran | success |
| 10 | Weekend | success (`SuccessExitStatus`) |
| 11 | NSE holiday | success (`SuccessExitStatus`) |
| 12 | Calendar unknown — **did not trade** | **FAILURE, alerts** |
| 13 | Market open but live.py failed | FAILURE, alerts |

## Why Restart=no

A session runs once per day and ends at 15:15 IST. With `Restart=always` a
holiday's immediate clean exit becomes an infinite restart loop hammering the
NSE and Fyers APIs all day. `Type=oneshot` also means systemd will not start a
second instance while one is running, so there are no duplicate processes.

## Checks

    systemctl list-timers 'paper-trading*'
    journalctl -u paper-trading.service -n 50
    ./predeploy_check.sh            # run this ON the VPS, not just locally
