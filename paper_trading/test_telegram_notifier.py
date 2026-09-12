"""
Telegram notifier tests: disabled/enabled behavior, correct endpoint/payload,
graceful failure handling, secret redaction, and message formatting from real
engine.Trade objects only. No real network call is ever made anywhere in
this file -- urllib.request.urlopen is monkeypatched (plain attribute swap).
No real Telegram token is used anywhere.

    python3 test_telegram_notifier.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import telegram_notifier as tn  # noqa: E402
from telegram_notifier import (  # noqa: E402
    TELEGRAM_API_BASE,
    TelegramNotifier,
    format_alert_message,
    format_no_trade_message,
    format_session_message,
)
from engine import Trade  # noqa: E402

# Obviously-fake, never-valid credentials -- used only to prove the notifier
# builds the right request; no real Telegram account is ever contacted.
FAKE_TOKEN = "123456789:FAKE-TEST-TOKEN-not-real-abcXYZ"
FAKE_CHAT_ID = "-1009999999999"

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   {detail}"))
    if not ok:
        FAILURES.append(name)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextmanager
def _patched_urlopen(fake_fn):
    original = urllib.request.urlopen
    urllib.request.urlopen = fake_fn
    try:
        yield
    finally:
        urllib.request.urlopen = original


def _trade(symbol="ABCLTD", net_pct=1.2, net_rupees=400.0, exit_reason="time") -> Trade:
    return Trade(
        symbol=symbol, day=date(2026, 9, 11), direction="short",
        entry_time="09:35", entry_price=100.0, exit_time="15:15", exit_price=98.8,
        exit_reason=exit_reason, stop_price=102.0, rel_volume=6.2, bars_held=68,
        shares=333, ticket_value=33_300.0, gross_pct=1.2, cost_pct=0.09,
        net_pct=net_pct, net_rupees=net_rupees,
    )


def test_disabled_notifier_makes_no_http_request():
    calls = []

    def fake(request, timeout=None):
        calls.append(request)
        raise AssertionError("urlopen must not be called when the notifier is disabled")

    notifier = TelegramNotifier(enabled=False, bot_token=FAKE_TOKEN, chat_id=FAKE_CHAT_ID)
    with _patched_urlopen(fake):
        sent = notifier.notify("should never be sent")

    check("disabled notifier returns False", sent is False)
    check("disabled notifier makes zero HTTP requests", calls == [])


def test_missing_credentials_disables_even_if_enabled_true():
    """TELEGRAM_ENABLED=true with no token/chat id configured must not
    attempt a request -- 'enabled' alone is not enough without a target."""
    calls = []

    def fake(request, timeout=None):
        calls.append(request)

    notifier = TelegramNotifier(enabled=True, bot_token=None, chat_id=None)
    check("missing credentials force enabled=False", notifier.enabled is False)
    with _patched_urlopen(fake):
        sent = notifier.notify("no target configured")
    check("notify() returns False with no target", sent is False)
    check("no HTTP request attempted", calls == [])


def test_successful_notification_correct_endpoint_and_payload():
    captured = []

    def fake(request, timeout=None):
        captured.append(request)
        return _FakeResponse({"ok": True, "result": {"message_id": 42}})

    notifier = TelegramNotifier(enabled=True, bot_token=FAKE_TOKEN, chat_id=FAKE_CHAT_ID)
    with _patched_urlopen(fake):
        sent = notifier.notify("hello from a test")

    check("successful notify() returns True", sent is True)
    check("exactly one HTTP request made", len(captured) == 1)
    req = captured[0]
    check("hits the correct sendMessage endpoint",
          req.full_url == f"{TELEGRAM_API_BASE}/bot{FAKE_TOKEN}/sendMessage", req.full_url)
    body = json.loads(req.data.decode("utf-8"))
    check("payload has correct chat_id/text",
          body == {"chat_id": FAKE_CHAT_ID, "text": "hello from a test"}, body)


def test_telegram_http_error_handled_gracefully():
    def fake(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", None, None)

    notifier = TelegramNotifier(enabled=True, bot_token=FAKE_TOKEN, chat_id=FAKE_CHAT_ID)
    with _patched_urlopen(fake):
        sent = notifier.notify("this will fail")

    check("HTTP error caught and returns False, not raised", sent is False)


def test_telegram_api_level_rejection_handled_gracefully():
    """Telegram can return HTTP 200 with {"ok": false} (e.g. bad chat id) --
    that must also be treated as a failed send, not a success."""
    def fake(request, timeout=None):
        return _FakeResponse({"ok": False, "description": "chat not found"})

    notifier = TelegramNotifier(enabled=True, bot_token=FAKE_TOKEN, chat_id=FAKE_CHAT_ID)
    with _patched_urlopen(fake):
        sent = notifier.notify("this will be rejected")

    check("'ok: false' response treated as failed send", sent is False)


def test_generic_network_exception_handled_gracefully():
    def fake(request, timeout=None):
        raise ConnectionResetError("simulated network drop")

    notifier = TelegramNotifier(enabled=True, bot_token=FAKE_TOKEN, chat_id=FAKE_CHAT_ID)
    with _patched_urlopen(fake):
        sent = notifier.notify("network is down")

    check("arbitrary network exception caught, never raised", sent is False)


def test_secrets_never_appear_in_printed_error_messages():
    """Worst-case simulation: the underlying exception's own message happens
    to contain the request URL (which embeds the bot token) and the chat id
    -- proves the notifier's _redact() backstop strips both before anything
    is printed, regardless of what a third-party exception contains."""
    def fake(request, timeout=None):
        raise urllib.error.URLError(f"Failed to reach {request.full_url} for chat {FAKE_CHAT_ID}")

    notifier = TelegramNotifier(enabled=True, bot_token=FAKE_TOKEN, chat_id=FAKE_CHAT_ID)
    captured = []
    import builtins
    original_print = builtins.print
    builtins.print = lambda *a, **kw: captured.append(" ".join(str(x) for x in a))
    try:
        with _patched_urlopen(fake):
            sent = notifier.notify("check secrets")
    finally:
        builtins.print = original_print

    check("notify() returns False on this failure", sent is False)
    check("something was printed for this failure", bool(captured))
    leaked = [m for m in captured if FAKE_TOKEN in m or FAKE_CHAT_ID in m]
    check("token/chat id never leak into printed output", not leaked, leaked)


def test_from_env_reads_telegram_fields(tmp_path=None):
    """from_env() must read TELEGRAM_ENABLED/BOT_TOKEN/CHAT_ID from .env via
    the same export-KEY=value mechanism market_data.load_parent_env() uses --
    proven here with a temp .env so the real project .env is never touched
    or exercised by the test suite."""
    import os
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    env_file = tmp / ".env"
    env_file.write_text(
        f"export TELEGRAM_ENABLED=true\n"
        f"export TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\n"
        f"export TELEGRAM_CHAT_ID={FAKE_CHAT_ID}\n"
    )

    original_here = tn.HERE
    saved_env = {k: os.environ.pop(k, None) for k in
                 ("TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    tn.HERE = tmp
    try:
        notifier = TelegramNotifier.from_env()
        check("from_env() reads enabled=True", notifier.enabled is True)
        check("from_env() reads bot token", notifier._bot_token == FAKE_TOKEN)
        check("from_env() reads chat id", notifier._chat_id == FAKE_CHAT_ID)
    finally:
        tn.HERE = original_here
        for k, v in saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_session_message_reflects_actual_trades_not_invented_values():
    """format_session_message() must render exactly the Trade objects it's
    given -- this is a pure formatting test, independent of the HTTP layer,
    and must never fabricate a symbol/number that wasn't passed in."""
    trades = [_trade(symbol="ABCLTD", net_pct=1.20, net_rupees=400.0, exit_reason="time"),
              _trade(symbol="XYZFIN", net_pct=-1.85, net_rupees=-616.0, exit_reason="stop")]
    message = format_session_message(date(2026, 9, 11), signal_count=5, chosen_count=2, trades=trades)

    check("date rendered", "11-Sep-2026" in message)
    check("both symbols present", "ABCLTD" in message and "XYZFIN" in message)
    check("winning trade net rupees present", "Rs+400" in message)
    check("losing trade net rupees present", "Rs-616" in message)
    check("day total P&L present", "Rs-216" in message)  # 400 - 616
    check("exit reasons present", "(time)" in message and "(stop)" in message)
    check("mode PAPER present", "Mode: PAPER" in message)
    check("no fabricated symbol", "RELIANCE" not in message)


