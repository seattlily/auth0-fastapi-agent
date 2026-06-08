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
import time
from typing import Any

import httpx


CIBA_GRANT = "urn:openid:params:grant-type:ciba"
LOGIN_HINT_FORMAT = "iss_sub"


class CibaError(RuntimeError):
    pass


def _domain() -> str:
    return os.environ["AUTH0_DOMAIN"]


def _client_id() -> str:
    return os.environ["AUTH0_CLIENT_ID"]


def _client_secret() -> str:
    return os.environ["AUTH0_CLIENT_SECRET"]


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
) -> dict[str, Any]:
    """Start a CIBA authentication request. Returns auth_req_id,
    expires_in, and the recommended polling interval. The user gets a
    push on their enrolled device with the binding_message shown."""
    body: dict[str, str] = {
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "login_hint": login_hint,
        "binding_message": binding_message,
        "scope": scope,
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
                    "CIBA approval timed out — user did not approve in time."
                )
            await asyncio.sleep(current_interval)
            continue

        # Terminal: access_denied, expired_token, invalid_request, etc.
        raise CibaError(
            f"CIBA token exchange failed: {code} — "
            f"{err.get('error_description', '')}"
        )


async def step_up(
    user_sub: str,
    binding_message: str,
    *,
    audience: str | None = None,
    max_seconds: int = 30,
) -> dict[str, Any]:
    """One-shot step-up: initiate the CIBA request, then poll until
    the user approves on their device. Returns the resulting token
    set; raises CibaError on any failure."""
    if not user_sub:
        raise CibaError("missing user sub for CIBA step-up")
    init = await initiate_bc_authorize(
        login_hint=login_hint_for_user_sub(user_sub),
        binding_message=binding_message,
        audience=audience,
    )
    return await poll_for_token(
        init["auth_req_id"],
        max_seconds=max_seconds,
        interval=int(init.get("interval", 2)),
    )
