import os
from typing import Any

import httpx

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
        detail = data.get("error_description") or data.get("message") or data.get("error") or resp.text
    except Exception:
        detail = resp.text
    raise MyAccountError(f"My Account API {action} failed ({resp.status_code}): {detail}")


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
    return resp.json()["access_token"]


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
    return resp.json()


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
