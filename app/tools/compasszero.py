"""Compass0 chat tools — every tool is permission-gated.

Each tool entry has:
- `schema`: OpenAI function-tool schema (what the model sees)
- `required_scopes`: tuple of Auth0 permissions that must be present
- `fn`: async callable that takes (args, ctx) and returns a JSON string

The dispatcher in main.py:
1. Filters the schema list to only tools the current user has scopes for.
2. Re-checks the scope server-side at execution time.
"""

import json
from datetime import datetime
from typing import Awaitable, Callable

from permissions import PermissionDenied, has_any_permission, has_permission, require, require_any
from mock_data import (
    add_approval_request,
    add_company,
    add_customer,
    add_document,
    add_experience,
    add_travel_agent,
    add_trip,
    get_agents,
    get_approval_requests,
    get_companies,
    get_company,
    get_customer,
    get_customer_by_email,
    get_customers,
    get_experiences_for_trip,
    get_trip,
    get_trips,
    remove_customer,
    remove_experience,
    remove_travel_agent,
    resolve_customer,
)


# ---------- read-only tools ----------


async def list_my_trips(args: dict, ctx: dict) -> str:
    require(ctx, "read:my_trips")
    customer_id = ctx.get("customer_id")
    if not customer_id:
        return json.dumps({"error": "no customer_id on user — ask an admin to set app_metadata.customer_id"})
    return json.dumps(get_trips(customer_id=customer_id))


async def list_company_trips(args: dict, ctx: dict) -> str:
    require_any(ctx, "read:company_trips", "read:all_trips")
    org_name = ctx.get("org_name")
    if has_permission(ctx, "read:all_trips"):
        return json.dumps(get_trips())
    if not org_name:
        return json.dumps({"error": "no org_name on user — log in via your travel agency's organization"})
    return json.dumps(get_trips(org_name=org_name))


async def list_all_trips(args: dict, ctx: dict) -> str:
    require(ctx, "read:all_trips")
    return json.dumps(get_trips())


async def list_my_customers(args: dict, ctx: dict) -> str:
    require_any(ctx, "read:my_customers", "read:all_customers")
    if has_permission(ctx, "read:all_customers"):
        return json.dumps(get_customers())
    return json.dumps(get_customers(org_name=ctx.get("org_name")))


async def list_all_customers(args: dict, ctx: dict) -> str:
    require(ctx, "read:all_customers")
    return json.dumps(get_customers())


async def list_companies(args: dict, ctx: dict) -> str:
    require(ctx, "read:all_companies")
    return json.dumps(get_companies())


async def list_travel_agents(args: dict, ctx: dict) -> str:
    require_any(ctx, "manage:agents", "manage:companies")
    org_name = args.get("org_name") or ctx.get("org_name")
    agents = get_agents(org_name=org_name) if org_name else get_agents()
    return json.dumps(agents)


async def list_pending_requests(args: dict, ctx: dict) -> str:
    """Return pending trip / experience approval requests scoped to the
    caller's role:

    - admin    → all pending requests across every org
    - agent    → pending requests in the agent's current org
                 (these are the ones awaiting their approval)
    - customer → just their own pending requests

    The chat assistant has no other way to read APPROVAL_REQUESTS, so
    this is the tool to call whenever the user asks about \"pending
    trips\", \"requests waiting on approval\", \"did my request go
    through?\", etc.
    """
    require(ctx, "read:my_company")
    role = ctx.get("role")
    if role == "compass_admin":
        return json.dumps(get_approval_requests(status="pending"))
    if role == "travel_agent":
        org = ctx.get("org_name")
        if not org:
            return json.dumps({"error": "no org_name on user — log in via your travel agency's organization"})
        return json.dumps(get_approval_requests(org_name=org, status="pending"))
    if role == "customer":
        cid = ctx.get("customer_id")
        if not cid:
            return json.dumps({"error": "no customer_id on user — ask an admin to set app_metadata.customer_id"})
        return json.dumps(get_approval_requests(customer_id=cid, status="pending"))
    return json.dumps({"error": f"role '{role}' cannot list pending requests"})


# ---------- write tools ----------


async def _ciba_step_up(ctx: dict, binding_message: str) -> str | None:
    """Run CIBA step-up against the signed-in user. Returns None on
    approval (or when CIBA is bypassed via env var). Returns a JSON
    error string if step-up fails so callers can return it directly
    to the LLM — and so the LLM stops retrying."""
    from .auth0_ciba import CibaError, CibaNotEnrolledError, step_up

    sub = ctx.get("sub")
    if not sub:
        return json.dumps(
            {
                "error": "step-up required but no user sub in token — cannot initiate CIBA",
                "stop_retrying": True,
            }
        )
    try:
        # 3-minute wait so the user has time to find their phone,
        # read the binding, and tap Approve. step_up will further
        # cap at the auth_req's actual expires_in so we never wait
        # past the point Auth0 would return expired_token.
        await step_up(user_sub=sub, binding_message=binding_message, max_seconds=180)
    except CibaNotEnrolledError as e:
        return json.dumps(
            {
                "error": str(e),
                "enrollment_url": "/mfa/enroll",
                "next_step": (
                    "This action requires a one-time MFA enrollment first. "
                    "Render this exact markdown link for the user, verbatim: "
                    "**[Set up step-up authentication](/mfa/enroll)** — they "
                    "will scan a QR code with the Auth0 Guardian app to "
                    "register their phone, then come back and ask you to "
                    "retry. Do NOT call any tools again until they say so."
                ),
                "stop_retrying": True,
            }
        )
    except CibaError as e:
        return json.dumps(
            {
                "error": f"Step-up authentication failed: {e}",
                "next_step": (
                    "Tell the user the step-up failed and quote the exact "
                    "reason from the error field above (denied / expired / "
                    "polling timed out / other). Do NOT retry the action "
                    "automatically — ask them whether they want to try "
                    "again, and remind them to keep their phone unlocked "
                    "and tap Approve in the Auth0 Guardian app."
                ),
                "stop_retrying": True,
            }
        )
    return None


async def book_trip(args: dict, ctx: dict) -> str:
    require(ctx, "book:trips")
    customer = resolve_customer(args["customer_id"])
    if not customer:
        return json.dumps({"error": f"customer not found: {args['customer_id']}"})
    if not has_permission(ctx, "manage:companies"):
        if customer["org_name"] != ctx.get("org_name"):
            raise PermissionDenied(
                f"Customer {args['customer_id']} is not in your organization."
            )

    binding = (
        f"Approve booking {args['type']} "
        f"{args['origin']} to {args['destination']} {args['depart_date']}"
    )
    err = await _ciba_step_up(ctx, binding)
    if err:
        return err

    trip = add_trip(
        customer_id=customer["id"],
        type=args["type"],
        origin=args["origin"],
        destination=args["destination"],
        depart_date=args["depart_date"],
        return_date=args["return_date"],
        cost=float(args["cost"]),
        currency=args.get("currency", "USD"),
    )
    return json.dumps({"ok": True, "trip": trip})


