"""
Telegram notifications -- observability only, same guarantee as
instrumentation.py: it observes, it never votes.

Isolated from all trading logic by design: this module can do exactly one
thing -- send an outbound HTTP POST to Telegram's Bot API -- and nothing
else. It has no access to the engine, the journal, or the live scan loop
beyond the plain strings it is handed, and there is no code path from here
back into signal detection, allocation, or the simulated fill. engine.py,
paper_config.py and journal.py must never import this module (the same
isolation `test_strategy_frozen.py` already enforces for the Phase 2
instrumentation modules).

FAILURE SEMANTICS: every failure mode here is caught and swallowed (printed,
never raised) -- a Telegram outage, bad token, or network error can never
turn a completed live session into a failed one, and can never delay or
block the journal write that already happened before notify() is called.
Callers get a plain bool back and are expected to treat False as "print and
move on", never as "undo anything."

SECRETS: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are read from
`paper_trading/.env` (or the project root's .env), the same
`export KEY=value` mechanism market_data.load_parent_env() already uses for
the Fyers credentials. Neither is ever printed, included in an exception
message, or returned by any method here -- the token only ever appears
inside the request URL handed directly to urllib.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

TELEGRAM_API_BASE = "https://api.telegram.org"

HERE = Path(__file__).resolve().parent


def _load_env() -> None:
    """Same minimal .env parser as market_data.load_parent_env(): checks
    this directory then the project root, supports an `export ` prefix,
    and never overwrites a variable already set in the real environment."""
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


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class TelegramNotifier:
    """Thin, fail-safe wrapper around Telegram's sendMessage endpoint."""

    def __init__(self, enabled: bool, bot_token: str | None = None,
                 chat_id: str | None = None, timeout: float = 10.0):
        self._configured = bool(bot_token) and bool(chat_id)
        self.enabled = bool(enabled) and self._configured
        if enabled and not self._configured:
            print("  [telegram] TELEGRAM_ENABLED is true but bot token and/or chat id "
                  "are missing -- notifications disabled until both are configured.")
        self._bot_token = bot_token
        self._chat_id = chat_id
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        """Read TELEGRAM_ENABLED / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from
        .env (see _load_env) and build a notifier. This project has no
        central Config object (paper_config.py is deliberately frozen
        strategy-only), so this is the one place Telegram settings live."""
        _load_env()
        enabled = _parse_bool(os.environ.get("TELEGRAM_ENABLED"), default=False)
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or None
        chat_id = os.environ.get("TELEGRAM_CHAT_ID") or None
        return cls(enabled=enabled, bot_token=bot_token, chat_id=chat_id)

    def _redact(self, text: str) -> str:
        """Defense-in-depth: strips the bot token and chat id out of
        whatever string is about to be printed. Under normal operation
        neither ever appears in urllib's own exception text -- this exists
        so that remains true even if some future urllib/Telegram edge case
        embeds the request URL (which contains the token) in an error."""
        if not isinstance(text, str):
            text = str(text)
        if self._bot_token:
            text = text.replace(self._bot_token, "<redacted>")
        if self._chat_id:
            text = text.replace(str(self._chat_id), "<redacted>")
        return text

    def notify(self, message: str) -> bool:
        """Best-effort send. Returns True only on a confirmed Telegram
        delivery ("ok": true in the response body); False for disabled,
        misconfigured, or any failure -- this method never raises."""
        if not self.enabled:
            return False

        url = f"{TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"
        payload = json.dumps({"chat_id": self._chat_id, "text": message}).encode("utf-8")
        request = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"  [telegram] notification failed: HTTP {e.code} {e.reason}")
            return False
        except urllib.error.URLError as e:
            print(self._redact(f"  [telegram] notification failed: {e.reason}"))
            return False
        except Exception as e:
            print(self._redact(f"  [telegram] notification failed: {type(e).__name__}: {e}"))
            return False

        if not body.get("ok"):
            print(self._redact(f"  [telegram] API reported failure: "
                                f"{body.get('description', '<no description>')}"))
            return False

        return True


def format_session_message(day: date, signal_count: int, chosen_count: int, trades: list) -> str:
    """Human-readable end-of-day summary from ACTUAL Trade records only
    (engine.Trade instances, the same objects journal.append() persists) --
    never invents or estimates a number that isn't on the object it's given.
    """
    date_str = day.strftime("%d-%b-%Y") if hasattr(day, "strftime") else str(day)

    if not trades:
        if signal_count == 0:
            lines = ["⚪ PAPER TRADING -- no trade", "", f"\U0001F4C5 {date_str}",
                      "No setup cleared the relative-volume filter.", "", "Mode: PAPER"]
        else:
            lines = ["⚪ PAPER TRADING -- no trade", "", f"\U0001F4C5 {date_str}",
                      f"{signal_count} qualifying signal(s), {chosen_count} selected, "
                      "none produced a fillable trade.", "", "Mode: PAPER"]
        return "\n".join(lines)

    total_rupees = sum(t.net_rupees for t in trades)
    emoji = "\U0001F7E2" if total_rupees >= 0 else "\U0001F534"

    lines = [f"{emoji} PAPER TRADING SESSION", "", f"\U0001F4C5 {date_str}",
              f"{signal_count} qualifying signal(s), {len(trades)} traded", ""]
    for t in trades:
        lines.append(f"SHORT {t.symbol}  {t.shares} sh @{t.entry_price:.2f} -> "
                      f"{t.exit_price:.2f}  ({t.exit_reason})")
        lines.append(f"  {t.net_pct:+.2f}%   Rs{t.net_rupees:+,.0f}")
    lines.append("")
    lines.append(f"\U0001F4B0 Day P&L: Rs{total_rupees:+,.0f}")
    lines.append("")
    lines.append("Mode: PAPER")
    return "\n".join(lines)


def format_alert_message(subject: str, detail: str) -> str:
    """A human needs to look at this -- an infra/data failure, not a normal
    trading outcome (weekend/holiday are NOT alerts; see format_no_trade_message)."""
    lines = ["\U0001F6A8 PAPER TRADING ALERT", "", subject, "", detail, "", "Mode: PAPER"]
    return "\n".join(lines)


def format_no_trade_message(day: date, reason: str) -> str:
    """The market simply wasn't open -- weekend or NSE holiday. This is a
    clean, expected outcome (see session_guard.py's exit codes 10/11), not
    a failure, so it gets a plain informational message rather than the
    ALERT framing format_alert_message uses for genuine problems. Exists so
    a silent day and a dead bot don't look the same on a VPS you aren't
    watching."""
    date_str = day.strftime("%d-%b-%Y") if hasattr(day, "strftime") else str(day)
    lines = ["⚪ PAPER TRADING -- market closed", "", f"\U0001F4C5 {date_str}",
              f"No trades today -- {reason}.", "", "Mode: PAPER"]
    return "\n".join(lines)
