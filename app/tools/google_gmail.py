import json

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .google_calendar import get_federated_access_token


async def list_recent_emails(
    refresh_token: str,
    max_results: int = 5,
    query: str = "",
) -> str:
    google_access_token = await get_federated_access_token(refresh_token, "google-oauth2")
    service = build("gmail", "v1", credentials=Credentials(google_access_token))

    list_resp = (
        service.users()
        .messages()
        .list(
            userId="me",
            maxResults=max(1, min(max_results, 25)),
            q=query or "in:inbox",
        )
        .execute()
    )

    messages = list_resp.get("messages", []) or []
    out: list[dict] = []
    for m in messages:
        full = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=m["id"],
                format="metadata",
                metadataHeaders=["Subject", "From", "Date", "To"],
            )
            .execute()
        )
        headers = {
            h["name"]: h["value"]
            for h in full.get("payload", {}).get("headers", []) or []
        }
        out.append(
            {
                "id": full.get("id"),
                "thread_id": full.get("threadId"),
                "subject": headers.get("Subject", "(no subject)"),
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "date": headers.get("Date", ""),
                "snippet": full.get("snippet", ""),
            }
        )

    return json.dumps(out)


GMAIL_LIST_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_recent_emails",
        "description": (
            "List the user's most recent Gmail messages (subject, sender, "
            "date, snippet). Use whenever the user asks about their email, "
            "inbox, recent messages, or 'what's my last email'. Snippets "
            "are short previews; if the user wants the full body, say so."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Number of messages to return. Default 5, max 25.",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Optional Gmail search query (same syntax as the "
                        "Gmail UI search box, e.g. 'from:alice@example.com', "
                        "'is:unread', 'subject:invoice newer_than:7d'). "
                        "Defaults to 'in:inbox'."
                    ),
                },
            },
        },
    },
}