async def cancel_trip(args: dict, ctx: dict) -> str:
    require(ctx, "book:trips")
    trip = get_trip(args["trip_id"])
    if not trip:
        return json.dumps({"error": f"unknown trip_id: {args['trip_id']}"})
    customer = get_customer(trip["customer_id"])
    if not has_permission(ctx, "manage:companies"):
        if not customer or customer["org_name"] != ctx.get("org_name"):
            raise PermissionDenied(
                f"Trip {args['trip_id']} is outside your organization."
            )
    if trip["status"] == "cancelled":
        return json.dumps({"error": f"trip {args['trip_id']} is already cancelled"})

    binding = f"Approve cancelling trip {args['trip_id']}"
    err = await _ciba_step_up(ctx, binding)
    if err:
        return err

    trip["status"] = "cancelled"
    return json.dumps({"ok": True, "trip": trip})


async def cancel_experience(args: dict, ctx: dict) -> str:
    require(ctx, "book:experiences")
    experience_id = args.get("experience_id", "")
    from mock_data import EXPERIENCES
    exp = next((e for e in EXPERIENCES if e["id"] == experience_id), None)
    if not exp:
        return json.dumps({"error": f"unknown experience_id: {experience_id}"})
    customer = get_customer(exp["customer_id"])
    if not has_permission(ctx, "manage:companies"):
        if not customer or customer["org_name"] != ctx.get("org_name"):
            raise PermissionDenied(
                f"Experience {experience_id} is outside your organization."
            )
    binding = f"Approve cancelling experience {experience_id} ({exp['name']})"
    err = await _ciba_step_up(ctx, binding)
    if err:
        return err
    removed = remove_experience(experience_id)
    return json.dumps({"ok": True, "removed": removed})


# ---------- flight search (mock) ----------


_AIRLINES = [
    ("United Airlines", "UA", 1000),
    ("Delta", "DL", 2000),
    ("Lufthansa", "LH", 3000),
]


def _mock_flights(origin: str, destination: str, date: str) -> list[dict]:
    """Deterministic-ish mock flight options. Same inputs → same offers."""
    o, d = origin.strip().upper(), destination.strip().upper()
    seed = sum(ord(c) for c in (o + d + date)) % 100
    base = 250 + seed * 8

    schedules = [
        ("08:30", "12:15", "3h 45m", 0, 0),
        ("13:00", "16:55", "3h 55m", 0, 75),
        ("21:45", "06:30+1", "5h 45m", 1, -40),
    ]
    flights = []
    for (name, code, base_no), (dep, arr, dur, stops, delta) in zip(_AIRLINES, schedules):
        flights.append(
            {
                "id": f"fl_{code}{base_no + seed}_{o}{d}_{date}",
                "airline": name,
                "flight_no": f"{code}{base_no + seed}",
                "origin": o,
                "destination": d,
                "date": date,
                "depart_time": dep,
                "arrive_time": arr,
                "duration": dur,
                "stops": stops,
                "price": base + delta,
                "currency": "USD",
            }
        )
    return flights


async def search_flights(args: dict, ctx: dict) -> str:
    require_any(ctx, "book:trips", "read:my_trips")
    origin = (args.get("origin") or "").strip()
    destination = (args.get("destination") or "").strip()
    date = (args.get("date") or "").strip()
    if not (origin and destination and date):
        return json.dumps({"error": "origin, destination, and date are all required."})
    return json.dumps(
        {
            "origin": origin.upper(),
            "destination": destination.upper(),
            "date": date,
            "flights": _mock_flights(origin, destination, date),
        }
    )


# ---------- experience catalog (mock) ----------


_EXPERIENCE_CATALOG: list[dict] = [
    {"id": "exc_001", "name": "Tuscan cooking class with a local chef",   "category": "cooking_class", "location": "Florence",      "country": "Italy",         "duration": "4h",  "price": 145, "currency": "USD", "description": "Hands-on small-group class — fresh pasta, sauces, tiramisu — in a 16th-century farmhouse.",  "available_times": ["09:00", "13:00", "17:00"]},
    {"id": "exc_002", "name": "Tokyo sushi-making workshop",              "category": "cooking_class", "location": "Tokyo",         "country": "Japan",         "duration": "3h",  "price": 110, "currency": "USD", "description": "Learn nigiri and maki technique from a Tsukiji-trained chef in a private kitchen.",           "available_times": ["10:00", "14:00"]},
    {"id": "exc_003", "name": "Bordeaux château wine tasting",            "category": "wine_tasting",  "location": "Bordeaux",      "country": "France",        "duration": "5h",  "price": 220, "currency": "USD", "description": "Visit two grand cru estates with a sommelier guide; six wines, lunch included.",              "available_times": ["09:30", "14:00"]},
    {"id": "exc_004", "name": "Napa Valley wine tour with vineyard lunch","category": "wine_tasting",  "location": "Napa",          "country": "USA",           "duration": "6h",  "price": 285, "currency": "USD", "description": "Three-vineyard tour by private van with a farm-to-table lunch on the second stop.",           "available_times": ["09:00", "11:00"]},
    {"id": "exc_005", "name": "Mt. Fuji day hike",                        "category": "hike",          "location": "Mt. Fuji",      "country": "Japan",         "duration": "10h", "price": 180, "currency": "USD", "description": "Yoshida-trail summit hike with a certified guide; gear and bus transfers included.",          "available_times": ["05:00", "06:00"]},
    {"id": "exc_006", "name": "Cinque Terre coastal hike",                "category": "hike",          "location": "Cinque Terre",  "country": "Italy",         "duration": "7h",  "price": 95,  "currency": "USD", "description": "Guided trek across the five villages on the high path, with a focaccia stop in Vernazza.",   "available_times": ["08:00", "10:00"]},
    {"id": "exc_007", "name": "Day trip to Versailles",                   "category": "day_trip",      "location": "Versailles",    "country": "France",        "duration": "8h",  "price": 130, "currency": "USD", "description": "Round-trip from Paris with skip-the-line palace + gardens entry and an art historian guide.", "available_times": ["08:30", "10:00", "13:00"]},
    {"id": "exc_008", "name": "Stonehenge & Bath day trip",               "category": "day_trip",      "location": "Wiltshire",     "country": "United Kingdom","duration": "10h", "price": 165, "currency": "USD", "description": "Coach from London with inner-circle Stonehenge access and a walking tour of Bath's Roman baths.", "available_times": ["07:30", "09:00"]},
    {"id": "exc_009", "name": "Barcelona tapas & wine walk",              "category": "food_tour",     "location": "Barcelona",     "country": "Spain",         "duration": "3h",  "price": 95,  "currency": "USD", "description": "Five-stop tapas crawl through the Gothic Quarter with wine pairings at each.",               "available_times": ["12:00", "18:00", "20:00"]},
    {"id": "exc_010", "name": "Marrakech medina & souks tour",            "category": "cultural",      "location": "Marrakech",     "country": "Morocco",       "duration": "4h",  "price": 75,  "currency": "USD", "description": "Walking tour of the medina with a local guide; mint tea at a riad to finish.",               "available_times": ["09:00", "14:00"]},
    {"id": "exc_011", "name": "Iceland glacier hike on Sólheimajökull",   "category": "hike",          "location": "Sólheimajökull","country": "Iceland",       "duration": "5h",  "price": 195, "currency": "USD", "description": "Guided ice-axe hike with crampons and helmets provided; transfer from Reykjavík available.",  "available_times": ["08:00", "11:00"]},
    {"id": "exc_012", "name": "West End theatre night, London",           "category": "cultural",      "location": "London",        "country": "United Kingdom","duration": "3h",  "price": 140, "currency": "USD", "description": "Premium-stalls ticket to a current West End show plus pre-theatre dinner reservation.",      "available_times": ["14:00", "19:30"]},
    {"id": "exc_013", "name": "Berlin street art & Mitte walking tour",   "category": "cultural",      "location": "Berlin",        "country": "Germany",       "duration": "3h",  "price": 65,  "currency": "USD", "description": "Expert-led walk through Kreuzberg and Mitte murals, with a stop at the East Side Gallery.", "available_times": ["10:00", "14:00", "17:00"]},
    {"id": "exc_014", "name": "Berlin food market & brewery tour",        "category": "food_tour",     "location": "Berlin",        "country": "Germany",       "duration": "4h",  "price": 90,  "currency": "USD", "description": "Markthalle Neun street food stops paired with tastings at two Berlin craft breweries.",       "available_times": ["11:00", "15:00"]},
    {"id": "exc_015", "name": "New York City skyline helicopter flight",  "category": "day_trip",      "location": "New York",      "country": "USA",           "duration": "1h",  "price": 295, "currency": "USD", "description": "15-minute doors-off helicopter loop over Manhattan, Hudson, and the Statue of Liberty.",     "available_times": ["09:00", "11:00", "13:00", "15:00"]},
    {"id": "exc_016", "name": "Sydney Harbour sailing & snorkelling",     "category": "day_trip",      "location": "Sydney",        "country": "Australia",     "duration": "5h",  "price": 175, "currency": "USD", "description": "Half-day on a classic wooden ketch — morning snorkelling off Manly, harbour sailing back.",   "available_times": ["08:30", "13:30"]},
]

