"""CompassZero chat tools — every tool is permission-gated.

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
    add_company,
    add_customer,
    add_experience,
    add_trip,
    get_companies,
    get_customer,
    get_customers,
    get_experiences_for_trip,
    get_trip,
    get_trips,
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
        # Generous timeout so the user has time to find their phone,
        # read the binding message, and tap Approve.
        await step_up(user_sub=sub, binding_message=binding_message, max_seconds=120)
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
                    "Tell the user the step-up prompt was denied or "
                    "timed out. Do NOT retry the action automatically — "
                    "ask them to confirm they want to try again."
                ),
                "stop_retrying": True,
            }
        )
    return None


async def book_trip(args: dict, ctx: dict) -> str:
    require(ctx, "book:trips")
    # Agents can only book for customers in their own org; admins can book for anyone.
    customer = get_customer(args["customer_id"])
    if not customer:
        return json.dumps({"error": f"unknown customer_id: {args['customer_id']}"})
    if not has_permission(ctx, "manage:companies"):
        if customer["org_name"] != ctx.get("org_name"):
            raise PermissionDenied(
                f"Customer {args['customer_id']} is not in your organization."
            )

    binding = (
        f"Approve: book {args['type']} "
        f"{args['origin']}→{args['destination']} {args['depart_date']}"
    )
    err = await _ciba_step_up(ctx, binding)
    if err:
        return err

    trip = add_trip(
        customer_id=args["customer_id"],
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

    binding = f"Approve: cancel trip {args['trip_id']}"
    err = await _ciba_step_up(ctx, binding)
    if err:
        return err

    trip["status"] = "cancelled"
    return json.dumps({"ok": True, "trip": trip})


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
    require(ctx, "book:trips")
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
    {"id": "exc_001", "name": "Tuscan cooking class with a local chef",   "category": "cooking_class", "location": "Florence",   "country": "Italy",        "duration": "4h",  "price": 145, "currency": "USD", "description": "Hands-on small-group class — fresh pasta, sauces, tiramisu — in a 16th-century farmhouse."},
    {"id": "exc_002", "name": "Tokyo sushi-making workshop",              "category": "cooking_class", "location": "Tokyo",      "country": "Japan",        "duration": "3h",  "price": 110, "currency": "USD", "description": "Learn nigiri and maki technique from a Tsukiji-trained chef in a private kitchen."},
    {"id": "exc_003", "name": "Bordeaux château wine tasting",            "category": "wine_tasting",  "location": "Bordeaux",   "country": "France",       "duration": "5h",  "price": 220, "currency": "USD", "description": "Visit two grand cru estates with a sommelier guide; six wines, lunch included."},
    {"id": "exc_004", "name": "Napa Valley wine tour with vineyard lunch","category": "wine_tasting",  "location": "Napa",       "country": "USA",          "duration": "6h",  "price": 285, "currency": "USD", "description": "Three-vineyard tour by private van with a farm-to-table lunch on the second stop."},
    {"id": "exc_005", "name": "Mt. Fuji day hike",                        "category": "hike",          "location": "Mt. Fuji",   "country": "Japan",        "duration": "10h", "price": 180, "currency": "USD", "description": "Yoshida-trail summit hike with a certified guide; gear and bus transfers included."},
    {"id": "exc_006", "name": "Cinque Terre coastal hike",                "category": "hike",          "location": "Cinque Terre","country": "Italy",       "duration": "7h",  "price": 95,  "currency": "USD", "description": "Guided trek across the five villages on the high path, with a focaccia stop in Vernazza."},
    {"id": "exc_007", "name": "Day trip to Versailles",                   "category": "day_trip",      "location": "Versailles", "country": "France",       "duration": "8h",  "price": 130, "currency": "USD", "description": "Round-trip from Paris with skip-the-line palace + gardens entry and an art historian guide."},
    {"id": "exc_008", "name": "Stonehenge & Bath day trip",               "category": "day_trip",      "location": "Wiltshire",  "country": "United Kingdom","duration": "10h","price": 165, "currency": "USD", "description": "Coach from London with inner-circle Stonehenge access and a walking tour of Bath's Roman baths."},
    {"id": "exc_009", "name": "Barcelona tapas & wine walk",              "category": "food_tour",     "location": "Barcelona",  "country": "Spain",        "duration": "3h",  "price": 95,  "currency": "USD", "description": "Five-stop tapas crawl through the Gothic Quarter with wine pairings at each."},
    {"id": "exc_010", "name": "Marrakech medina & souks tour",            "category": "cultural",      "location": "Marrakech",  "country": "Morocco",      "duration": "4h",  "price": 75,  "currency": "USD", "description": "Walking tour of the medina with a local guide; mint tea at a riad to finish."},
    {"id": "exc_011", "name": "Iceland glacier hike on Sólheimajökull",   "category": "hike",          "location": "Sólheimajökull","country": "Iceland",   "duration": "5h",  "price": 195, "currency": "USD", "description": "Guided ice-axe hike with crampons and helmets provided; transfer from Reykjavík available."},
    {"id": "exc_012", "name": "West End theatre night, London",           "category": "cultural",      "location": "London",     "country": "United Kingdom","duration": "3h", "price": 140, "currency": "USD", "description": "Premium-stalls ticket to a current West End show plus pre-theatre dinner reservation."},
]

_EXPERIENCE_CATEGORIES = sorted({e["category"] for e in _EXPERIENCE_CATALOG})


async def search_experiences(args: dict, ctx: dict) -> str:
    require(ctx, "book:experiences")
    location = (args.get("location") or "").strip().lower()
    category = (args.get("category") or "").strip().lower()

    out = _EXPERIENCE_CATALOG
    if location:
        out = [e for e in out if location in e["location"].lower() or location in e["country"].lower()]
    if category:
        out = [e for e in out if e["category"] == category]
    return json.dumps(
        {
            "categories": _EXPERIENCE_CATEGORIES,
            "match_count": len(out),
            "experiences": out,
        }
    )


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
    it into the local CompassZero company list so the dashboard reflects it."""
    require(ctx, "manage:companies")
    from .auth0_management import ManagementError, create_organization

    name = (args.get("name") or "").strip()
    display_name = (args.get("display_name") or name).strip()
    if not name:
        return json.dumps({"error": "name is required (lowercase slug, no spaces)."})

    binding = f"Approve: create org '{name}'"
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
    corresponding entry from the local CompassZero company list."""
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

    binding = f"Approve: DELETE org '{name}'"
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


async def create_customer(args: dict, ctx: dict) -> str:
    require(ctx, "manage:companies")
    customer = add_customer(
        name=args["name"],
        email=args["email"],
        org_name=args["org_name"],
        agent_id=args.get("agent_id"),
    )
    return json.dumps({"ok": True, "customer": customer})


async def create_my_customer(args: dict, ctx: dict) -> str:
    require(ctx, "book:trips")
    org_name = ctx.get("org_name")
    if not org_name:
        return json.dumps(
            {"error": "no org_name on your token — log in via your travel agency's organization"}
        )
    customer = add_customer(
        name=args["name"],
        email=args["email"],
        org_name=org_name,
        agent_id=ctx.get("agent_id"),
    )
    return json.dumps({"ok": True, "customer": customer})


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
                "description": "List all bookings inside the signed-in agent's company. Use for 'all our customers' trips', 'recent bookings for our company', etc.",
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
                "description": "List bookings across all CompassZero companies. Admin-only.",
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
                "description": "List every customer across all CompassZero companies. Admin-only.",
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
                "description": "List every CompassZero company customer with budget vs. spent. Admin-only.",
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
                "description": "Book a new trip (flight / hotel / train) for a customer. Agents can only book for customers in their own org; admins can book for anyone. Always confirm details with the user before calling.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "customer_id":  {"type": "string", "description": "Customer ID (cu_xxx). Use list_my_customers to find IDs."},
                        "type":         {"type": "string", "enum": ["flight", "hotel", "train"]},
                        "origin":       {"type": "string", "description": "Origin city or IATA code."},
                        "destination":  {"type": "string", "description": "Destination city or IATA code."},
                        "depart_date":  {"type": "string", "description": "Departure date YYYY-MM-DD."},
                        "return_date":  {"type": "string", "description": "Return date YYYY-MM-DD."},
                        "cost":         {"type": "number", "description": "Total cost in the company's currency."},
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
    "book_experience": {
        "required_scopes": ("book:experiences",),
        "fn": book_experience,
        "schema": {
            "type": "function",
            "function": {
                "name": "book_experience",
                "description": "Add an experience (tour / activity / dinner reservation) to an existing trip. Agents can only book for trips owned by customers in their org.",
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
    "create_company": {
        "required_scopes": ("manage:companies",),
        "fn": create_company,
        "schema": {
            "type": "function",
            "function": {
                "name": "create_company",
                "description": "Add a local CompassZero company record (mock data only — does NOT create the Auth0 organization). Admin-only. Prefer create_auth0_organization unless you specifically need a local-only entry.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "org_name":     {"type": "string", "description": "Auth0 org slug (lowercase, dashes)."},
                        "display_name": {"type": "string", "description": "Pretty company name."},
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
                    "AND mirror it into the local CompassZero company list. "
                    "Use this whenever an admin says 'create an organization', "
                    "'add a new company / customer org', 'spin up a tenant for "
                    "Acme', etc. Admin-only. Triggers a CIBA push to the "
                    "admin's enrolled device — they must approve before the "
                    "org is created."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name":         {"type": "string", "description": "Org slug — lowercase, dashes only, no spaces (e.g. 'acme-inc')."},
                        "display_name": {"type": "string", "description": "Pretty company name shown in the UI (e.g. 'Acme Inc')."},
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
                    "remove the matching local CompassZero company. Use when "
                    "an admin says 'delete the org for Acme', 'remove the "
                    "company customer X', etc. Admin-only. Triggers a CIBA "
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
    "create_customer": {
        "required_scopes": ("manage:companies",),
        "fn": create_customer,
        "schema": {
            "type": "function",
            "function": {
                "name": "create_customer",
                "description": "Create a new customer record under an existing company. Admin-only.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name":     {"type": "string"},
                        "email":    {"type": "string"},
                        "org_name": {"type": "string", "description": "Auth0 org slug of the company."},
                        "agent_id": {"type": "string", "description": "Optional travel agent ID (ag_xxx)."},
                    },
                    "required": ["name", "email", "org_name"],
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
    "search_experiences": {
        "required_scopes": ("book:experiences",),
        "fn": search_experiences,
        "schema": {
            "type": "function",
            "function": {
                "name": "search_experiences",
                "description": (
                    "Browse a curated catalog of experiences (cooking classes, "
                    "wine tastings, hikes, day trips, food tours, cultural "
                    "outings) the agent can add to a customer's trip. Filter "
                    "by location and/or category, or call with no args to "
                    "see the whole catalog. After the user picks one, call "
                    "book_experience with the chosen experience's name, the "
                    "trip_id, a date inside the trip's window, the price as "
                    "cost, and the location."
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
        "required_scopes": ("book:trips",),
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
                    "After the user picks one, call book_trip with the chosen flight's "
                    "origin, destination, date, and price."
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
