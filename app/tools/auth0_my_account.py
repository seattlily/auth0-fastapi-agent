# Auth0 My Account API — Connected Accounts
#
# Audience: https://{AUTH0_DOMAIN}/me/
#
# IMPORTANT — why a separate OAuth code flow is required:
# When AUTH0_AUDIENCE is set to a custom API (e.g. https://compasszero-api),
# Auth0 audience-locks the app's refresh token at login. Exchanging that
# refresh token for audience=.../me/ silently returns the original token:
# aud stays on the custom API, connected_accounts scopes are dropped, and
# the My Account API responds with HTTP 401 "Invalid Token".
#
# The fix is a dedicated code flow in main.py:
#   GET /connections/authorize  →  Auth0 /authorize?audience=.../me/
#   GET /connections/ma-callback → exchange_code_for_ma_token() here
#   token stored in request.session["ma_access_token"] (Starlette cookie session)
#
# Required Auth0 dashboard config (see AUTH0_SETUP.md §3):
#   - My Account API activated (APIs → "Activate My Account API" banner)
#   - App authorized with create/read/delete:me:connected_accounts scopes
#   - http://localhost:8000/connections/ma-callback in Allowed Callback URLs
#   - http://localhost:8000/connections/callback in Allowed Callback URLs

import base64
import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CONNECTED_ACCOUNTS_SCOPES = (
    "openid profile offline_access "
    "create:me:connected_accounts "
    "read:me:connected_accounts "
    "delete:me:connected_accounts"
)


class MyAccountError(RuntimeError):
    pass


def _domain() -> str:
    return os.environ["AUTH0_DOMAIN"]


def _audience() -> str:
    return f"https://{_domain()}/me/"


def _api_base() -> str:
    return f"https://{_domain()}/me/v1/connected-accounts"


def _raise_for_status(resp: httpx.Response, action: str) -> None:
    if resp.status_code < 400:
        return
    try:
        data = resp.json()
        detail = (
            data.get("error_description")
            or data.get("message")
            or data.get("detail")
            or data.get("error")
            or resp.text
        )
    except Exception:
        detail = resp.text
    logger.error("My Account API %s failed (%s): %s", action, resp.status_code, resp.text)
    raise MyAccountError(f"My Account API {action} failed ({resp.status_code}): {detail}")


async def exchange_code_for_ma_token(code: str, redirect_uri: str) -> str:
    """Exchange an authorization code (from a /me-audience auth flow) for a My Account token."""
    body = {
        "grant_type": "authorization_code",
        "client_id": os.environ["AUTH0_CLIENT_ID"],
        "client_secret": os.environ["AUTH0_CLIENT_SECRET"],
        "code": code,
        "redirect_uri": redirect_uri,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"https://{_domain()}/oauth/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    _raise_for_status(resp, "MA code exchange")
    return resp.json()["access_token"]


async def mint_my_account_token(refresh_token: str) -> str:
    if not refresh_token:
        raise MyAccountError(
            "No refresh token in session. Log out and log in again to grant the connected-accounts scopes."
        )
    body = {
        "grant_type": "refresh_token",
        "client_id": os.environ["AUTH0_CLIENT_ID"],
        "client_secret": os.environ["AUTH0_CLIENT_SECRET"],
        "refresh_token": refresh_token,
        "audience": _audience(),
        "scope": CONNECTED_ACCOUNTS_SCOPES,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"https://{_domain()}/oauth/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    _raise_for_status(resp, "token exchange")
    token = resp.json()["access_token"]
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        logger.warning(
            "My Account token — aud=%r  scope=%r",
            claims.get("aud"),
            claims.get("scope"),
        )
    except Exception as _e:
        logger.warning("Could not decode My Account token: %s", _e)
    return token


async def initiate_connect(
    my_account_token: str,
    connection: str,
    redirect_uri: str,
    state: str,
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "connection": connection,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if scopes:
        body["scopes"] = scopes
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{_api_base()}/connect",
            json=body,
            headers={
                "Authorization": f"Bearer {my_account_token}",
                "Content-Type": "application/json",
            },
        )
    _raise_for_status(resp, "connect (initiate)")
    data = resp.json()
    logger.warning("initiate_connect response: %s", data)
    return data


async def complete_connect(
    my_account_token: str,
    auth_session: str,
    connect_code: str,
    redirect_uri: str,
) -> dict[str, Any]:
    body = {
        "auth_session": auth_session,
        "connect_code": connect_code,
        "redirect_uri": redirect_uri,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{_api_base()}/complete",
            json=body,
            headers={
                "Authorization": f"Bearer {my_account_token}",
                "Content-Type": "application/json",
            },
        )
    _raise_for_status(resp, "connect (complete)")
    return resp.json()


async def list_accounts(my_account_token: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_api_base()}/accounts",
            headers={"Authorization": f"Bearer {my_account_token}"},
        )
    _raise_for_status(resp, "list accounts")
    return resp.json().get("accounts", [])


async def delete_account(my_account_token: str, account_id: str) -> None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(
            f"{_api_base()}/accounts/{account_id}",
            headers={"Authorization": f"Bearer {my_account_token}"},
        )
    _raise_for_status(resp, "delete account")
