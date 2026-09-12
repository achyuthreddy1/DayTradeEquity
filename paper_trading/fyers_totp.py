"""
Headless Fyers login via TOTP -- no browser required.

WHY: the normal OAuth flow needs a browser to render the Fyers login page
and catch the redirect. A VPS has neither, so the token has to be pasted
in by hand every day. This flow generates the 2FA code itself and walks
the login endpoints directly, so an unattended machine can refresh its
own token.

!!! SECURITY -- READ BEFORE USING !!!
This requires your Fyers ID, your PIN, and your TOTP secret to sit on the
machine. That combination is DURABLE, FULL ACCESS to your broker account:
anything that can read those files can log in as you and place orders.
Contrast a plain access token, which is bad to leak but expires the same
evening.

Mitigations, in order of importance:
  1. Keep these three values in a SEPARATE file from the rest of the
     config -- `.secrets` here -- so that ordinary `.env` handling (copies,
     backups, pasting into a chat) cannot leak them by accident.
  2. chmod 600 that file. `fyers_auth.py --auto` refuses to run if the
     permissions are looser than that.
  3. Do not enable this on a machine you do not control.
For a PAPER trading test, which never places an order, consider whether
avoiding a 30-second daily paste is worth taking on that risk at all.

TOTP is implemented here against RFC 6238 with the standard library
(hmac/struct/base64) instead of adding pyotp -- one less dependency on a
small VPS, and it is ~10 lines. `--selftest` checks it against the RFC's
published test vectors.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

SECRETS_PATH = Path(__file__).resolve().parent / ".secrets"

SEND_OTP = "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2"
VERIFY_OTP = "https://api-t2.fyers.in/vagator/v2/verify_otp"
VERIFY_PIN = "https://api-t2.fyers.in/vagator/v2/verify_pin_v2"
# The login/2FA steps still live on the old vagator host, but the authorize
# step moved to the v3 base: the v2 one now answers -410 "deprecated
# endpoint". Do not "tidy" these onto one base URL -- they differ.
TOKEN_URL = "https://api-t1.fyers.in/api/v3/token"

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json",
}


def generate_totp(secret: str, at: int | None = None, digits: int = 6,
                  period: int = 30) -> str:
    """RFC 6238 TOTP. `secret` is the base32 key Fyers shows with the QR."""
    key = secret.strip().replace(" ", "").upper()
    key = base64.b32decode(key + "=" * ((8 - len(key) % 8) % 8))
    counter = int((at if at is not None else time.time()) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def _b64(s: str) -> str:
    return base64.b64encode(str(s).encode()).decode()


def _post(url: str, payload: dict, token: str | None = None) -> requests.Response:
    h = dict(HEADERS)
    if token:
        h["Authorization"] = f"Bearer {token}"
    return requests.post(url, json=payload, headers=h, timeout=20)


def _raise_if_throttled(j: dict, step: str) -> None:
    """A 429 is not a credential problem, and must not be reported as one.

    Every step below explains its failure in terms of a wrong id/secret/PIN,
    which sends you auditing .secrets when the real answer is to wait.
    """
    code = str(j.get("code", ""))
    msg = str(j.get("message", "")).lower()
    if code == "429" or "too many requests" in msg:
        raise RuntimeError(
            f"{step} failed: RATE LIMITED by Fyers -- {j.get('message', j)}\n"
            "     -> This is NOT a credential problem. Fyers throttles repeated\n"
            "        login attempts; wait a few minutes and run it again.")


def login(fy_id: str, pin: str, totp_secret: str, app_id: str,
          redirect_uri: str, verbose: bool = True) -> str:
    """Walk the login endpoints and return an auth_code.

    Each step surfaces the server's own message on failure rather than a
    generic error, because the failures are all distinguishable and
    actionable: a wrong TOTP secret, a wrong PIN and an un-approved app
    need three different fixes.
    """
    def say(msg):
        if verbose:
            print(f"  {msg}")

    r = _post(SEND_OTP, {"fy_id": _b64(fy_id), "app_id": "2"})
    j = r.json() if r.content else {}
    _raise_if_throttled(j, "step 1 (send OTP)")
    if "request_key" not in j:
        raise RuntimeError(
            f"step 1 (send OTP) failed: {json.dumps(j)[:200]}\n"
            "     -> check FYERS_ID is your login ID (e.g. XY12345), not the app id")
    say("step 1/4  OTP requested")

    otp = generate_totp(totp_secret)
    r = _post(VERIFY_OTP, {"request_key": j["request_key"], "otp": otp})
    j = r.json() if r.content else {}
    _raise_if_throttled(j, "step 2 (verify TOTP)")
    if "request_key" not in j:
        raise RuntimeError(
            f"step 2 (verify TOTP) failed: {json.dumps(j)[:200]}\n"
            "     -> the TOTP secret is wrong, or this machine's CLOCK IS OFF.\n"
            "        TOTP is time-based; more than ~30s of drift breaks it.")
    say(f"step 2/4  TOTP {otp} accepted")

    r = _post(VERIFY_PIN, {"request_key": j["request_key"],
                           "identity_type": "pin", "identifier": _b64(pin)})
    j = r.json() if r.content else {}
    _raise_if_throttled(j, "step 3 (verify PIN)")
    tok = (j.get("data") or {}).get("access_token")
    if not tok:
        raise RuntimeError(
            f"step 3 (verify PIN) failed: {json.dumps(j)[:200]}\n"
            "     -> check FYERS_PIN")
    say("step 3/4  PIN accepted")

    # app_id here is the client id WITHOUT its "-100" suffix; appType is
    # that suffix. FYERS_APP_ID is stored in the combined form.
    base_app, _, app_type = app_id.partition("-")
    r = _post(TOKEN_URL, {
        "fyers_id": fy_id, "app_id": base_app, "redirect_uri": redirect_uri,
        "appType": app_type or "100", "code_challenge": "", "state": "None",
        "scope": "", "nonce": "", "response_type": "code", "create_cookie": True,
    }, token=tok)
    j = r.json() if r.content else {}
    _raise_if_throttled(j, "step 4 (authorize)")
    url = j.get("Url") or j.get("url")
    if not url:
        raise RuntimeError(
            f"step 4 (authorize) failed: {json.dumps(j)[:200]}\n"
            "     -> if this is a NEW app, approve it ONCE in a browser first:\n"
            "        run `python3 fyers_auth.py` on a desktop and complete the login.")
    codes = parse_qs(urlparse(url).query).get("auth_code")
    if not codes:
        raise RuntimeError(f"no auth_code in redirect: {url[:200]}")
    say("step 4/4  auth_code received")
    return codes[0]


def load_secrets() -> dict:
    """Read .secrets, refusing to proceed if it is world/group readable."""
    if not SECRETS_PATH.exists():
        raise SystemExit(
            f"\n  {SECRETS_PATH} not found.\n\n"
            "  Create it with these three lines, then chmod 600 it:\n\n"
            "      FYERS_ID=XY12345          # your Fyers LOGIN id\n"
            "      FYERS_PIN=1234            # your 4-digit PIN\n"
            "      FYERS_TOTP_SECRET=ABCD…   # the base32 key behind the QR code\n\n"
            "  Enable TOTP first at https://myaccount.fyers.in/ManageAccount\n"
            "  ('login via External 2FA TOTP') and copy the KEY, not the QR image.\n\n"
            "      chmod 600 .secrets\n")
    mode = SECRETS_PATH.stat().st_mode & 0o077
    if mode:
        raise SystemExit(
            f"\n  {SECRETS_PATH} is readable by other users "
            f"(mode {oct(SECRETS_PATH.stat().st_mode)[-3:]}).\n"
            "  It holds durable access to your broker account. Fix it:\n\n"
            f"      chmod 600 {SECRETS_PATH.name}\n")
    out = {}
    for line in SECRETS_PATH.read_text().splitlines():
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    # An unfilled placeholder (`FYERS_ID=`) is missing, not present-but-blank:
    # without this the login walks to the live endpoint with an empty id and
    # comes back with a rate limit instead of a usable error.
    out = {k: v for k, v in out.items() if v}
    missing = {"FYERS_ID", "FYERS_PIN", "FYERS_TOTP_SECRET"} - set(out)
    if missing:
        raise SystemExit(f"\n  {SECRETS_PATH} is missing: {', '.join(sorted(missing))}\n")
    return out


def selftest() -> bool:
    """RFC 6238 published test vectors (SHA-1, 8-digit; we take 6)."""
    secret = base64.b32encode(b"12345678901234567890").decode()
    cases = [(59, "94287082"), (1111111109, "07081804"), (1234567890, "89005924")]
    ok = True
    print("  TOTP against RFC 6238 test vectors:")
    for t, expect8 in cases:
        got = generate_totp(secret, at=t, digits=8)
        good = got == expect8
        ok &= good
        print(f"    {'PASS' if good else 'FAIL'}  t={t:<11} got {got}  want {expect8}")
    print(f"    {'PASS' if ok else 'FAIL'}  6-digit form at t=59: "
          f"{generate_totp(secret, at=59)} (expect 287082)")
    ok &= generate_totp(secret, at=59) == "287082"
    return ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    if "--code" in sys.argv:          # print the current code, to compare
        s = load_secrets()            # against your authenticator app
        print(generate_totp(s["FYERS_TOTP_SECRET"]))
        sys.exit(0)
    print(__doc__)
