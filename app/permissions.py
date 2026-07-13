"""Permission helpers for Compass0.

The Auth0 API at audience `https://compasszero.api` has RBAC enabled
with **Add Permissions in the Access Token** turned on, so the access
token's `permissions` claim is populated automatically based on the
user's assigned Auth0 Roles.

Role → permissions mapping (configured in the Auth0 dashboard, not
here — this file just consumes whatever the token says):

  compass_admin → all permissions
  travel_agent  → read:my_company, read:company_trips, read:my_customers,
                  book:trips, book:experiences
  customer      → read:my_trips, read:my_company

Custom claims at the namespace below are populated by a small
post-login Action that copies `app_metadata.customer_id` and
`app_metadata.agent_id` to the token (see AUTH0_API_DEFINITIONS.md).

`org_id` and `org_name` are standard Auth0 Organizations claims —
populated automatically when a user logs in via an Organization. No
Action needed for those.
"""

NAMESPACE = "https://compasszero.app/"


def get_user_context(access_token_claims: dict | None, id_token_claims: dict | None) -> dict:
    """Aggregate everything a request handler / chat tool dispatcher
    needs to make a permission decision.

    Returns:
        {
            "permissions": set[str],
            "role": "compass_admin" | "travel_agent" | "customer" | "unknown",
            "org_id": str | None,
            "org_name": str | None,
            "customer_id": str | None,
            "agent_id": str | None,
        }
    """
    at = access_token_claims or {}
    it = id_token_claims or {}
    perms = set(at.get("permissions") or [])
    role = role_for(perms)
    # If RBAC didn't pin a role (e.g. Okta SSO admins who haven't been
    # assigned the compass_admin Auth0 Role yet), fall back to a custom
    # claim that an Auth0 Action can stamp onto the token from
    # app_metadata.role. Lets the Okta-only flow work without RBAC set up.
    if role == "unknown":
        claim_role = (
            at.get(NAMESPACE + "role")
            or it.get(NAMESPACE + "role")
            or ""
        )
        if claim_role in ("compass_admin", "travel_agent", "customer"):
            role = claim_role
    return {
        "permissions": perms,
        "role": role,
        "sub": it.get("sub") or at.get("sub"),
        "org_id": at.get("org_id") or it.get("org_id"),
        "org_name": (
            at.get("org_name")
            or it.get("org_name")
            or at.get(NAMESPACE + "org_name")
            or it.get(NAMESPACE + "org_name")
        ),
        "customer_id": (
            it.get(NAMESPACE + "customer_id") or at.get(NAMESPACE + "customer_id")
        ),
        "agent_id": (
            it.get(NAMESPACE + "agent_id") or at.get(NAMESPACE + "agent_id")
        ),
    }


def role_for(permissions: set[str]) -> str:
    if "manage:companies" in permissions or "manage:agents" in permissions:
        return "compass_admin"
    if "book:trips" in permissions:
        return "travel_agent"
    if "read:my_trips" in permissions:
        return "customer"
    return "unknown"


def has_permission(ctx: dict, *needed: str) -> bool:
    perms = ctx.get("permissions") or set()
    return all(n in perms for n in needed)


def has_any_permission(ctx: dict, *candidates: str) -> bool:
    perms = ctx.get("permissions") or set()
    return any(c in perms for c in candidates)


class PermissionDenied(Exception):
    pass


def require(ctx: dict, *needed: str) -> None:
    """Raise PermissionDenied if any required permission is missing."""
    perms = ctx.get("permissions") or set()
    missing = [n for n in needed if n not in perms]
    if missing:
        raise PermissionDenied(
            f"Missing required permission(s): {', '.join(missing)}. "
            f"Your role is '{ctx.get('role', 'unknown')}'."
        )


def require_any(ctx: dict, *candidates: str) -> None:
    """Raise PermissionDenied if NONE of the candidate permissions is held."""
    if not has_any_permission(ctx, *candidates):
        raise PermissionDenied(
            f"Need at least one of: {', '.join(candidates)}. "
            f"Your role is '{ctx.get('role', 'unknown')}'."
        )