_EXPERIENCE_CATEGORIES = sorted({e["category"] for e in _EXPERIENCE_CATALOG})


async def search_experiences(args: dict, ctx: dict) -> str:
    require_any(ctx, "book:experiences", "read:my_trips")
    location = (args.get("location") or "").strip().lower()
    category = (args.get("category") or "").strip().lower()

    out = _EXPERIENCE_CATALOG
    location_matched = True
    if location:
        filtered = [e for e in out if location in e["location"].lower() or location in e["country"].lower()]
        if filtered:
            out = filtered
        else:
            # No exact match — return full catalog so the agent can present
            # all available options rather than reporting zero results.
            location_matched = False
    if category:
        out = [e for e in out if e["category"] == category]
    result: dict = {
        "categories": _EXPERIENCE_CATEGORIES,
        "match_count": len(out),
        "experiences": out,
    }
    if location and not location_matched:
        result["location_note"] = (
            f"No experiences found specifically in '{args.get('location')}'. "
            "Showing all available experiences instead."
        )
    return json.dumps(result)


# ---------- trip drill-down ----------


async def get_trip_details(args: dict, ctx: dict) -> str:
    require_any(ctx, "read:my_trips", "read:company_trips", "read:all_trips")
    trip = get_trip(args["trip_id"])
    if not trip:
        return json.dumps({"error": f"unknown trip_id: {args['trip_id']}"})
    customer = get_customer(trip["customer_id"])
    if not has_permission(ctx, "read:all_trips"):
        if has_permission(ctx, "read:company_trips"):
            if not customer or customer["org_name"] != ctx.get("org_name"):
                raise PermissionDenied(
                    f"Trip {args['trip_id']} is outside your organization."
                )
        elif has_permission(ctx, "read:my_trips"):
            if trip["customer_id"] != ctx.get("customer_id"):
                raise PermissionDenied(
                    f"Trip {args['trip_id']} is not yours."
                )
    return json.dumps(
        {
            "trip": trip,
            "customer": (
                {"id": customer["id"], "name": customer["name"], "email": customer["email"]}
                if customer
                else None
            ),
            "experiences": get_experiences_for_trip(trip["id"]),
        }
    )


async def book_experience(args: dict, ctx: dict) -> str:
    require(ctx, "book:experiences")
    trip = get_trip(args["trip_id"])
    if not trip:
        return json.dumps({"error": f"unknown trip_id: {args['trip_id']}"})
    customer = get_customer(trip["customer_id"])
    if not customer:
        return json.dumps({"error": "trip's customer not found"})
    if not has_permission(ctx, "manage:companies"):
        if customer["org_name"] != ctx.get("org_name"):
            raise PermissionDenied(
                f"Trip {args['trip_id']} belongs to a customer outside your organization."
            )
    experience = add_experience(
        customer_id=trip["customer_id"],
        trip_id=args["trip_id"],
        name=args["name"],
        date=args["date"],
        cost=float(args["cost"]),
        location=args.get("location", ""),
    )
    return json.dumps({"ok": True, "experience": experience})


async def book_customer_experience(args: dict, ctx: dict) -> str:
    """Book a standalone experience for a customer with no flight
    attached — e.g. a one-off cooking class or wine tasting that
    isn't part of a trip booking."""
    require(ctx, "book:experiences")
    customer = resolve_customer(args["customer_id"])
    if not customer:
        return json.dumps(
            {"error": f"customer not found: {args['customer_id']}"}
        )
    if not has_permission(ctx, "manage:companies"):
        if customer["org_name"] != ctx.get("org_name"):
            raise PermissionDenied(
                f"Customer {args['customer_id']} is not in your organization."
            )

    binding = (
        f"Approve booking activity for customer {customer['name']} ({customer['id']})"
    )
    err = await _ciba_step_up(ctx, binding)
    if err:
        return err

    experience = add_experience(
        customer_id=customer["id"],
        name=args["name"],
        date=args["date"],
        cost=float(args["cost"]),
        location=args.get("location", ""),
    )
    return json.dumps({"ok": True, "experience": experience})


async def create_company(args: dict, ctx: dict) -> str:
    require(ctx, "manage:companies")
    company = add_company(
        org_name=args["org_name"],
        display_name=args["display_name"],
        budget=float(args["budget"]),
        currency=args.get("currency", "USD"),
    )
    return json.dumps({"ok": True, "company": company})


