"""
One-time (well, once-per-day) interactive login for the Fyers API v3.

Fyers access tokens expire daily (~end of trading day), and there's no
password-based grant -- you must log in through Fyers' hosted login page
each time. This script drives that flow:

  1. Builds the login URL from FYERS_APP_ID / FYERS_REDIRECT_URI.
  2. You open it in a browser, log in, and get redirected to
     FYERS_REDIRECT_URI with an `auth_code` query param (the redirect
     doesn't need to resolve to anything real -- just copy the URL from
     the address bar).
  3. Paste that URL (or just the auth_code) back here.
  4. It exchanges the code for an access token and writes/updates
     FYERS_ACCESS_TOKEN in .env.

There is also a headless path (`--auto`) that generates the 2FA code from
a TOTP secret instead of using a browser -- see fyers_totp.py, and read its
security warning before enabling it.

Usage:
  source .env  # (not required for this script itself, but keeps env consistent)
  python3 fyers_auth.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from fyers_apiv3 import fyersModel

ENV_PATH = Path(__file__).parent / ".env"


def _extract_auth_code(raw: str) -> str:
    raw = raw.strip()
    if "auth_code=" in raw or "?" in raw:
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        if "auth_code" in qs:
            return qs["auth_code"][0]
        raise ValueError(f"Could not find auth_code in pasted URL: {raw}")
    return raw


def _upsert_env_var(name: str, value: str) -> None:
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    pattern = re.compile(rf"^\s*export\s+{name}=")
    new_line = f"export {name}={value}"
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    ENV_PATH.write_text("\n".join(lines) + "\n")



def _load_env_file() -> None:
    """Read .env into os.environ so this script works without `source .env`."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip().strip('"').strip("'")


def check_token() -> bool:
    """Make one real API call to prove the saved token actually works.

    Worth doing BEFORE the market opens rather than discovering at 09:30
    that the token is stale -- the whole session is lost either way, but
    only one of them is recoverable in time.
    """
    _load_env_file()
    app_id = os.environ.get("FYERS_APP_ID")
    token = os.environ.get("FYERS_ACCESS_TOKEN")
    if not app_id or not token:
        print("  NO TOKEN -- FYERS_APP_ID or FYERS_ACCESS_TOKEN missing from .env")
        return False
    try:
        fy = fyersModel.FyersModel(client_id=app_id, is_async=False,
                                   token=token, log_path="")
        resp = fy.get_profile()
    except Exception as exc:
        print(f"  TOKEN CHECK FAILED ({type(exc).__name__}: {str(exc)[:80]})")
        return False
    if isinstance(resp, dict) and resp.get("s") == "ok":
        name = (resp.get("data") or {}).get("name", "?")
        print(f"  TOKEN OK -- authenticated as {name}")
        return True
    print(f"  TOKEN REJECTED -- {str(resp)[:120]}")
    return False


def set_token(raw: str) -> None:
    """Save a token pasted from elsewhere.

    This exists for headless machines. The interactive flow needs a
    BROWSER to complete the Fyers login, which a VPS does not have -- so
    you log in on a laptop and paste the resulting token here.

    Accepts the bare token, or `appId:token` (some Fyers tooling emits
    that form), or a redirect URL containing access_token=.
    """
    raw = raw.strip().strip('"').strip("'")
    if "access_token=" in raw:
        qs = parse_qs(urlparse(raw).query)
        raw = qs.get("access_token", [raw])[0]
    if raw.count(":") == 1 and raw.split(":")[0].isalnum() and len(raw.split(":")[0]) < 40:
        raw = raw.split(":", 1)[1]        # strip an "appId:" prefix
    if not raw or len(raw) < 20:
        sys.exit(f"That does not look like an access token (got {len(raw)} chars)")
    _upsert_env_var("FYERS_ACCESS_TOKEN", raw)
    print(f"Saved FYERS_ACCESS_TOKEN to {ENV_PATH}\n")
    check_token()


