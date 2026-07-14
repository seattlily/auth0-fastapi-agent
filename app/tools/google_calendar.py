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


async def get_google_account_email(access_token: str) -> str | None:
    """Return the email of the Google account that owns this access token."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code == 200:
        return resp.json().get("email")
    return None


def _calendar_link_for_account(html_link: str | None, email: str | None) -> str | None:
    """Append authuser=<email> to a Google Calendar htmlLink so it opens
    in the Token Vault connected account rather than the browser default."""
    if not html_link:
        return html_link
    if not email:
        return html_link
    sep = "&" if "?" in html_link else "?"
    return f"{html_link}{sep}authuser={email}"


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


async def create_calendar_event(
    refresh_token: str,
    summary: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    attendees: list[str] | None = None,
) -> str:
    google_access_token = await get_federated_access_token(refresh_token, "google-oauth2")
    service = build("calendar", "v3", credentials=Credentials(google_access_token))

    event: dict = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if description:
        event["description"] = description
    if location:
        event["location"] = location
    if attendees:
        event["attendees"] = [{"email": e} for e in attendees]

    created = (
        service.events()
        .insert(calendarId="primary", body=event, sendUpdates="none")
        .execute()
    )

    google_email = await get_google_account_email(google_access_token)
    html_link = _calendar_link_for_account(created.get("htmlLink"), google_email)

    return json.dumps(
        {
            "id": created.get("id"),
            "htmlLink": html_link,
            "summary": created.get("summary"),
            "start": created.get("start"),
            "end": created.get("end"),
            "location": created.get("location"),
            "calendar_account": google_email,
        }
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


CREATE_CALENDAR_EVENT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_calendar_event",
        "description": (
            "Create a new event on the user's primary Google Calendar. "
            "Use whenever the user asks to schedule, add, book, or put "
            "something on their calendar. Always confirm the start/end "
            "with the user if not given explicitly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Event title (e.g., 'Lunch with Alex').",
                },
                "start": {
                    "type": "string",
                    "description": (
                        "Event start time in RFC3339 format with timezone "
                        "offset, e.g. '2026-06-05T15:00:00-07:00'."
                    ),
                },
                "end": {
                    "type": "string",
                    "description": (
                        "Event end time in RFC3339 format with timezone "
                        "offset, e.g. '2026-06-05T16:00:00-07:00'."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Optional notes / agenda for the event.",
                },
                "location": {
                    "type": "string",
                    "description": "Optional location string.",
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of attendee email addresses.",
                },
            },
            "required": ["summary", "start", "end"],
        },
    },
}