def test_session_message_no_trades_variants():
    no_signals = format_session_message(date(2026, 9, 11), signal_count=0, chosen_count=0, trades=[])
    check("zero-signal day says nothing cleared the filter",
          "relative-volume filter" in no_signals)

    no_fill = format_session_message(date(2026, 9, 11), signal_count=3, chosen_count=2, trades=[])
    check("signals-but-no-fill day mentions the signal/chosen counts",
          "3 qualifying signal(s)" in no_fill and "2 selected" in no_fill)


def test_alert_message_contains_subject_and_detail():
    msg = format_alert_message("Session gate: calendar unknown for 2026-09-11",
                                "NSE unreachable from this host.")
    check("alert contains subject", "calendar unknown" in msg)
    check("alert contains detail", "NSE unreachable" in msg)
    check("alert is clearly marked", "ALERT" in msg)


def test_no_trade_message_weekend_and_holiday():
    weekend = format_no_trade_message(date(2026, 9, 13), "weekend (Sunday)")
    check("weekend message names the reason", "weekend (Sunday)" in weekend)
    check("weekend message is not framed as an ALERT", "ALERT" not in weekend)
    check("weekend message has the date", "13-Sep-2026" in weekend)

    holiday = format_no_trade_message(date(2026, 9, 14), "NSE holiday: Ganesh Chaturthi")
    check("holiday message names the reason", "Ganesh Chaturthi" in holiday)
    check("holiday message is not framed as an ALERT", "ALERT" not in holiday)


if __name__ == "__main__":
    test_disabled_notifier_makes_no_http_request()
    test_missing_credentials_disables_even_if_enabled_true()
    test_successful_notification_correct_endpoint_and_payload()
    test_telegram_http_error_handled_gracefully()
    test_telegram_api_level_rejection_handled_gracefully()
    test_generic_network_exception_handled_gracefully()
    test_secrets_never_appear_in_printed_error_messages()
    test_from_env_reads_telegram_fields()
    test_session_message_reflects_actual_trades_not_invented_values()
    test_session_message_no_trades_variants()
    test_alert_message_contains_subject_and_detail()
    test_no_trade_message_weekend_and_holiday()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("All telegram_notifier tests passed.")