async def create_auth0_organization(args: dict, ctx: dict) -> str:
    """Create a real Auth0 Organization via the Management API and mirror
    it into the local Compass0 company list so the dashboard reflects it."""
    require(ctx, "manage:companies")
    from .auth0_management import ManagementError, create_organization

    name = (args.get("name") or "").strip()
    display_name = (args.get("display_name") or name).strip()
    if not name:
        return json.dumps({"error": "name is required (lowercase slug, no spaces)."})

    binding = f"Approve creating organization {name}"
    err = await _ciba_step_up(ctx, binding)
    if err:
        return err

    try:
        org = await create_organization(name=name, display_name=display_name)
    except ManagementError as e:
        return json.dumps({"error": str(e)})

    company = add_company(
        org_name=org.get("name", name),
        display_name=org.get("display_name", display_name),
        budget=float(args.get("budget") or 100000),
        currency=args.get("currency") or "USD",
    )
    return json.dumps({"ok": True, "auth0_org": org, "company": company})


async def delete_auth0_organization(args: dict, ctx: dict) -> str:
    """Delete an Auth0 Organization via the Management API and remove the
    corresponding entry from the local Compass0 company list."""
    require(ctx, "manage:companies")
    from .auth0_management import (
        ManagementError,
        delete_organization,
        get_organization_by_name,
    )

    name = (args.get("name") or "").strip()
    if not name:
        return json.dumps({"error": "name (org slug) is required."})

    try:
        org = await get_organization_by_name(name)
    except ManagementError as e:
        return json.dumps({"error": str(e)})
    if not org:
        return json.dumps({"error": f"no Auth0 organization named '{name}'"})

    binding = f"Approve DELETING organization {name}"
    err = await _ciba_step_up(ctx, binding)
    if err:
        return err

    try:
        await delete_organization(org["id"])
    except ManagementError as e:
        return json.dumps({"error": str(e)})

    # Mirror locally — drop matching company.
    from mock_data import COMPANIES

    for c in list(COMPANIES):
        if c["org_name"] == name:
            COMPANIES.remove(c)
    return json.dumps({"ok": True, "deleted": {"id": org["id"], "name": name}})


async def create_travel_agent(args: dict, ctx: dict) -> str:
    """Admin-only: create a real Auth0 user, add them to an existing
    Organization, and assign the travel_agent role. Mirrors locally
    into the TRAVEL_AGENTS list so the dashboard reflects it."""
    require(ctx, "manage:companies")
    from .auth0_management import (
        ManagementError,
        add_organization_member,
        assign_organization_member_roles,
        create_database_user,
        get_organization_by_name,
        get_role_id,
    )

    org_slug = (args.get("org_name") or "").strip()
    name = (args.get("name") or "").strip()
    email = (args.get("email") or "").strip()
    if not (org_slug and name and email):
        return json.dumps(
            {"error": "org_name, name, and email are all required."}
        )

    try:
        org = await get_organization_by_name(org_slug)
    except ManagementError as e:
        return json.dumps({"error": str(e)})
    if not org:
        return json.dumps(
            {
                "error": (
                    f"no Auth0 organization named '{org_slug}'. "
                    "Create it first via create_auth0_organization, then retry."
                )
            }
        )

    try:
        role_id = await get_role_id("travel_agent")
    except ManagementError as e:
        return json.dumps({"error": str(e)})
    if not role_id:
        return json.dumps(
            {
                "error": (
                    "no Auth0 Role named 'travel_agent' found in the tenant. "
                    "Create the role under Auth0 Dashboard → User Management → "
                    "Roles before adding agents."
                )
            }
        )

    binding = f"Approve adding travel agent {email} to {org_slug}"
    err = await _ciba_step_up(ctx, binding)
    if err:
        return err

    try:
        user = await create_database_user(email=email, name=name)
        await add_organization_member(org["id"], user["user_id"])
        await assign_organization_member_roles(
            org["id"], user["user_id"], [role_id]
        )
    except ManagementError as e:
        return json.dumps({"error": str(e)})

    agent = add_travel_agent(name=name, email=email, org_name=org_slug)
    return json.dumps(
        {
            "ok": True,
            "agent": agent,
            "auth0_user": {
                "user_id": user.get("user_id"),
                "email": user.get("email"),
                "name": user.get("name"),
            },
            "note": (
                "Auth0 user created without an email invite. The agent "
                "will need an admin password reset or change-password "
                "ticket before they can log in."
            ),
        }
    )


async def delete_travel_agent(args: dict, ctx: dict) -> str:
    """Admin-only: remove a travel agent — strip them from the Auth0
    organization, delete the underlying Auth0 user, and drop the local
    mirror. Symmetric to create_travel_agent."""
    require(ctx, "manage:companies")
    from .auth0_management import (
        ManagementError,
        delete_user,
        find_user_by_email,
        get_organization_by_name,
        remove_organization_member,
    )

    org_slug = (args.get("org_name") or "").strip()
    email = (args.get("email") or "").strip()
    if not (org_slug and email):
        return json.dumps({"error": "org_name and email are both required."})

    try:
        org = await get_organization_by_name(org_slug)
    except ManagementError as e:
        return json.dumps({"error": str(e)})
    if not org:
        return json.dumps({"error": f"no Auth0 organization named '{org_slug}'"})

    try:
        user = await find_user_by_email(email)
    except ManagementError as e:
        return json.dumps({"error": str(e)})
    if not user:
        return json.dumps(
            {
                "error": (
                    f"no Auth0 user with email {email}. The agent may have "
                    "already been removed — only the local record will be "
                    "cleaned up."
                )
            }
        )

    binding = f"Approve REMOVING travel agent {email} from {org_slug}"
    err = await _ciba_step_up(ctx, binding)
    if err:
        return err

    user_id = user["user_id"]
    try:
        await remove_organization_member(org["id"], user_id)
        await delete_user(user_id)
    except ManagementError as e:
        return json.dumps({"error": str(e)})

    removed = remove_travel_agent(email=email, org_name=org_slug)
    return json.dumps(
        {
            "ok": True,
            "removed_from_org": org_slug,
            "deleted_user_id": user_id,
            "local_agent": removed,
        }
    )