def _exchange_auth_code(auth_code: str, app_id: str, secret_id: str,
                        redirect_uri: str) -> str:
    """Swap an auth_code for an access token and write it to .env."""
    session = fyersModel.SessionModel(
        client_id=app_id,
        secret_key=secret_id,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    session.set_token(auth_code)
    response = session.generate_token()

    if response.get("s") != "ok" or "access_token" not in response:
        sys.exit(f"Token generation failed: {response}")

    access_token = response["access_token"]
    _upsert_env_var("FYERS_ACCESS_TOKEN", access_token)
    print(f"\nSaved FYERS_ACCESS_TOKEN to {ENV_PATH}")
    return access_token


def auto_login() -> None:
    """Headless refresh -- generate the 2FA code ourselves, no browser.

    The credential handling lives in fyers_totp.py, along with the warning
    about what putting a PIN and TOTP secret on disk actually costs. This
    only joins that auth_code to the normal token exchange.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import fyers_totp

    _load_env_file()
    app_id = os.environ.get("FYERS_APP_ID")
    secret_id = os.environ.get("FYERS_SECRET_ID")
    redirect_uri = os.environ.get("FYERS_REDIRECT_URI", "https://127.0.0.1")
    if not app_id or not secret_id:
        sys.exit("FYERS_APP_ID / FYERS_SECRET_ID not set in .env")

    creds = fyers_totp.load_secrets()   # exits with setup help if missing/loose
    try:
        auth_code = fyers_totp.login(
            fy_id=creds["FYERS_ID"],
            pin=creds["FYERS_PIN"],
            totp_secret=creds["FYERS_TOTP_SECRET"],
            app_id=app_id,
            redirect_uri=redirect_uri,
        )
    except RuntimeError as exc:
        # Each step already explains its own failure; a traceback only
        # buries that under frames the reader cannot act on.
        sys.exit(f"\n  headless login failed at {exc}\n")
    _exchange_auth_code(auth_code, app_id, secret_id, redirect_uri)
    check_token()


def main() -> None:
    _load_env_file()
    app_id = os.environ.get("FYERS_APP_ID")
    secret_id = os.environ.get("FYERS_SECRET_ID")
    redirect_uri = os.environ.get("FYERS_REDIRECT_URI", "https://127.0.0.1")

    if not app_id or not secret_id:
        sys.exit(
            "FYERS_APP_ID / FYERS_SECRET_ID not set. Run `source .env` first "
            "(or export them manually)."
        )

    session = fyersModel.SessionModel(
        client_id=app_id,
        secret_key=secret_id,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )

    login_url = session.generate_authcode()
    print("1. Open this URL in a browser and log in with your Fyers credentials:\n")
    print(f"   {login_url}\n")
    print(f"2. After login, you'll be redirected to {redirect_uri}?...&auth_code=...")
    print("   Copy the FULL resulting URL from the address bar (it doesn't need to load).\n")

    pasted = input("Paste the redirect URL (or just the auth_code) here: ")
    auth_code = _extract_auth_code(pasted)

    _exchange_auth_code(auth_code, app_id, secret_id, redirect_uri)
    print("Run `source .env` again to pick it up in your current shell.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--token", "-t"):
        if len(sys.argv) < 3:
            sys.exit("usage: python3 fyers_auth.py --token <ACCESS_TOKEN>")
        set_token(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] in ("--auto", "-a"):
        auto_login()
    elif len(sys.argv) > 1 and sys.argv[1] in ("--check", "-c"):
        sys.exit(0 if check_token() else 1)
    elif len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print(__doc__ or "")
        print("  python3 fyers_auth.py              full interactive login (needs a browser)")
        print("  python3 fyers_auth.py --token XXX  paste a token obtained elsewhere")
        print("  python3 fyers_auth.py --auto       headless login via TOTP (needs .secrets)")
        print("  python3 fyers_auth.py --check      verify the saved token still works")
    else:
        main()
