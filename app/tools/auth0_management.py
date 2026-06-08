"""Auth0 Management API client — used for admin actions like creating
organizations and listing org members.

Authentication uses the client_credentials grant against the
Management API audience. By default it reuses the app's own
AUTH0_CLIENT_ID / AUTH0_CLIENT_SECRET; if you'd rather use a
dedicated M2M app, set AUTH0_MGMT_CLIENT_ID and
AUTH0_MGMT_CLIENT_SECRET.

Required scopes on the M2M grant: `create:organizations`,
`read:organizations`, `read:organization_members`. Authorize them
under Auth0 Dashboard → APIs → Auth0 Management API → Machine to
Machine Applications → {your app}.
"""

import asyncio
import os
import time
from typing import Any

import httpx

from mock_data import COMPANIES, add_company


class ManagementError(RuntimeError):
    pass


_token_cache: dict[str, Any] = {"token": "", "expires_at": 0.0}


def _domain() -> str:
    return os.environ["AUTH0_DOMAIN"]


def _audience() -> str:
    return f"https://{_domain()}/api/v2/"


def _api_base() -> str:
    return f"https://{_domain()}/api/v2"


def _client_credentials() -> tuple[str, str]:
    return (
        os.environ.get("AUTH0_MGMT_CLIENT_ID") or os.environ["AUTH0_CLIENT_ID"],
        os.environ.get("AUTH0_MGMT_CLIENT_SECRET") or os.environ["AUTH0_CLIENT_SECRET"],
    )


def _raise_for_status(resp: httpx.Response, action: str) -> None:
    if resp.status_code < 400:
        return
    try:
        data = resp.json()
        detail = (
            data.get("message")
            or data.get("error_description")
            or data.get("error")
            or resp.text
        )
    except Exception:
        detail = resp.text
    raise ManagementError(
        f"Auth0 Management API {action} failed ({resp.status_code}): {detail}"
    )


async def _get_management_token() -> str:
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 30:
        return _token_cache["token"]

    client_id, client_secret = _client_credentials()
    body = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "audience": _audience(),
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"https://{_domain()}/oauth/token",
            json=body,
            headers={"Content-Type": "application/json"},
        )
    _raise_for_status(resp, "token exchange")
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return data["access_token"]


async def create_organization(
    name: str, display_name: str, metadata: dict | None = None
) -> dict:
    token = await _get_management_token()
    body: dict[str, Any] = {"name": name, "display_name": display_name}
    if metadata:
        body["metadata"] = metadata
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{_api_base()}/organizations",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    _raise_for_status(resp, "create organization")
    return resp.json()


async def list_organizations() -> list[dict]:
    token = await _get_management_token()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_api_base()}/organizations",
            headers={"Authorization": f"Bearer {token}"},
        )
    _raise_for_status(resp, "list organizations")
    data = resp.json()
    if isinstance(data, dict):
        return data.get("organizations") or []
    return data


async def get_organization_by_name(name: str) -> dict | None:
    token = await _get_management_token()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_api_base()}/organizations/name/{name}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code == 404:
        return None
    _raise_for_status(resp, "get organization by name")
    return resp.json()


async def delete_organization(org_id: str) -> None:
    token = await _get_management_token()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(
            f"{_api_base()}/organizations/{org_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code in (204, 200):
        return
    _raise_for_status(resp, "delete organization")


async def list_organization_members(org_id: str) -> list[dict]:
    token = await _get_management_token()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_api_base()}/organizations/{org_id}/members",
            headers={"Authorization": f"Bearer {token}"},
        )
    _raise_for_status(resp, "list organization members")
    data = resp.json()
    if isinstance(data, dict):
        return data.get("members") or []
    return data


# ---------- local <-> Auth0 sync ----------

SYNC_TTL_SECONDS = 120  # rate-limit reconciliation to once every 2 minutes
_sync_state: dict[str, Any] = {"last_sync_at": 0.0, "last_result": None}
_sync_lock = asyncio.Lock()


def sync_status() -> dict:
    now = time.time()
    last = _sync_state["last_sync_at"]
    return {
        "last_sync_at": last,
        "next_sync_in": max(0, SYNC_TTL_SECONDS - (now - last)) if last else 0,
        "last_result": _sync_state["last_result"],
    }


async def reconcile_companies_with_auth0(force: bool = False) -> dict:
    """Pull the live org list from Auth0 and reconcile it with the local
    COMPANIES mock — add new orgs, drop orgs that no longer exist in
    Auth0, and refresh display names. Rate-limited via SYNC_TTL_SECONDS.
    Never raises; sync failures are returned in the result dict so page
    rendering can continue."""
    now = time.time()
    if not force and now - _sync_state["last_sync_at"] < SYNC_TTL_SECONDS:
        return {"skipped": True, "reason": "rate_limited", **sync_status()}

    async with _sync_lock:
        # Re-check inside the lock so concurrent requests don't double-sync.
        now = time.time()
        if not force and now - _sync_state["last_sync_at"] < SYNC_TTL_SECONDS:
            return {"skipped": True, "reason": "rate_limited", **sync_status()}

        try:
            auth0_orgs = await list_organizations()
        except ManagementError as e:
            result = {"error": str(e)}
            _sync_state["last_result"] = result
            return result

        auth0_by_name = {o["name"]: o for o in auth0_orgs}
        local_names = {c["org_name"] for c in COMPANIES}

        removed: list[str] = []
        for c in list(COMPANIES):
            if c["org_name"] not in auth0_by_name:
                COMPANIES.remove(c)
                removed.append(c["org_name"])

        added: list[str] = []
        for name, org in auth0_by_name.items():
            if name not in local_names:
                add_company(
                    org_name=name,
                    display_name=org.get("display_name", name),
                    budget=100_000,
                )
                added.append(name)

        renamed: list[str] = []
        for c in COMPANIES:
            org = auth0_by_name.get(c["org_name"])
            if org and org.get("display_name") and org["display_name"] != c["display_name"]:
                c["display_name"] = org["display_name"]
                renamed.append(c["org_name"])

        result = {
            "added": added,
            "removed": removed,
            "renamed": renamed,
            "auth0_total": len(auth0_orgs),
            "synced_at": now,
        }
        _sync_state["last_sync_at"] = now
        _sync_state["last_result"] = result
        return result
