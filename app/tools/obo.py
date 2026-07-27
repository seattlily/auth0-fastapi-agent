import hashlib
import os
import time

import httpx

OBO_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
OBO_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


class OBOError(RuntimeError):
    pass


# key → (access_token, expires_at)
_cache: dict[str, tuple[str, float]] = {}


def _cache_key(access_token: str, audience: str, scope: str) -> str:
    raw = f"{access_token}:{audience}:{scope}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def get_obo_token(
    access_token: str,
    audience: str,
    scope: str = "",
) -> str:
    """Exchange a user access token for a downstream-audience token via OBO.

    The original user identity (sub) is preserved in the new token.
    The act claim records the delegation chain (capped at 5 levels by Auth0).
    Tokens are cached for their full lifetime to avoid repeated exchanges.
    """
    if not access_token:
        raise OBOError("No access token in session — user must be logged in.")

    key = _cache_key(access_token, audience, scope)
    cached = _cache.get(key)
    if cached:
        token, expires_at = cached
        if time.time() < expires_at - 30:
            return token

    domain = os.environ["AUTH0_DOMAIN"]
    client_id = os.environ.get("AUTH0_API_CLIENT_ID") or os.environ["AUTH0_CLIENT_ID"]
    client_secret = os.environ.get("AUTH0_API_CLIENT_SECRET") or os.environ["AUTH0_CLIENT_SECRET"]
    body: dict = {
        "grant_type": OBO_GRANT_TYPE,
        "client_id": client_id,
        "client_secret": client_secret,
        "subject_token": access_token,
        "subject_token_type": OBO_TOKEN_TYPE,
        "requested_token_type": OBO_TOKEN_TYPE,
        "audience": audience,
    }
    if scope:
        body["scope"] = scope

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(f"https://{domain}/oauth/token", json=body)

    if resp.status_code >= 400:
        try:
            data = resp.json()
            detail = data.get("error_description") or data.get("error") or resp.text
        except Exception:
            detail = resp.text
        raise OBOError(f"OBO exchange failed ({resp.status_code}): {detail}")

    data = resp.json()
    token = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))
    _cache[key] = (token, time.time() + expires_in)
    return token


OBO_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "obo_token_exchange",
        "description": (
            "Exchange the signed-in user's access token for a token scoped to "
            "a downstream Auth0-protected API (On-Behalf-Of flow). The user's "
            "identity is preserved. Use this before calling any internal "
            "microservice that validates Auth0 tokens with a different audience."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "audience": {
                    "type": "string",
                    "description": (
                        "The API identifier (audience) of the downstream service "
                        "to obtain a token for."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Optional space-delimited scopes to request from the "
                        "downstream API."
                    ),
                },
            },
            "required": ["audience"],
        },
    },
}
