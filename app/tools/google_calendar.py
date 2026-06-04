import datetime
import json
import os

import httpx
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TOKEN_VAULT_GRANT = (
    "urn:auth0:params:oauth:grant-type:token-exchange:federated-connection-access-token"
)
SUBJECT_TOKEN_TYPE_REFRESH = "urn:ietf:params:oauth:token-type:refresh_token"
REQUESTED_TOKEN_TYPE_FEDERATED = (
    "http://auth0.com/oauth/token-type/federated-connection-access-token"
)


class TokenVaultError(RuntimeError):
    pass


async def get_federated_access_token(
    refresh_token: str, connection: str = "google-oauth2"
) -> str:
    if not refresh_token:
        raise TokenVaultError(
            "No Auth0 refresh token in session. Log out and log in via Google to grant the Calendar scope."
        )

    domain = os.environ["AUTH0_DOMAIN"]
    body = {
        "client_id": os.environ["AUTH0_CLIENT_ID"],
        "client_secret": os.environ["AUTH0_CLIENT_SECRET"],
        "subject_token": refresh_token,
        "grant_type": TOKEN_VAULT_GRANT,
        "subject_token_type": SUBJECT_TOKEN_TYPE_REFRESH,
        "requested_token_type": REQUESTED_TOKEN_TYPE_FEDERATED,
        "connection": connection,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(f"https://{domain}/oauth/token", json=body)
    if resp.status_code >= 400:
        try:
            data = resp.json()
            detail = data.get("error_description") or data.get("error") or resp.text
        except Exception:
            detail = resp.text
        raise TokenVaultError(f"Auth0 Token Vault exchange failed ({resp.status_code}): {detail}")
    return resp.json()["access_token"]


async def list_upcoming_calendar_events(
    refresh_token: str, days: int = 7, max_results: int = 5
) -> str:
    google_access_token = await get_federated_access_token(refresh_token, "google-oauth2")

    service = build("calendar", "v3", credentials=Credentials(google_access_token))
    now = datetime.datetime.utcnow()
    events = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=now.isoformat() + "Z",
            timeMax=(now + datetime.timedelta(days=days)).isoformat() + "Z",
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )

    return json.dumps(
        [
            {
                "summary": e.get("summary", "(no title)"),
                "start": e["start"].get("dateTime", e["start"].get("date")),
                "end": e.get("end", {}).get("dateTime", e.get("end", {}).get("date")),
                "location": e.get("location"),
            }
            for e in events
        ]
    )


CALENDAR_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_upcoming_calendar_events",
        "description": (
            "List the signed-in user's upcoming Google Calendar events. "
            "Use this whenever the user asks about their calendar, schedule, "
            "meetings, or what's coming up."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Look-ahead window in days. Default 7.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of events to return. Default 5.",
                },
            },
        },
    },
}
