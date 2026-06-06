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


async def create_customer(args: dict, ctx: dict) -> str:
    require(ctx, "manage:companies")
    customer = add_customer(
        name=args["name"],
        email=args["email"],
        org_name=args["org_name"],
        agent_id=args.get("agent_id"),
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
                "description": "Add a new company customer to CompassZero. Admin-only.",
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