async def generate_contract(args: dict, ctx: dict) -> str:
    """Admin-only: generate a Compass0 ↔ org services contract PDF
    and add it to the documents list. No CIBA — this is a low-risk
    document creation, not an Auth0 mutation."""
    require(ctx, "manage:companies")
    from .documents import documents_dir, generate_contract_pdf

    org_slug = (args.get("org_name") or "").strip()
    if not org_slug:
        return json.dumps({"error": "org_name is required."})

    company = get_company(org_name=org_slug)
    if not company:
        return json.dumps(
            {
                "error": (
                    f"no local organization named '{org_slug}'. Create the "
                    "organization first via create_auth0_organization, then retry."
                )
            }
        )

    out = documents_dir() / f"contract-{org_slug}.pdf"
    generate_contract_pdf(
        org_name=org_slug,
        display_name=company["display_name"],
        output_path=out,
    )
    doc = add_document(
        kind="contract",
        title=f"Compass0 × {company['display_name']} services agreement",
        filename=out.name,
        org_name=org_slug,
        size_bytes=out.stat().st_size,
    )
    return json.dumps({"ok": True, "document": doc})


async def request_trip(args: dict, ctx: dict) -> str:
    """Customer-facing: submit a trip request that an agent in the
    customer's org must approve before it becomes a booking. No CIBA;
    the agent's approve action is what triggers MFA later."""
    require(ctx, "read:my_trips")
    customer_id = ctx.get("customer_id")
    org = ctx.get("org_name")
    if not customer_id or not org:
        return json.dumps(
            {
                "error": (
                    "missing customer_id or org_name on token — log in via "
                    "your travel agency's organization."
                )
            }
        )

    details = {
        "type": args.get("type", "flight"),
        "origin": args["origin"],
        "destination": args["destination"],
        "depart_date": args["depart_date"],
        "return_date": args["return_date"],
        "cost": float(args["cost"]),
        "currency": args.get("currency", "USD"),
    }
    req = add_approval_request(
        kind="trip",
        customer_id=customer_id,
        org_name=org,
        details=details,
    )
    return json.dumps(
        {
            "ok": True,
            "request": req,
            "message": (
                "Your travel agent has been notified. They'll review and "
                "approve or deny the request from their dashboard — you'll "
                "see the trip on /trips once approved."
            ),
        }
    )


async def request_experience(args: dict, ctx: dict) -> str:
    """Customer-facing: request an experience that needs agent approval."""
    require(ctx, "read:my_trips")
    customer_id = ctx.get("customer_id")
    org = ctx.get("org_name")
    if not customer_id or not org:
        return json.dumps(
            {
                "error": (
                    "missing customer_id or org_name on token — log in via "
                    "your travel agency's organization."
                )
            }
        )

    details = {
        "name": args["name"],
        "date": args["date"],
        "cost": float(args["cost"]),
        "location": args.get("location", ""),
        "trip_id": args.get("trip_id", ""),
    }
    req = add_approval_request(
        kind="experience",
        customer_id=customer_id,
        org_name=org,
        details=details,
    )
    return json.dumps(
        {
            "ok": True,
            "request": req,
            "message": (
                "Your travel agent has been notified. They'll review and "
                "approve or deny the request from their dashboard."
            ),
        }
    )


async def create_my_customer(args: dict, ctx: dict) -> str:
    """Travel-agent: create a local customer record AND a real Auth0 user,
    then add that user to the agent's organization with the customer role."""
    require(ctx, "book:trips")
    from .auth0_management import (
        ManagementError,
        add_organization_member,
        assign_organization_member_roles,
        create_database_user,
        get_organization_by_name,
        get_role_id,
    )

    org_name = ctx.get("org_name")
    if not org_name:
        return json.dumps(
            {"error": "no org_name on your token — log in via your travel agency's organization"}
        )

    name = (args.get("name") or "").strip()
    email = (args.get("email") or "").strip()
    if not (name and email):
        return json.dumps({"error": "name and email are required."})

    try:
        org = await get_organization_by_name(org_name)
    except ManagementError as e:
        return json.dumps({"error": str(e)})
    if not org:
        return json.dumps(
            {"error": f"no Auth0 organization named '{org_name}' — contact your admin."}
        )

    try:
        role_id = await get_role_id("customer")
    except ManagementError as e:
        return json.dumps({"error": str(e)})

    try:
        auth0_user = await create_database_user(email=email, name=name)
        await add_organization_member(org["id"], auth0_user["user_id"])
        if role_id:
            await assign_organization_member_roles(
                org["id"], auth0_user["user_id"], [role_id]
            )
    except ManagementError as e:
        return json.dumps({"error": str(e)})

    customer = add_customer(
        name=name,
        email=email,
        org_name=org_name,
        agent_id=ctx.get("agent_id"),
    )
    customer["auth0_user_id"] = auth0_user.get("user_id")
    return json.dumps(
        {
            "ok": True,
            "customer": customer,
            "auth0_user": {
                "user_id": auth0_user.get("user_id"),
                "email": auth0_user.get("email"),
            },
            "note": (
                "Auth0 user created. They will need a password reset "
                "before they can log in."
            ),
        }
    )


async def delete_customer(args: dict, ctx: dict) -> str:
    """Travel-agent: remove a customer from the organization — strips them
    from the Auth0 org, deletes the Auth0 user, and drops the local record.
    Requires CIBA step-up. Agents can only delete customers in their own org."""
    require(ctx, "book:trips")
    from .auth0_management import (
        ManagementError,
        delete_user,
        find_user_by_email,
        get_organization_by_name,
        remove_organization_member,
    )

    org_name = ctx.get("org_name")
    if not org_name:
        return json.dumps(
            {"error": "no org_name on your token — log in via your travel agency's organization"}
        )

    identifier = (args.get("customer_id") or "").strip()
    email = (args.get("email") or "").strip()
    if not identifier and not email:
        return json.dumps({"error": "provide either customer_id or email."})

    # Resolve customer from local records
    if identifier:
        customer = get_customer(identifier)
    else:
        customer = get_customer_by_email(email)

    if not customer:
        return json.dumps({"error": f"no customer found for {identifier or email}."})
    if customer["org_name"] != org_name:
        return json.dumps({"error": "that customer is not in your organization."})

    email = customer["email"]

    try:
        org = await get_organization_by_name(org_name)
    except ManagementError as e:
        return json.dumps({"error": str(e)})
    if not org:
        return json.dumps({"error": f"no Auth0 organization named '{org_name}'."})

    try:
        auth0_user = await find_user_by_email(email)
    except ManagementError as e:
        return json.dumps({"error": str(e)})

    binding = f"Approve DELETING customer {customer['name']} ({email}) from {org_name}"
    err = await _ciba_step_up(ctx, binding)
    if err:
        return err

    deleted_user_id = None
    if auth0_user:
        user_id = auth0_user["user_id"]
        try:
            await remove_organization_member(org["id"], user_id)
            await delete_user(user_id)
            deleted_user_id = user_id
        except ManagementError as e:
            return json.dumps({"error": str(e)})

    removed = remove_customer(customer["id"])
    return json.dumps(
        {
            "ok": True,
            "removed_customer": removed,
            "deleted_auth0_user_id": deleted_user_id,
            "note": (
                None if deleted_user_id
                else "No Auth0 user found for this email — only the local record was removed."
            ),
        }
    )


