"""Google OAuth for the Calendar API. Used by stages 2 and 6.

One scope covers both free/busy reads and event writes. Requesting
`calendar.events` alone succeeds at consent and then 403s on free/busy, which
is a confusing failure an hour later — so this module refuses to use a cached
token whose scopes don't cover what we need, and re-runs consent instead.
"""

from __future__ import annotations

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import config

SETUP_HELP = """
Google Calendar isn't set up yet. One-time, about three minutes:

  1. https://console.cloud.google.com/projectcreate — create a project
     (or select an existing one).
  2. APIs & Services -> Library -> search "Google Calendar API" -> Enable.
  3. APIs & Services -> OAuth consent screen:
       User type: External
       Fill in app name and your email, save through to the end.
       Audience -> Test users -> ADD USERS -> add your own Google address.
       (Without this you get "app is blocked" at consent.)
  4. APIs & Services -> Credentials -> Create credentials
       -> OAuth client ID -> Application type: **Desktop app** -> Create.
  5. Download the JSON and save it as:
       {creds_path}

Then re-run. A browser window will open once to grant access; the token is
cached in {token_path} and gitignored.
"""


def _needs_reconsent(creds: Credentials | None) -> bool:
    """True if the cached token can't do what stages 2 and 6 need."""
    if creds is None:
        return True
    granted = set(creds.scopes or [])
    required = set(config.GOOGLE_SCOPES)
    if required <= granted:
        return False
    # A token granting full calendar access satisfies the narrower scopes too.
    return "https://www.googleapis.com/auth/calendar" not in granted


def credentials() -> Credentials:
    """Return usable credentials, running the consent flow if needed."""
    creds_path = Path(config.GOOGLE_CREDENTIALS_PATH)
    token_path = Path(config.GOOGLE_TOKEN_PATH)

    creds: Credentials | None = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(token_path), config.GOOGLE_SCOPES)
        except ValueError:
            creds = None

    if _needs_reconsent(creds):
        if creds is not None:
            print(f"  [auth] cached token lacks the required scope; re-consenting")
            token_path.unlink(missing_ok=True)
        creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
            return creds
        except Exception:                                  # noqa: BLE001
            # Refresh token revoked or expired — fall through to full consent.
            token_path.unlink(missing_ok=True)

    if not creds_path.exists():
        raise FileNotFoundError(
            SETUP_HELP.format(
                creds_path=creds_path.resolve(),
                token_path=token_path.resolve(),
            )
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(creds_path), config.GOOGLE_SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    token_path.write_text(creds.to_json())
    os.chmod(token_path, 0o600) if os.name != "nt" else None
    print(f"  [auth] token saved to {token_path}")
    return creds


def calendar_service():
    """An authorized Calendar API client."""
    return build("calendar", "v3", credentials=credentials(), cache_discovery=False)


if __name__ == "__main__":
    # Verifies the whole chain: credentials, scope, and a real API call that
    # needs the READ half of the scope — the one that silently 403s if the
    # consent screen only granted calendar.events.
    from datetime import timedelta

    svc = calendar_service()
    cal = svc.calendars().get(calendarId=config.GOOGLE_CALENDAR_ID).execute()
    print(f"  calendar: {cal.get('summary')} ({cal.get('timeZone')})")

    start = config.now_local()
    body = {
        "timeMin": start.isoformat(),
        "timeMax": (start + timedelta(days=7)).isoformat(),
        "timeZone": str(config.TIMEZONE),
        "items": [{"id": config.GOOGLE_CALENDAR_ID}],
    }
    fb = svc.freebusy().query(body=body).execute()
    entry = fb["calendars"][config.GOOGLE_CALENDAR_ID]
    if entry.get("errors"):
        raise SystemExit(f"free/busy returned errors: {entry['errors']}")
    print(f"  free/busy OK - {len(entry.get('busy', []))} busy blocks in the next 7 days")
    print("google_auth: read and write scopes verified")
