"""Auth0 CIBA (Client-Initiated Backchannel Authentication) helpers.

CIBA lets the app trigger a step-up auth flow that runs on the
user's enrolled authenticator (typically Auth0 Guardian push).
Use it to gate sensitive actions — admins creating/deleting orgs,
agents booking/cancelling trips.

Tenant config required:
- Auth0 Dashboard -> Applications -> {your app} -> Advanced Settings
  -> Grant Types -> enable "Client Initiated Backchannel Authentication
  (CIBA)".
- The signed-in user must be enrolled in MFA via Auth0 Guardian
  (push notifications). Without that, /bc-authorize returns
  user_not_eligible.

Reference:
- https://auth0.com/docs/get-started/applications/configure-client-initiated-backchannel-authentication
"""

import asyncio
import json
import os
import re
import time
from typing import Any

import httpx


CIBA_GRANT = "urn:openid:params:grant-type:ciba"
LOGIN_HINT_FORMAT = "iss_sub"
BINDING_MESSAGE_MAX = 64  # Auth0's hard cap on binding_message length


_BINDING_DISALLOWED = re.compile(r"[^A-Za-z0-9 \-_.]")


def sanitize_binding(message: str) -> str:
    """Auth0's /bc-authorize rejects binding_message values that contain
    anything outside a small allowed set ("can only contain
    alphanumerics, whitespace and characters"). The auth0-assistant0
    sample sticks to pure alphanumerics + spaces; we allow that plus
    -, _, . because they show up naturally in slugs and dates.
    Anything else gets replaced with a space, runs of whitespace are
    collapsed, and the result is hard-capped at 64 chars."""
    s = (message or "").strip()
    s = _BINDING_DISALLOWED.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > BINDING_MESSAGE_MAX:
        s = s[:BINDING_MESSAGE_MAX].rstrip()
    return s


# Backwards-compat alias — we used to only enforce the length cap.
truncate_binding = sanitize_binding

# Auth0 error codes / phrases that indicate the user has no enrolled
# push factor (Auth0 Guardian app) and therefore cannot complete CIBA.
NOT_ENROLLED_HINTS = (
    "user_not_eligible",
    "no_eligible",
    "no_push",
    "not enrolled",
    "no authenticators",
    "no authenticator",
    "no_authenticator",
)
ENROLLMENT_HINT_MSG = (
    "Looks like this user has no push authenticator enrolled in "
    "Auth0 Guardian. Set up MFA push enrollment in your tenant "
    "(Dashboard → Security → Multi-factor Authentication → enable "
    "'Push Notifications using Auth0 Guardian'), then have the user "
    "log out and back in to register a device. While iterating on "
    "the demo you can also set CIBA_REQUIRED=false in .env to "
    "bypass step-up entirely."
)


class CibaError(RuntimeError):
    pass


class CibaNotEnrolledError(CibaError):
    """Raised specifically when /bc-authorize fails because the user
    has no push factor enrolled. Has its own subclass so callers can
    surface a more actionable message than a generic CibaError."""


def is_ciba_required() -> bool:
    """When CIBA_REQUIRED is set to a falsy value, step_up() becomes a
    no-op. Useful for local demos where Guardian push isn't yet
    configured. Defaults to enabled."""
    return os.environ.get("CIBA_REQUIRED", "true").lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _domain() -> str:
    return os.environ["AUTH0_DOMAIN"]


def _client_id() -> str:
    """Client id used for CIBA bc-authorize / token exchange.

    Falls back to AUTH0_CLIENT_ID when AUTH0_CIBA_CLIENT_ID isn't set.
    Setting it to a separate Auth0 application is what lets the main
    web app drop the CIBA grant type — required when the web app's
    Organizations setting is `organization_usage: require` (Business
    Users Only login experience), since `require` and CIBA are
    mutually exclusive on a single Auth0 application.
    """
    return (
        os.environ.get("AUTH0_CIBA_CLIENT_ID")
        or os.environ["AUTH0_CLIENT_ID"]
    )


def _client_secret() -> str:
    return (
        os.environ.get("AUTH0_CIBA_CLIENT_SECRET")
        or os.environ["AUTH0_CLIENT_SECRET"]
    )


def login_hint_for_user_sub(sub: str) -> str:
    """Build the iss_sub login_hint Auth0 expects for CIBA. Auth0 uses
    this to figure out which user to push the prompt to."""
    return json.dumps(
        {
            "format": LOGIN_HINT_FORMAT,
            "iss": f"https://{_domain()}/",
            "sub": sub,
        }
    )