# ---------- tool registry ----------


TOOLS: dict[str, dict] = {
    "list_my_trips": {
        "required_scopes": ("read:my_trips",),
        "fn": list_my_trips,
        "schema": {
            "type": "function",
            "function": {
                "name": "list_my_trips",
                "description": "List the signed-in customer's own bookings (trips + hotels + trains). Use whenever the user asks 'my trips', 'my upcoming travel', 'where am I going', etc.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "list_company_trips": {
        "required_scopes": ("read:company_trips",),
        "fn": list_company_trips,
        "schema": {
            "type": "function",
            "function": {
                "name": "list_company_trips",
                "description": "List all bookings inside the signed-in agent's organization. Use for 'all our customers' trips', 'recent bookings for our org', etc.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "list_all_trips": {
        "required_scopes": ("read:all_trips",),
        "fn": list_all_trips,
        "schema": {
            "type": "function",
            "function": {
                "name": "list_all_trips",
                "description": "List bookings across all Compass0 organizations. Admin-only.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "list_my_customers": {
        "required_scopes": ("read:my_customers",),
        "fn": list_my_customers,
        "schema": {
            "type": "function",
            "function": {
                "name": "list_my_customers",
                "description": "List the customers in the signed-in travel agent's organization.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "list_all_customers": {
        "required_scopes": ("read:all_customers",),
        "fn": list_all_customers,
        "schema": {
            "type": "function",
            "function": {
                "name": "list_all_customers",
                "description": "List every customer across all Compass0 organizations. Admin-only.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "list_companies": {
        "required_scopes": ("read:all_companies",),
        "fn": list_companies,
        "schema": {
            "type": "function",
            "function": {
                "name": "list_companies",
                "description": "List every Compass0 organization with budget vs. spent. Admin-only.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "list_travel_agents": {
        "required_scopes": ("manage:companies",),
        "fn": list_travel_agents,
        "schema": {
            "type": "function",
            "function": {
                "name": "list_travel_agents",
                "description": (
                    "List travel agents. Admin sees all agents across every "
                    "organization; pass org_name to filter to one org."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "org_name": {
                            "type": "string",
                            "description": "Filter to a specific org slug. Omit to list all agents.",
                        }
                    },
                },
            },
        },
    },
    "list_pending_requests": {
        "required_scopes": ("read:my_company",),
        "fn": list_pending_requests,
        "schema": {
            "type": "function",
            "function": {
                "name": "list_pending_requests",
                "description": (
                    "List pending trip and experience approval requests "
                    "(submitted by customers via request_trip / "
                    "request_experience, awaiting an agent's decision). "
                    "Use whenever the user asks about 'pending trips', "
                    "'pending requests', 'awaiting approval', 'did my "
                    "request go through?', or any variation. Customers see "
                    "their own; travel agents see ones in their org "
                    "awaiting their approval; admins see everything. The "
                    "tool auto-scopes by role — never prompt the user for "
                    "org_name or customer_id."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "book_trip": {
        "required_scopes": ("book:trips",),
        "fn": book_trip,
        "schema": {
            "type": "function",
            "function": {
                "name": "book_trip",
                "description": (
                    "Book a new flight/hotel/train for a customer. "
                    "PRECONDITION — for flights, you MUST call search_flights "
                    "first, present the numbered results to the user, and wait "
                    "for them to choose one before calling this tool. Populate "
                    "origin, destination, depart_date, and cost from the chosen "
                    "result — never invent or default these values. For hotels "
                    "and trains where no search tool exists, ask the user for "
                    "the details before calling. Agents can only book for "
                    "customers in their own org; admins can book for anyone."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "customer_id":  {"type": "string", "description": "Customer ID (cu_xxx), email, or full name. Use list_my_customers if unsure."},
                        "type":         {"type": "string", "enum": ["flight", "hotel", "train"]},
                        "origin":       {"type": "string", "description": "Origin city or IATA code."},
                        "destination":  {"type": "string", "description": "Destination city or IATA code."},
                        "depart_date":  {"type": "string", "description": "Departure date YYYY-MM-DD."},
                        "return_date":  {"type": "string", "description": "Return date YYYY-MM-DD."},
                        "cost":         {"type": "number", "description": "Total cost in the organization's currency."},
                        "currency":     {"type": "string", "description": "ISO currency code. Default USD."},
                    },
                    "required": ["customer_id", "type", "origin", "destination", "depart_date", "return_date", "cost"],
                },
            },
        },
    },
    "cancel_trip": {
        "required_scopes": ("book:trips",),
        "fn": cancel_trip,
        "schema": {
            "type": "function",
            "function": {
                "name": "cancel_trip",
                "description": (
                    "Cancel an existing booking by setting its status to "
                    "'cancelled'. Requires CIBA step-up — the user has to "
                    "approve the cancellation on their enrolled device. "
                    "Agents can only cancel trips owned by customers in "
                    "their org; admins can cancel any trip."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "trip_id": {"type": "string", "description": "Trip ID (tr_xxx) to cancel."},
                    },
                    "required": ["trip_id"],
                },
            },
        },
    },
    "cancel_experience": {
        "required_scopes": ("book:experiences",),
        "fn": cancel_experience,
        "schema": {
            "type": "function",
            "function": {
                "name": "cancel_experience",
                "description": (
                    "Permanently remove a booked experience. Requires CIBA "
                    "step-up — the user must approve on their enrolled device. "
                    "Agents can only cancel experiences for customers in their org."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "experience_id": {
                            "type": "string",
                            "description": "Experience ID (ex_xxx) to cancel.",
                        },
                    },
                    "required": ["experience_id"],
                },
            },
        },
    },
    "book_experience": {
        "required_scopes": ("book:experiences",),
        "fn": book_experience,
        "schema": {
            "type": "function",
            "function": {
                "name": "book_experience",
                "description": (
                    "Attach an experience to an EXISTING trip. "
                    "PRECONDITION — call search_experiences first, present "
                    "the numbered results, and wait for the user to choose "
                    "one AND a specific time slot before calling this tool. "
                    "Never auto-select an experience. Use for experiences "
                    "that attach to a parent trip; for standalone experiences "
                    "use book_customer_experience instead. Agents can only "
                    "book for trips owned by customers in their own org."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "trip_id":  {"type": "string", "description": "Trip ID (tr_xxx) to attach the experience to."},
                        "name":     {"type": "string", "description": "Short experience name."},
                        "date":     {"type": "string", "description": "Date YYYY-MM-DD."},
                        "cost":     {"type": "number", "description": "Cost."},
                        "location": {"type": "string", "description": "City or venue."},
                    },
                    "required": ["trip_id", "name", "date", "cost"],
                },
            },
        },
    },
    "book_customer_experience": {
        "required_scopes": ("book:experiences",),
        "fn": book_customer_experience,
        "schema": {
            "type": "function",
            "function": {
                "name": "book_customer_experience",
                "description": (
                    "Book a STANDALONE experience for a customer (no parent "
                    "trip). PRECONDITION — call search_experiences first, "
                    "present the numbered results, and wait for the user to "
                    "choose one AND a specific time slot before calling this "
                    "tool. Never auto-select an experience or time. Triggers "
                    "a CIBA push — agent must approve on their enrolled "
                    "device. Agents can only book for customers in their org."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "customer_id": {"type": "string", "description": "Customer ID (cu_xxx), email, or full name."},
                        "name":        {"type": "string", "description": "Experience name (e.g., 'Tuscan cooking class')."},
                        "date":        {"type": "string", "description": "Date YYYY-MM-DD."},
                        "cost":        {"type": "number", "description": "Cost."},
                        "location":    {"type": "string", "description": "City or venue."},
                    },
                    "required": ["customer_id", "name", "date", "cost"],
                },
            },
        },
    },
    "create_company": {
        "required_scopes": ("manage:companies",),
        "fn": create_company,
        "schema": {
            "type": "function",
            "function": {
                "name": "create_company",
                "description": "Add a local Compass0 organization record (mock data only — does NOT create the Auth0 organization). Admin-only. Prefer create_auth0_organization unless you specifically need a local-only entry.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "org_name":     {"type": "string", "description": "Auth0 org slug (lowercase, dashes)."},
                        "display_name": {"type": "string", "description": "Pretty organization name."},
                        "budget":       {"type": "number", "description": "Annual travel budget."},
                        "currency":     {"type": "string", "description": "ISO currency code. Default USD."},
                    },
                    "required": ["org_name", "display_name", "budget"],
                },
            },
        },
    },
    "create_auth0_organization": {
        "required_scopes": ("manage:companies",),
        "fn": create_auth0_organization,
        "schema": {
            "type": "function",
            "function": {
                "name": "create_auth0_organization",
                "description": (
                    "Create a real Auth0 Organization via the Management API "
                    "AND mirror it into the local Compass0 organization list. "
                    "Use this whenever an admin says 'create an organization', "
                    "'add a new customer org', 'spin up a tenant for "
                    "Acme', etc. Admin-only. Triggers a CIBA push to the "
                    "admin's enrolled device — they must approve before the "
                    "org is created."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name":         {"type": "string", "description": "Org slug — lowercase, dashes only, no spaces (e.g. 'acme-inc')."},
                        "display_name": {"type": "string", "description": "Pretty organization name shown in the UI (e.g. 'Acme Inc')."},
                        "budget":       {"type": "number", "description": "Optional annual travel budget for the local mirror. Default 100000."},
                        "currency":     {"type": "string", "description": "Optional ISO currency code. Default USD."},
                    },
                    "required": ["name", "display_name"],
                },
            },
        },
    },
    "delete_auth0_organization": {
        "required_scopes": ("manage:companies",),
        "fn": delete_auth0_organization,
        "schema": {
            "type": "function",
            "function": {
                "name": "delete_auth0_organization",
                "description": (
                    "Delete an Auth0 Organization via the Management API and "
                    "remove the matching local Compass0 organization. Use when "
                    "an admin says 'delete the org for Acme', 'remove the "
                    "organization X', etc. Admin-only. Triggers a CIBA "
                    "push to the admin's device — they must approve the "
                    "deletion before it executes. Confirm with the user "
                    "before calling this."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Org slug to delete (e.g. 'acme-inc')."},
                    },
                    "required": ["name"],
                },
            },
        },
    },
    "create_travel_agent": {
        "required_scopes": ("manage:companies",),
        "fn": create_travel_agent,
        "schema": {
            "type": "function",
            "function": {
                "name": "create_travel_agent",
                "description": (
                    "Create a new travel agent inside an existing Auth0 "
                    "organization. Use this whenever an admin says 'add a "
                    "travel agent', 'create a new agent for Acme', 'invite "
                    "a new agent', etc. Creates a real Auth0 user, adds "
                    "them to the organization, and assigns the travel_agent "
                    "role. Admin-only. Triggers a CIBA push to the admin's "
                    "device — they must approve. IMPORTANT: admins manage "
                    "organizations and travel agents only. Travel agents "
                    "(not admins) add CUSTOMERS — never try to add a "
                    "customer for an admin."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "org_name": {"type": "string", "description": "Org slug of an existing Auth0 organization (e.g. 'acme-inc')."},
                        "name":     {"type": "string", "description": "Agent's full name."},
                        "email":    {"type": "string", "description": "Agent's email address — used as their Auth0 login."},
                    },
                    "required": ["org_name", "name", "email"],
                },
            },
        },
    },
    "delete_travel_agent": {
        "required_scopes": ("manage:companies",),
        "fn": delete_travel_agent,
        "schema": {
            "type": "function",
            "function": {
                "name": "delete_travel_agent",
                "description": (
                    "Remove a travel agent from an Auth0 organization and "
                    "delete the underlying Auth0 user. Use whenever an admin "
                    "says 'remove travel agent X', 'fire/offboard agent for "
                    "Acme', 'delete agent y@z.com', etc. Admin-only. "
                    "Triggers a CIBA push — the admin must approve. This "
                    "fully deletes the Auth0 user account; if you only want "
                    "to remove them from the org, ask the admin to clarify "
                    "before calling. Always confirm the agent's email and "
                    "org with the user before invoking."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "org_name": {"type": "string", "description": "Org slug the agent belongs to (e.g. 'acme-inc')."},
                        "email":    {"type": "string", "description": "Agent's email address (used to find the Auth0 user)."},
                    },
                    "required": ["org_name", "email"],
                },
            },
        },
    },
    "generate_contract": {
        "required_scopes": ("manage:companies",),
        "fn": generate_contract,
        "schema": {
            "type": "function",
            "function": {
                "name": "generate_contract",
                "description": (
                    "Generate a Compass0 ↔ organization services agreement "
                    "PDF and store it in the documents list. Use whenever an "
                    "admin says 'generate a contract for X', 'draft a "
                    "Compass0 contract for org Y', 'I just created an "
                    "organization, make the paperwork', etc. Admin-only. The "
                    "organization must already exist (create it first via "
                    "create_auth0_organization). Does NOT trigger CIBA — it's "
                    "a low-risk document creation, not an Auth0 mutation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "org_name": {"type": "string", "description": "Org slug of an existing organization (e.g. 'acme-inc')."},
                    },
                    "required": ["org_name"],
                },
            },
        },
    },
    "request_trip": {
        "required_scopes": ("read:my_trips",),
        "fn": request_trip,
        "schema": {
            "type": "function",
            "function": {
                "name": "request_trip",
                "description": (
                    "Submit a trip request for agent approval. "
                    "PRECONDITION — you MUST call search_flights first and "
                    "show the customer the numbered results. Only call "
                    "request_trip AFTER the customer has explicitly chosen "
                    "one of the options from those results. Populate "
                    "origin, destination, depart_date, return_date, and "
                    "cost directly from the chosen search result — never "
                    "invent or default these values. If you have not yet "
                    "called search_flights in this conversation, do that "
                    "first instead of calling this tool."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type":         {"type": "string", "enum": ["flight", "hotel", "train"]},
                        "origin":       {"type": "string", "description": "Origin city or IATA code."},
                        "destination":  {"type": "string", "description": "Destination city or IATA code."},
                        "depart_date":  {"type": "string", "description": "Departure date YYYY-MM-DD."},
                        "return_date":  {"type": "string", "description": "Return date YYYY-MM-DD."},
                        "cost":         {"type": "number", "description": "Total cost in the requested currency."},
                        "currency":     {"type": "string", "description": "ISO currency code. Default USD."},
                    },
                    "required": ["type", "origin", "destination", "depart_date", "return_date", "cost"],
                },
            },
        },
    },
    "request_experience": {
        "required_scopes": ("read:my_trips",),
        "fn": request_experience,
        "schema": {
            "type": "function",
            "function": {
                "name": "request_experience",
                "description": (
                    "Submit a request for an experience (cooking class, wine "
                    "tasting, hike, etc.) that the customer's travel agent "
                    "will approve or deny. Use whenever a CUSTOMER says 'book "
                    "this experience for me', 'I want the Tuscan cooking "
                    "class', etc. Customers can't book directly; their agent "
                    "must approve."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name":     {"type": "string", "description": "Experience name."},
                        "date":     {"type": "string", "description": "Date YYYY-MM-DD."},
                        "cost":     {"type": "number", "description": "Cost."},
                        "location": {"type": "string", "description": "City or venue."},
                        "trip_id":  {"type": "string", "description": "Optional trip ID this experience would attach to."},
                    },
                    "required": ["name", "date", "cost"],
                },
            },
        },
    },
    "create_my_customer": {
        "required_scopes": ("book:trips",),
        "fn": create_my_customer,
        "schema": {
            "type": "function",
            "function": {
                "name": "create_my_customer",
                "description": (
                    "Create a new customer in the signed-in agent's own organization. "
                    "Use this whenever a travel agent says 'add a new customer', "
                    "'create a customer', etc. The org is auto-filled from the user's "
                    "token — never ask the user for org_name. Just collect name and email."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name":  {"type": "string", "description": "Customer's full name."},
                        "email": {"type": "string", "description": "Customer's email address."},
                    },
                    "required": ["name", "email"],
                },
            },
        },
    },
    "delete_customer": {
        "required_scopes": ("book:trips",),
        "fn": delete_customer,
        "schema": {
            "type": "function",
            "function": {
                "name": "delete_customer",
                "description": (
                    "Permanently remove a customer from the organization. "
                    "Strips the customer from the Auth0 organization, deletes "
                    "the Auth0 user account, and removes the local record. "
                    "Requires CIBA step-up (device approval). Agents can only "
                    "delete customers in their own org. Provide either "
                    "customer_id or email to identify the customer."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "customer_id": {
                            "type": "string",
                            "description": "Local customer ID (e.g. cu_001). Preferred if known.",
                        },
                        "email": {
                            "type": "string",
                            "description": "Customer email address. Used if customer_id is not available.",
                        },
                    },
                },
            },
        },
    },
    "search_experiences": {
        "required_scopes": (),
        "fn": search_experiences,
        "schema": {
            "type": "function",
            "function": {
                "name": "search_experiences",
                "description": (
                    "Browse a curated catalog of experiences (cooking classes, "
                    "wine tastings, hikes, day trips, food tours, cultural "
                    "outings). Filter by location and/or category, or call with "
                    "no args to see the whole catalog. IMPORTANT: this catalog "
                    "has no date-based availability — 'available_times' lists "
                    "recurring daily time slots that run every day; any future "
                    "date is valid. Never say an experience is unavailable "
                    "because of the date. After the user picks one: travel "
                    "agents call book_experience or book_customer_experience to "
                    "actually book; CUSTOMERS call request_experience instead — "
                    "they cannot book directly, their travel agent must approve."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "Optional city, region, or country to filter by (e.g., 'Florence', 'Japan')."},
                        "category": {"type": "string", "description": "Optional category. One of: cooking_class, wine_tasting, hike, day_trip, food_tour, cultural."},
                    },
                },
            },
        },
    },
    "search_flights": {
        "required_scopes": (),
        "fn": search_flights,
        "schema": {
            "type": "function",
            "function": {
                "name": "search_flights",
                "description": (
                    "Search available flights for a date and origin/destination pair. "
                    "Returns 3 mock flight options (airline, flight_no, depart/arrive "
                    "times, duration, stops, price). Use this whenever the user asks "
                    "to look for flights, find a flight, or compare flight options. "
                    "After the user picks one: travel agents call book_trip; "
                    "CUSTOMERS call request_trip instead — customers cannot book "
                    "directly, their travel agent has to approve the request first."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "origin":      {"type": "string", "description": "Origin city or IATA code (e.g., 'JFK', 'New York')."},
                        "destination": {"type": "string", "description": "Destination city or IATA code."},
                        "date":        {"type": "string", "description": "Departure date in YYYY-MM-DD."},
                    },
                    "required": ["origin", "destination", "date"],
                },
            },
        },
    },
    "get_trip_details": {
        "required_scopes": ("read:my_company",),
        "fn": get_trip_details,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_trip_details",
                "description": (
                    "Fetch full details for a single trip — the trip record, the "
                    "owning customer's name/email, and any booked experiences. "
                    "Use whenever the user asks for details on a specific trip. "
                    "Customers can only view their own; agents can view anything in "
                    "their org; admins can view any trip."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "trip_id": {"type": "string", "description": "Trip ID (tr_xxx)."},
                    },
                    "required": ["trip_id"],
                },
            },
        },
    },
}


def visible_schemas(ctx: dict) -> list[dict]:
    """Filter the tool catalog to only what the user has scope for.
    The model never sees the rest."""
    perms = ctx.get("permissions") or set()
    return [
        t["schema"]
        for t in TOOLS.values()
        if all(s in perms for s in t["required_scopes"])
    ]


async def dispatch(name: str, args: dict, ctx: dict) -> str:
    """Re-check permission server-side then invoke the tool."""
    tool = TOOLS.get(name)
    if not tool:
        return json.dumps({"error": f"unknown tool: {name}"})
    perms = ctx.get("permissions") or set()
    missing = [s for s in tool["required_scopes"] if s not in perms]
    if missing:
        return json.dumps(
            {
                "error": f"permission denied — missing {', '.join(missing)}",
                "your_role": ctx.get("role"),
            }
        )
    try:
        return await tool["fn"](args, ctx)
    except PermissionDenied as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
