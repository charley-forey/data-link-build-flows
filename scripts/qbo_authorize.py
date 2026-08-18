"""One-time QuickBooks Online OAuth consent.

QuickBooks uses the authorization-code grant, which requires a human to approve
access in a browser once. There is no client-credentials path and no way to
automate this first step - which is why QBO ingestion is blocked until someone
runs this.

    python scripts/qbo_authorize.py                  # sandbox (default)
    python scripts/qbo_authorize.py --production

What it does:

    1. Opens Intuit's consent page in your browser.
    2. Listens on http://localhost:8080/callback for the redirect.
    3. Exchanges the authorization code for tokens.
    4. Prints the refresh token and realm id, and offers to append them to .env.

WHAT TO DO WITH THE OUTPUT

    The refresh token ROTATES on every use and hard-expires at 100 days. The
    value printed here is only the SEED: after the first successful pipeline
    run, the live token lives in `dl_meta_token` in the lakehouse (or in Key
    Vault in production), and the one in .env is stale.

    That is intended. Do not "fix" a failing run by pasting this value back in
    without first checking dl_meta_token - you will invalidate the working token.

REDIRECT URI

    http://localhost:8080/callback must be registered on the app at
    https://developer.intuit.com under Keys & OAuth -> Redirect URIs. Intuit
    matches it exactly, including the trailing path and the scheme.
"""

from __future__ import annotations

import argparse
import base64
import http.server
import json
import os
import secrets
import socketserver
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform" / "lib"))

from fabric_common import get_secret, load_dotenv  # noqa: E402

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REDIRECT_URI = "http://localhost:8080/callback"
PORT = 8080

# com.intuit.quickbooks.accounting is the only scope this platform needs. Asking
# for payments or payroll as well would widen what a leaked token can reach for
# no benefit.
SCOPE = "com.intuit.quickbooks.accounting"

_result: dict[str, str] = {}


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - required by the base class
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _result.update({k: v[0] for k, v in params.items()})

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in _result
        self.wfile.write(
            (
                "<html><body style='font-family:system-ui;padding:3rem'>"
                f"<h2>{'Authorised.' if ok else 'Authorisation failed.'}</h2>"
                f"<p>{'You can close this tab and return to the terminal.' if ok else _result}</p>"
                "</body></html>"
            ).encode("utf-8")
        )

    def log_message(self, *args: object) -> None:
        """Silence the default request logging - it would print the auth code."""


def wait_for_callback(timeout: int = 300) -> dict[str, str]:
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("localhost", PORT), CallbackHandler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        deadline = threading.Event()
        while not _result and not deadline.wait(0.5):
            timeout -= 0.5
            if timeout <= 0:
                break
        httpd.shutdown()
    return _result


def exchange(code: str, client_id: str, client_secret: str) -> dict:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    body = urllib.parse.urlencode(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
            "x-include-refresh-token-hard-expires-in": "true",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def append_env(refresh_token: str, realm_id: str, environment: str) -> None:
    env_path = ROOT.parent / ".env" if (ROOT.parent / ".env").exists() else ROOT / ".env"
    lines = [
        "",
        "# Written by scripts/qbo_authorize.py. The refresh token ROTATES on every",
        "# use - after the first pipeline run the live value is in dl_meta_token,",
        "# and this one is a stale seed. See docs/05-runbook.md.",
        f"QUICKBOOKS_REFRESH_TOKEN={refresh_token}",
        f"QUICKBOOKS_REALM_ID={realm_id}",
        f"QUICKBOOKS_ENVIRONMENT={environment}",
        "",
    ]
    with open(env_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    print(f"\nAppended to {env_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--production",
        action="store_true",
        help="authorise the production company rather than a sandbox",
    )
    parser.add_argument("--no-write", action="store_true", help="print only, do not touch .env")
    args = parser.parse_args()
    environment = "production" if args.production else "sandbox"

    load_dotenv(str(ROOT / ".env"))
    load_dotenv(str(ROOT.parent / ".env"))

    try:
        client_id = get_secret("QUICKBOOKS_CLIENT_ID")
        client_secret = get_secret("QUICKBOOKS_CLIENT_SECRET")
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    state = secrets.token_urlsafe(16)
    url = f"{AUTH_URL}?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": SCOPE,
            "redirect_uri": REDIRECT_URI,
            "state": state,
        }
    )

    print(f"Authorising QuickBooks ({environment}).")
    print(f"Redirect URI must be registered on the Intuit app: {REDIRECT_URI}\n")
    print("Opening your browser. If nothing happens, visit:\n")
    print(f"  {url}\n")
    webbrowser.open(url)

    result = wait_for_callback()
    if "code" not in result:
        print(f"error: no authorization code received ({result or 'timed out'})", file=sys.stderr)
        return 1
    # State is checked because a mismatch means the response did not come from
    # the request we made.
    if result.get("state") != state:
        print("error: state mismatch - discarding this response", file=sys.stderr)
        return 1

    realm_id = result.get("realmId", "")
    tokens = exchange(result["code"], client_id, client_secret)

    refresh_token = tokens["refresh_token"]
    refresh_days = int(tokens.get("x_refresh_token_expires_in", 0)) / 86400

    print("\nAuthorised.")
    print(f"  realm id            {realm_id}")
    print(f"  environment         {environment}")
    print(f"  refresh token       {refresh_token[:12]}...{refresh_token[-6:]}")
    print(f"  refresh expires in  {refresh_days:.0f} days")
    print(
        "\nReminder: this token rotates on every use. After the first pipeline run\n"
        "the live value is in dl_meta_token, not here."
    )

    if not args.no_write:
        append_env(refresh_token, realm_id, environment)
        print("\nNext: run dl_02_extract_qbo in Fabric, or set the same values in Key Vault.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