async def initiate_bc_authorize(
    login_hint: str,
    binding_message: str,
    *,
    scope: str = "openid profile",
    audience: str | None = None,
    requested_expiry: int = 300,
) -> dict[str, Any]:
    """Start a CIBA authentication request. Returns auth_req_id,
    expires_in, and the recommended polling interval. The user gets a
    push on their enrolled device with the binding_message shown.

    requested_expiry asks Auth0 to keep the auth_req_id alive long
    enough that a human has time to find their phone and approve.
    Tenants may cap below the request — we read response.expires_in
    when polling so we never outlive the actual auth_req."""
    body: dict[str, str] = {
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "login_hint": login_hint,
        "binding_message": sanitize_binding(binding_message),
        "scope": scope,
        "requested_expiry": str(requested_expiry),
    }
    if audience:
        body["audience"] = audience

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"https://{_domain()}/bc-authorize",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code >= 400:
        try:
            data = resp.json()
            detail = (
                data.get("error_description")
                or data.get("error")
                or resp.text
            )
        except Exception:
            detail = resp.text
        lowered = (detail or "").lower()
        if any(h in lowered for h in NOT_ENROLLED_HINTS):
            raise CibaNotEnrolledError(
                f"CIBA bc-authorize: {detail}. {ENROLLMENT_HINT_MSG}"
            )
        raise CibaError(
            f"CIBA bc-authorize failed ({resp.status_code}): {detail}"
        )
    return resp.json()


async def poll_for_token(
    auth_req_id: str,
    *,
    max_seconds: int = 30,
    interval: int = 2,
) -> dict[str, Any]:
    """Poll /oauth/token for the result of a CIBA request. Returns the
    token set on approval. Raises CibaError on denial, expiry, or if
    the deadline elapses without a decision."""
    deadline = time.time() + max_seconds
    current_interval = max(1, interval)

    while True:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"https://{_domain()}/oauth/token",
                data={
                    "grant_type": CIBA_GRANT,
                    "auth_req_id": auth_req_id,
                    "client_id": _client_id(),
                    "client_secret": _client_secret(),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code < 400:
            return resp.json()

        try:
            err = resp.json()
        except Exception:
            err = {"error": resp.text}
        code = err.get("error", "")

        if code in ("authorization_pending", "slow_down"):
            if code == "slow_down":
                current_interval += 1
            if time.time() + current_interval > deadline:
                raise CibaError(
                    f"Polling timed out after {max_seconds}s without an "
                    "approve/deny on the device."
                )
            await asyncio.sleep(current_interval)
            continue

        # Terminal: access_denied / expired_token / invalid_request / etc.
        if code == "access_denied":
            raise CibaError("User denied the request on their device.")
        if code == "expired_token":
            raise CibaError(
                "auth_req_id expired before it was approved — Auth0 push "
                "may not have been received, or the user took too long."
            )
        raise CibaError(
            f"CIBA token exchange failed: {code} — "
            f"{err.get('error_description', '')}"
        )


async def step_up(
    user_sub: str,
    binding_message: str,
    *,
    audience: str | None = None,
    max_seconds: int = 180,
) -> dict[str, Any]:
    """One-shot step-up: initiate the CIBA request, then poll until
    the user approves on their device. Returns the resulting token
    set; raises CibaError on any failure.

    Skipped (returns {"bypassed": True}) when CIBA_REQUIRED env var is
    set to a falsy value — useful when iterating on the demo without
    having Guardian push set up yet."""
    if not is_ciba_required():
        return {"bypassed": True}
    if not user_sub:
        raise CibaError("missing user sub for CIBA step-up")
    init = await initiate_bc_authorize(
        login_hint=login_hint_for_user_sub(user_sub),
        binding_message=binding_message,
        audience=audience,
    )
    # Cap the polling deadline by the auth_req's actual expires_in,
    # minus a small safety margin, so we never poll past the point
    # where Auth0 would just return expired_token.
    expires_in = int(init.get("expires_in") or 300)
    poll_for = min(max_seconds, max(30, expires_in - 5))
    return await poll_for_token(
        init["auth_req_id"],
        max_seconds=poll_for,
        interval=int(init.get("interval", 2)),
    )
