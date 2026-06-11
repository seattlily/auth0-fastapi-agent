import base64
import json
import os
import secrets
import time

from auth0_fastapi.auth.auth_client import AuthClient
from auth0_fastapi.config import Auth0Config
from auth0_fastapi.server.routes import register_auth_routes
from auth0_fastapi.server.routes import router as auth_router
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openai import AsyncOpenAI
from starlette.middleware.sessions import SessionMiddleware

from mock_data import (
    COMPANIES,
    DOCUMENTS,
    EXPERIENCES,
    TRAVEL_AGENTS,
    TRIPS,
    add_company,
    add_document,
    add_experience,
    add_trip,
    get_agent,
    get_agents,
    get_approval_request,
    get_approval_requests,
    get_companies,
    get_company,
    get_customer,
    get_customers,
    get_document,
    get_documents,
    get_experiences_for_trip,
    get_trip,
    get_trips,
    update_approval_request,
)
from permissions import (
    PermissionDenied,
    get_user_context,
    has_any_permission,
    has_permission,
)
from tools.auth0_ciba import CibaError, CibaNotEnrolledError, step_up
from tools.auth0_management import (
    ManagementError,
    create_enrollment_ticket,
    create_organization,
    delete_organization,
    get_organization_by_name,
    list_organization_members,
    list_user_enrollments,
    list_user_organizations,
    reconcile_companies_with_auth0,
    sync_status,
)
from tools.auth0_my_account import (
    MyAccountError,
    complete_connect,
    delete_account,
    initiate_connect,
    list_accounts,
    mint_my_account_token,
)
from tools.compasszero import TOOLS as CZ_TOOLS
from tools.compasszero import dispatch as cz_dispatch
from tools.compasszero import visible_schemas as cz_visible_schemas
from tools.documents import (
    documents_dir,
    generate_contract_pdf,
    generate_invoice_pdf,
)
from tools.google_calendar import (
    CALENDAR_TOOL_SCHEMA,
    CREATE_CALENDAR_EVENT_TOOL_SCHEMA,
    TokenVaultError,
    create_calendar_event,
    list_upcoming_calendar_events,
)
from tools.google_gmail import GMAIL_LIST_TOOL_SCHEMA, list_recent_emails

MAX_TOOL_ITERATIONS = 4

# Chat tools that block on a CIBA step-up. The chat stream surfaces
# a "approve on your device" notice before dispatch so the user
# knows where the latency is coming from.
# Per-org visual overrides — when set, the user's templates render with
# a different primary/accent and an org-supplied logo next to the brand
# mark. Demonstrates per-tenant theming on top of Auth0 Organizations.
# Keys may be either Auth0 `org_id` (e.g. "org_aGUHzOkqG9Volr3d") or
# the org slug `org_name` (e.g. "globex-ltd"). The runtime lookup
# checks org_id first, then org_name — so a recreated Auth0 org with
# a new id still finds its theme as long as one of them matches.
BRAND_OVERRIDES: dict[str, dict[str, str]] = {
    "globex-ltd": {
        "primary": "#9dd600",
        "primary_dark": "#7db300",
        "primary_soft": "#eefadf",
        "secondary": "#7dc2d8",
        "secondary_soft": "#e6f4f9",
        "logo_url": (
            "https://img.magnific.com/free-vector/"
            "globe-grid-earth_78370-7981.jpg?w=360"
        ),
    },
    "northwind-corp": {
        "primary": "#030a2b",
        "primary_dark": "#020618",
        "primary_soft": "#e6e8ee",
        "secondary": "#565252",
        "secondary_soft": "#ececec",
        "logo_url": (
            "https://media.istockphoto.com/id/1127367066/vector/"
            "north-arrow-icon-or-n-direction-and-navigation-point-"
            "symbol-vector-logo-in-circle-for-gps.jpg"
            "?s=612x612&w=0&k=20&c=ynSV8xSAVPeGXRthPnrfuezFd7BGNJ0okpiEjdY5H00="
        ),
    },
    "org_aGUHzOkqG9Volr3d": {
        "primary": "#242b61",
        "primary_dark": "#1a2049",
        "primary_soft": "#e8eaf3",
        "secondary": "#e4dddd",
        "secondary_soft": "#f1ecec",
        "logo_url": (
            "https://i.fbcd.co/products/original/"
            "logo-88d7008f5b8d759cec9c792bc69657ebd6ce2c9c336c183e9c262defb7d5e2d3.jpg"
        ),
    },
}


CIBA_GATED_CHAT_TOOLS = {
    "book_trip",
    "book_customer_experience",
    "cancel_trip",
    "create_auth0_organization",
    "create_travel_agent",
    "delete_auth0_organization",
    "delete_travel_agent",
}


def _short_arg(value, max_len: int = 120) -> str:
    if isinstance(value, (dict, list)):
        s = json.dumps(value, ensure_ascii=False, default=str)
    else:
        s = str(value)
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _summarize_tool_result(name: str, result: str) -> str:
    """One-line, human-readable summary of what the tool returned, for
    the chat status feed. Returns an empty string when nothing useful
    is parseable."""
    try:
        data = json.loads(result)
    except Exception:
        return ""

    if isinstance(data, dict):
        if "error" in data:
            return f"error: {_short_arg(data['error'], max_len=160)}"
        # write tools — surface the created object's id / name
        if data.get("ok"):
            for key in (
                "trip", "agent", "customer", "auth0_user", "experience",
                "document", "request", "auth0_org", "company",
            ):
                obj = data.get(key)
                if isinstance(obj, dict):
                    obj_id = (
                        obj.get("id")
                        or obj.get("user_id")
                        or obj.get("name")
                        or obj.get("filename")
                    )
                    return f"{key}{f' {obj_id}' if obj_id else ''}"
            if "deleted_user_id" in data:
                return f"deleted user {data['deleted_user_id']}"
            if "removed_from_org" in data:
                return f"removed from {data['removed_from_org']}"
            return "ok"
        # search/list results — count what came back
        for key in ("flights", "experiences", "matches", "results"):
            if isinstance(data.get(key), list):
                return f"{len(data[key])} {key}"
        if "match_count" in data:
            return f"{data['match_count']} matches"

    if isinstance(data, list):
        return f"{len(data)} items"

    return ""


def _tool_status_badge(name: str) -> str:
    """Tag the tool with what permission tier / Auth0 surface it hits,
    so the operator can see at a glance why a call is taking time."""
    tags: list[str] = []
    if name in CIBA_GATED_CHAT_TOOLS:
        tags.append("CIBA")
    if name in {
        "create_auth0_organization", "delete_auth0_organization",
        "create_travel_agent", "delete_travel_agent",
    }:
        tags.append("Mgmt API")
    if name in GOOGLE_TOOLS_BY_NAME:
        tags.append("Token Vault")
    return f" `{' · '.join(tags)}`" if tags else ""


def _build_bookings(trips: list[dict], experiences: list[dict]) -> list[dict]:
    """Merge trips and standalone/attached experiences into one
    chronological feed for the dashboard's recent-bookings table."""
    bookings: list[dict] = []
    for t in trips:
        bookings.append(
            {
                "id": t["id"],
                "kind": "trip",
                "type": t["type"],
                "customer_id": t["customer_id"],
                "summary": f"{t['origin']} → {t['destination']}",
                "primary_date": t["depart_date"],
                "date_label": f"{t['depart_date']} – {t['return_date']}",
                "cost": t["cost"],
                "currency": t["currency"],
                "status": t["status"],
                "link": f"/trips/{t['id']}",
                "location": "",
            }
        )
    for e in experiences:
        bookings.append(
            {
                "id": e["id"],
                "kind": "experience",
                "type": "activity",
                "customer_id": e["customer_id"],
                "summary": e["name"],
                "primary_date": e["date"],
                "date_label": e["date"],
                "cost": e["cost"],
                "currency": "USD",
                "status": "booked",
                "link": (f"/trips/{e['trip_id']}" if e.get("trip_id") else None),
                "location": e.get("location", ""),
            }
        )
    bookings.sort(key=lambda b: b["primary_date"], reverse=True)
    return bookings
GOOGLE_CONNECTION_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
]

load_dotenv(override=True)

app = FastAPI(title="CompassZero")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["APP_SECRET_KEY"],
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
# Cache-bust static assets on every restart so template/CSS changes
# show up without needing a hard refresh.
templates.env.globals["static_version"] = str(int(time.time()))
# Optional Auth0 connection name for staff / admin SSO (e.g. "Okta").
# When set, the home page shows a secondary "Sign in via {name}"
# link that bypasses the connection picker and hits the IdP
# directly — no org param, so the admin lands without org context.
templates.env.globals["admin_connection_name"] = os.environ.get(
    "ADMIN_CONNECTION_NAME", ""
)
templates.env.globals["admin_connection_label"] = os.environ.get(
    "ADMIN_CONNECTION_LABEL", "Okta"
)


# ---------- Auth0 SDK setup ----------

_auth0_kwargs = {
    "domain": os.environ["AUTH0_DOMAIN"],
    "client_id": os.environ["AUTH0_CLIENT_ID"],
    "client_secret": os.environ["AUTH0_CLIENT_SECRET"],
    "app_base_url": os.environ.get("APP_BASE_URL", "http://localhost:8000"),
    "secret": os.environ["APP_SECRET_KEY"],
    "authorization_params": {
        "scope": (
            "openid profile email offline_access "
            "create:me:connected_accounts "
            "read:me:connected_accounts "
            "delete:me:connected_accounts"
        ),
    },
}
if os.environ.get("AUTH0_AUDIENCE"):
    _auth0_kwargs["audience"] = os.environ["AUTH0_AUDIENCE"]

auth0_config = Auth0Config(**_auth0_kwargs)
auth_client = AuthClient(auth0_config)


def _relax_cookies_for_local_http(client) -> None:
    """SDK marks cookies as Secure by default — browsers drop them on http://localhost.
    Fix the state cookie via cookie_options and patch the transaction store's set()."""
    import types

    state_store = getattr(client, "_state_store", None)
    if state_store is not None and hasattr(state_store, "cookie_options"):
        state_store.cookie_options["secure"] = False
        state_store.cookie_options["samesite"] = "lax"

    transaction_store = getattr(client, "_transaction_store", None)
    if transaction_store is not None:

        async def _set_no_secure(self, identifier, value, options=None):
            if options is None or "response" not in options:
                raise ValueError("Response object is required in store options.")
            response = options["response"]
            encrypted_value = self.encrypt(identifier, value.model_dump())
            # 10-minute TTL so MFA / Guardian enrollment / SSO redirects
            # have plenty of time before the transaction cookie expires
            # and the callback fails state validation.
            response.set_cookie(
                key=self.cookie_name,
                value=encrypted_value,
                path="/",
                samesite="lax",
                secure=False,
                httponly=True,
                max_age=600,
            )

        transaction_store.set = types.MethodType(_set_no_secure, transaction_store)


if os.environ.get("USE_SECURE_COOKIES", "").lower() not in ("1", "true", "yes"):
    _relax_cookies_for_local_http(auth_client.client)

app.state.config = auth0_config
app.state.auth_client = auth_client
register_auth_routes(auth_router, auth0_config)
app.include_router(auth_router)


openai_client = AsyncOpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
)
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")


# ---------- documents ----------


_UPLOAD_ALLOWED_EXT = {".pdf", ".docx", ".txt"}
_UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


def _ensure_seed_documents() -> None:
    """Generate any missing contract / invoice PDFs and DOCUMENTS entries
    on demand. Idempotent — safe to call from /dashboard and /documents."""
    have_contracts = {
        d["org_name"] for d in DOCUMENTS if d["kind"] == "contract"
    }
    for company in COMPANIES:
        if company["org_name"] in have_contracts:
            continue
        out = documents_dir() / f"contract-{company['org_name']}.pdf"
        if not out.exists():
            generate_contract_pdf(
                org_name=company["org_name"],
                display_name=company["display_name"],
                output_path=out,
            )
        add_document(
            kind="contract",
            title=f"CompassZero × {company['display_name']} services agreement",
            filename=out.name,
            org_name=company["org_name"],
            size_bytes=out.stat().st_size,
        )

    have_invoices = {d["trip_id"] for d in DOCUMENTS if d["kind"] == "invoice"}
    for trip in TRIPS:
        if trip["id"] in have_invoices:
            continue
        customer = get_customer(trip["customer_id"])
        if not customer:
            continue
        company = get_company(org_name=customer.get("org_name", ""))
        out = documents_dir() / f"invoice-{trip['id']}.pdf"
        if not out.exists():
            generate_invoice_pdf(
                trip=trip, customer=customer, company=company, output_path=out
            )
        add_document(
            kind="invoice",
            title=f"Invoice {trip['id'].upper().replace('TR_', 'INV-')} · {customer['name']}",
            filename=out.name,
            org_name=customer.get("org_name", ""),
            customer_id=customer["id"],
            trip_id=trip["id"],
            size_bytes=out.stat().st_size,
        )


def _docs_visible_to(ctx: dict) -> list[dict]:
    role = ctx.get("role")
    if role == "compass_admin":
        return list(DOCUMENTS)
    if role == "travel_agent":
        org = ctx.get("org_name") or ""
        return [d for d in DOCUMENTS if d.get("org_name") == org]
    if role == "customer":
        cid = ctx.get("customer_id") or ""
        return [
            d
            for d in DOCUMENTS
            if d["kind"] == "invoice" and d.get("customer_id") == cid
        ]
    return []


def _user_can_view_doc(ctx: dict, doc: dict) -> bool:
    role = ctx.get("role")
    if role == "compass_admin":
        return True
    if role == "travel_agent":
        return doc.get("org_name") == ctx.get("org_name")
    if role == "customer":
        return (
            doc["kind"] == "invoice"
            and doc.get("customer_id") == ctx.get("customer_id")
        )
    return False


# ---------- helpers ----------


def _store_options(request: Request, response: Response) -> dict:
    return {"request": request, "response": response}


async def _get_session(request: Request, response: Response) -> dict | None:
    return await auth_client.client.get_session(
        store_options=_store_options(request, response)
    )


async def _get_user(request: Request, response: Response) -> dict | None:
    return await auth_client.client.get_user(
        store_options=_store_options(request, response)
    )


def _tokens_from_session(session: dict | None) -> tuple[str, str]:
    s = session or {}
    refresh_token = s.get("refresh_token") or ""
    token_sets = s.get("token_sets") or []
    access_token = ""
    if token_sets:
        access_token = token_sets[0].get("access_token", "") or ""
    return access_token, refresh_token


def decode_jwt_claims(token: str) -> dict:
    try:
        parts = (token or "").split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def decode_jwt_header(token: str) -> dict:
    try:
        if not token or "." not in token:
            return {}
        header = (token or "").split(".")[0]
        header += "=" * (-len(header) % 4)
        return json.loads(base64.urlsafe_b64decode(header))
    except Exception:
        return {}


def classify_token(token: str) -> str:
    if not token:
        return "empty"
    dots = token.count(".")
    if dots == 2:
        return "jws"
    if dots == 4:
        return "jwe"
    return "opaque"


async def require_login(request: Request, response: Response) -> tuple[dict, dict, dict]:
    """Returns (user, session, ctx) or raises by returning a redirect via the caller.
    Caller checks `if user is None: return RedirectResponse('/auth/login')`."""
    user = await _get_user(request, response)
    session = await _get_session(request, response) or {}
    access_token, _ = _tokens_from_session(session)
    access_claims = decode_jwt_claims(access_token)
    ctx = get_user_context(access_claims, user or {})

    # Resolve the user's company so templates can show the company
    # display_name as the primary brand instead of CompassZero.
    org = ctx.get("org_name")
    if org:
        company = get_company(org_name=org)
        if company:
            ctx["company_display_name"] = company["display_name"]
    brand = (
        BRAND_OVERRIDES.get(ctx.get("org_id") or "")
        or BRAND_OVERRIDES.get(ctx.get("org_name") or "")
    )
    if brand:
        ctx["brand"] = brand

    # Per-user app-state isolation: Starlette's SessionMiddleware cookie
    # is independent of the SDK's session, so app state (conversation,
    # pending_connect, cached org memberships) survives a logout. Reset
    # whenever the signed-in user changes.
    sub = (user or {}).get("sub") or ""
    if request.session.get("conversation_owner") != sub:
        request.session["conversation_owner"] = sub
        request.session["conversation"] = []
        request.session.pop("pending_connect", None)
        request.session.pop("user_organizations", None)

    # Multi-org membership lookup, cached on the session so we hit
    # the Management API once per login. Surfaces the top-nav
    # switcher when the user belongs to 2+ Auth0 Organizations.
    if user and "user_organizations" not in request.session:
        try:
            request.session["user_organizations"] = (
                await list_user_organizations(sub) if sub else []
            )
        except ManagementError:
            request.session["user_organizations"] = []
    ctx["user_organizations"] = request.session.get("user_organizations", []) or []

    return user, session, ctx


def build_system_prompt(user: dict | None, ctx: dict) -> str:
    profile = {
        "name": (user or {}).get("name"),
        "email": (user or {}).get("email"),
        "role": ctx.get("role"),
        "org_name": ctx.get("org_name"),
        "customer_id": ctx.get("customer_id"),
        "agent_id": ctx.get("agent_id"),
        "permissions": sorted(ctx.get("permissions") or []),
    }
    profile = {k: v for k, v in profile.items() if v not in (None, "", [], {})}

    return (
        "You are the CompassZero AI assistant — CompassZero is a B2B travel "
        "platform. Your tools are filtered to match the signed-in user's "
        "Auth0 permissions, so only call what's available to you. Use the "
        "user profile below to personalize answers and decide which tool to "
        "call.\n\n"
        "Be action-oriented. When the user asks for something doable (book a "
        "trip, create a record, list their stuff), take the action right away "
        "using sensible defaults instead of running a multi-question intake. "
        "If a detail is missing, pick a reasonable default, do the action, "
        "and tell the user what you assumed — they can refine in a follow-up. "
        "Resolve IDs yourself: never ask a user to type a customer_id, "
        "trip_id, or org_name. Call the appropriate list_* tool first to "
        "look up the ID, then proceed. For travel agents booking a trip with "
        "no customer specified, list your customers and pick the first one "
        "as the default. For dates, default to depart ~2 weeks out and "
        "return ~5–7 days later. For cost, ~1500 USD. For type, 'flight'. "
        "After the write succeeds, summarize what you did in one short line "
        "and offer to change any field. Only ask a clarifying question when "
        "the request itself is genuinely ambiguous (e.g., 'fix the trip' — "
        "which trip?). If a user asks for something outside their role, "
        "politely explain what they can do instead.\n\n"
        "Roles are distinct: admins (compass_admin) manage organizations and "
        "travel agents only; travel agents manage customers and bookings. If "
        "an admin asks to add a customer, decline politely and offer to add "
        "a travel agent (use create_travel_agent) or a new organization "
        "instead — never repurpose another tool to create a customer for an "
        "admin.\n\n"
        "Booking approvals: customers cannot book directly. When a customer "
        "asks to 'book' something, route them to request_trip / "
        "request_experience instead — that creates a pending request a "
        "travel agent must approve from their dashboard. After calling "
        "request_*, tell the customer their agent will review the request. "
        "Travel agents review and approve / deny pending requests on their "
        "dashboard, not via chat tools.\n\n"
        f"User profile:\n{json.dumps(profile, indent=2, default=str)}"
    )


# Calendar / Gmail tools — available to anyone with at least their own
# trip-read scope. Agents/admins get full access; customers get them so
# they can add their own trips to their own Google Calendar.
GOOGLE_TOOL_PERMISSIONS = ("book:trips", "read:my_trips")
GOOGLE_TOOLS_BY_NAME = {
    "list_upcoming_calendar_events": (CALENDAR_TOOL_SCHEMA, list_upcoming_calendar_events),
    "create_calendar_event": (CREATE_CALENDAR_EVENT_TOOL_SCHEMA, create_calendar_event),
    "list_recent_emails": (GMAIL_LIST_TOOL_SCHEMA, list_recent_emails),
}


def _can_use_google_tools(ctx: dict) -> bool:
    return has_any_permission(ctx, *GOOGLE_TOOL_PERMISSIONS)


def visible_google_schemas(ctx: dict) -> list[dict]:
    if not _can_use_google_tools(ctx):
        return []
    return [s for s, _ in GOOGLE_TOOLS_BY_NAME.values()]


async def dispatch_google_tool(name: str, args: dict, refresh_token: str) -> str:
    schema_fn = GOOGLE_TOOLS_BY_NAME.get(name)
    if not schema_fn:
        return None  # signal "not a Google tool"
    _, fn = schema_fn
    if name == "list_upcoming_calendar_events":
        return await fn(
            refresh_token=refresh_token,
            days=int(args.get("days", 7)),
            max_results=int(args.get("max_results", 5)),
        )
    if name == "create_calendar_event":
        return await fn(
            refresh_token=refresh_token,
            summary=args["summary"],
            start=args["start"],
            end=args["end"],
            description=args.get("description", ""),
            location=args.get("location", ""),
            attendees=args.get("attendees") or None,
        )
    if name == "list_recent_emails":
        return await fn(
            refresh_token=refresh_token,
            max_results=int(args.get("max_results", 5)),
            query=args.get("query", ""),
        )
    return None


# ---------- pages ----------


@app.get("/")
async def home(request: Request, response: Response):
    user = await _get_user(request, response)
    if user:
        return RedirectResponse(url="/dashboard")
    # Logged out — drop any leftover chat history so the next sign-in starts fresh.
    request.session.pop("conversation", None)
    request.session.pop("conversation_owner", None)
    return templates.TemplateResponse(request=request, name="home.html")


@app.get("/connect/google-calendar")
async def connect_google_calendar(request: Request):
    from urllib.parse import urlencode

    params = {
        "connection": "google-oauth2",
        "connection_scope": " ".join(GOOGLE_CONNECTION_SCOPES),
    }
    return RedirectResponse(url=f"/auth/login?{urlencode(params)}")


@app.get("/switch-org/{org_id}")
async def switch_org(request: Request, org_id: str):
    """Re-auth into a different Auth0 Organization.

    The SDK's `/auth/login` route forwards arbitrary query params to
    `/authorize`, so passing `organization=<id>` here lands the user
    on Auth0's universal login scoped to that org. If they have an
    active SSO session, Auth0 silently re-issues a token with the
    new `org_id` / `org_name` claims; otherwise they re-enter
    credentials against that org's connections.

    Guards: caller must already be logged in, and `org_id` must be
    one of the user's cached memberships — never trust the path
    parameter to grant access to an org the user isn't in.
    """
    user = await _get_user(request, Response())
    if not user:
        return RedirectResponse(url="/auth/login")

    memberships = request.session.get("user_organizations") or []
    member_ids = {o.get("id") for o in memberships if o.get("id")}
    if org_id not in member_ids:
        return RedirectResponse(url="/dashboard")

    # Conversation is scoped to whichever org context the user is
    # acting in — clear it on a switch so the next org starts fresh.
    request.session["conversation"] = []
    return RedirectResponse(url=f"/auth/login?organization={org_id}")


@app.get("/dashboard")
async def dashboard(request: Request, response: Response):
    user, session, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")

    role = ctx["role"]
    visible_tools = [s["function"]["name"] for s in cz_visible_schemas(ctx)] + [
        s["function"]["name"] for s in visible_google_schemas(ctx)
    ]

    # Surface a one-shot enrollment nudge for any role whose actions
    # trigger CIBA — admins (org create/delete) and travel agents
    # (book/cancel trip). Customers don't have CIBA-gated actions, so
    # skip the lookup for them.
    needs_enrollment = False
    if role in ("compass_admin", "travel_agent"):
        user_sub = ctx.get("sub") or user.get("sub")
        if user_sub:
            try:
                needs_enrollment = not await list_user_enrollments(user_sub)
            except ManagementError:
                pass

    common = {
        "user": user,
        "ctx": ctx,
        "messages": request.session.get("conversation", []),
        "visible_tools": visible_tools,
        "needs_enrollment": needs_enrollment,
        "flash_success": request.query_params.get("success"),
        "flash_error": request.query_params.get("error"),
    }

    if role == "compass_admin":
        try:
            await reconcile_companies_with_auth0()
        except Exception:
            pass  # don't block the dashboard on Auth0 sync failure
        companies = get_companies()
        all_trips = get_trips()
        all_customers = get_customers()
        kpi = {
            "companies": len(companies),
            "customers": len(all_customers),
            "trips": len(all_trips),
            "trips_completed": sum(1 for t in all_trips if t["status"] == "completed"),
            "total_spent": sum(c["spent"] for c in companies),
        }
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={**common, "companies": companies, "kpi": kpi},
        )

    if role == "travel_agent":
        org = ctx.get("org_name")
        my_company = get_company(org_name=org) if org else None
        trips = get_trips(org_name=org) if org else []
        customers = get_customers(org_name=org) if org else []
        customer_names = {c["id"]: c["name"] for c in customers}
        customer_ids = {c["id"] for c in customers}
        experiences = [e for e in EXPERIENCES if e["customer_id"] in customer_ids]
        bookings = _build_bookings(trips, experiences)
        pending_approvals = (
            get_approval_requests(org_name=org, status="pending") if org else []
        )
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                **common,
                "my_company": my_company,
                "bookings": bookings,
                "kpi": {"customers": len(customers)},
                "customer_names": customer_names,
                "pending_approvals": pending_approvals,
            },
        )

    if role == "customer":
        customer_id = ctx.get("customer_id")
        org = ctx.get("org_name")
        my_company = get_company(org_name=org) if org else None
        trips = get_trips(customer_id=customer_id) if customer_id else []
        experiences = (
            [e for e in EXPERIENCES if e["customer_id"] == customer_id]
            if customer_id
            else []
        )
        bookings = _build_bookings(trips, experiences)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                **common,
                "my_company": my_company,
                "bookings": bookings,
                "kpi": {"my_bookings": len(bookings)},
            },
        )

    # role == "unknown"
    return templates.TemplateResponse(
        request=request, name="dashboard.html", context=common
    )


@app.get("/companies")
async def companies_page(request: Request, response: Response):
    user, _, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    if not has_permission(ctx, "read:all_companies"):
        return RedirectResponse(url="/dashboard")

    sync_result: dict | None = None
    if has_permission(ctx, "manage:companies"):
        try:
            sync_result = await reconcile_companies_with_auth0()
        except Exception as e:
            sync_result = {"error": f"{type(e).__name__}: {e}"}

    companies = get_companies()
    counts = {
        "customers": {c["org_name"]: len(get_customers(org_name=c["org_name"])) for c in companies},
        "trips": {c["org_name"]: len(get_trips(org_name=c["org_name"])) for c in companies},
    }
    return templates.TemplateResponse(
        request=request,
        name="companies.html",
        context={
            "user": user,
            "ctx": ctx,
            "companies": companies,
            "counts": counts,
            "can_manage": has_permission(ctx, "manage:companies"),
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success"),
            "sync": sync_result,
            "sync_status": sync_status(),
        },
    )


@app.post("/companies")
async def companies_create(request: Request, response: Response):
    user, _, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    if not has_permission(ctx, "manage:companies"):
        return RedirectResponse(url="/dashboard", status_code=303)

    from urllib.parse import quote_plus

    form = await request.form()
    name = (form.get("name") or "").strip()
    display_name = (form.get("display_name") or name).strip()
    budget_raw = (form.get("budget") or "100000").strip()

    if not name:
        return RedirectResponse(url="/companies?error=name+is+required", status_code=303)
    try:
        budget = float(budget_raw)
    except ValueError:
        return RedirectResponse(url="/companies?error=invalid+budget", status_code=303)

    try:
        await step_up(
            user_sub=ctx.get("sub"),
            binding_message=f"Approve creating organization {name}",
            max_seconds=120,
        )
    except CibaNotEnrolledError:
        return RedirectResponse(
            url=f"/mfa/enroll?return_to={quote_plus('/companies')}",
            status_code=303,
        )
    except CibaError as e:
        return RedirectResponse(
            url=f"/companies?error={quote_plus(f'CIBA step-up failed: {e}')}",
            status_code=303,
        )

    try:
        org = await create_organization(name=name, display_name=display_name)
    except ManagementError as e:
        return RedirectResponse(
            url=f"/companies?error={quote_plus(str(e))}", status_code=303
        )

    add_company(
        org_name=org.get("name", name),
        display_name=org.get("display_name", display_name),
        budget=budget,
    )
    return RedirectResponse(
        url=f"/companies?success={quote_plus('Created Auth0 organization ' + org.get('name', name))}",
        status_code=303,
    )


@app.post("/companies/{company_id}/delete")
async def companies_delete(
    request: Request, response: Response, company_id: str
):
    user, _, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    if not has_permission(ctx, "manage:companies"):
        return RedirectResponse(url="/dashboard", status_code=303)

    from urllib.parse import quote_plus

    company = get_company(company_id=company_id)
    if not company:
        return RedirectResponse(
            url="/companies?error=company+not+found", status_code=303
        )

    try:
        await step_up(
            user_sub=ctx.get("sub"),
            binding_message=f"Approve DELETING organization {company['org_name']}",
            max_seconds=120,
        )
    except CibaNotEnrolledError:
        return RedirectResponse(
            url=f"/mfa/enroll?return_to={quote_plus('/companies/' + company_id)}",
            status_code=303,
        )
    except CibaError as e:
        return RedirectResponse(
            url=f"/companies?error={quote_plus(f'CIBA step-up failed: {e}')}",
            status_code=303,
        )

    try:
        auth0_org = await get_organization_by_name(company["org_name"])
        if auth0_org:
            await delete_organization(auth0_org["id"])
    except ManagementError as e:
        return RedirectResponse(
            url=f"/companies?error={quote_plus(str(e))}", status_code=303
        )

    if company in COMPANIES:
        COMPANIES.remove(company)
    return RedirectResponse(
        url=(
            f"/companies?success="
            f"{quote_plus('Deleted organization ' + company['org_name'])}"
        ),
        status_code=303,
    )


@app.get("/companies/{company_id}")
async def company_detail(request: Request, response: Response, company_id: str):
    user, _, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    company = get_company(company_id=company_id)
    if not company:
        return RedirectResponse(url="/companies" if has_permission(ctx, "read:all_companies") else "/dashboard")

    if has_permission(ctx, "read:all_companies"):
        pass  # admin sees all
    elif has_permission(ctx, "read:my_company") and company["org_name"] == ctx.get("org_name"):
        pass  # agent / customer sees own company
    else:
        return RedirectResponse(url="/dashboard")

    customers = get_customers(org_name=company["org_name"])
    agents = get_agents(org_name=company["org_name"])
    trips = get_trips(org_name=company["org_name"])
    customer_names = {c["id"]: c["name"] for c in customers}
    agent_names = {a["id"]: a["name"] for a in agents}

    auth0_org: dict | None = None
    auth0_members: list[dict] = []
    auth0_error: str | None = None
    if has_permission(ctx, "manage:companies"):
        try:
            auth0_org = await get_organization_by_name(company["org_name"])
            if auth0_org:
                auth0_members = await list_organization_members(auth0_org["id"])
        except ManagementError as e:
            auth0_error = str(e)

    return templates.TemplateResponse(
        request=request,
        name="company_detail.html",
        context={
            "user": user,
            "ctx": ctx,
            "company": company,
            "customers": customers,
            "agents": agents,
            "trips": trips,
            "customer_names": customer_names,
            "agent_names": agent_names,
            "auth0_org": auth0_org,
            "auth0_members": auth0_members,
            "auth0_error": auth0_error,
        },
    )


@app.get("/customers")
async def customers_page(request: Request, response: Response):
    user, _, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    if not has_any_permission(ctx, "read:all_customers", "read:my_customers"):
        return RedirectResponse(url="/dashboard")

    if has_permission(ctx, "read:all_customers"):
        customers = get_customers()
        scope_label = "all companies"
    else:
        customers = get_customers(org_name=ctx.get("org_name"))
        scope_label = ctx.get("org_name") or "your organization"

    company_names = {c["org_name"]: c["display_name"] for c in get_companies()}
    agent_names = {a["id"]: a["name"] for a in TRAVEL_AGENTS}
    trip_counts: dict[str, int] = {}
    for t in TRIPS:
        trip_counts[t["customer_id"]] = trip_counts.get(t["customer_id"], 0) + 1

    return templates.TemplateResponse(
        request=request,
        name="customers.html",
        context={
            "user": user,
            "ctx": ctx,
            "customers": customers,
            "company_names": company_names,
            "agent_names": agent_names,
            "trip_counts": trip_counts,
            "scope_label": scope_label,
        },
    )


@app.get("/trips")
async def trips_page(request: Request, response: Response):
    user, _, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")

    if has_permission(ctx, "read:all_trips"):
        trips = get_trips()
        scope_label = "all companies"
    elif has_permission(ctx, "read:company_trips") and ctx.get("org_name"):
        trips = get_trips(org_name=ctx["org_name"])
        scope_label = ctx["org_name"]
    elif has_permission(ctx, "read:my_trips") and ctx.get("customer_id"):
        trips = get_trips(customer_id=ctx["customer_id"])
        scope_label = "your bookings"
    else:
        return RedirectResponse(url="/dashboard")

    customer_names = {c["id"]: c["name"] for c in get_customers()}
    return templates.TemplateResponse(
        request=request,
        name="trips.html",
        context={
            "user": user,
            "ctx": ctx,
            "trips": sorted(trips, key=lambda t: t["depart_date"], reverse=True),
            "customer_names": customer_names,
            "scope_label": scope_label,
        },
    )


@app.get("/trips/{trip_id}")
async def trip_detail(request: Request, response: Response, trip_id: str):
    user, _, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    trip = get_trip(trip_id)
    if not trip:
        return RedirectResponse(url="/trips")
    customer = get_customer(trip["customer_id"])
    company = get_company(org_name=customer["org_name"]) if customer else None

    if has_permission(ctx, "read:all_trips"):
        pass
    elif has_permission(ctx, "read:company_trips") and customer and customer["org_name"] == ctx.get("org_name"):
        pass
    elif has_permission(ctx, "read:my_trips") and trip["customer_id"] == ctx.get("customer_id"):
        pass
    else:
        return RedirectResponse(url="/dashboard")

    return templates.TemplateResponse(
        request=request,
        name="trip_detail.html",
        context={
            "user": user,
            "ctx": ctx,
            "trip": trip,
            "customer": customer,
            "company": company,
            "experiences": get_experiences_for_trip(trip_id),
        },
    )


@app.get("/profile")
async def profile(request: Request, response: Response):
    user, session, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    access_token, _ = _tokens_from_session(session)
    id_token = ""
    token_sets = (session or {}).get("token_sets") or []
    if token_sets:
        id_token = token_sets[0].get("id_token", "") or ""

    access_token_kind = classify_token(access_token)
    access_token_header = decode_jwt_header(access_token) if access_token else {}
    access_token_claims = (
        decode_jwt_claims(access_token) if access_token_kind == "jws" else {}
    )
    id_token_header = decode_jwt_header(id_token) if id_token else {}

    # Enrollment status section: any role whose actions are CIBA-gated
    # (admins + travel agents) — skip for customers, who don't have
    # CIBA-gated tools and don't need to see the section.
    enrollments: list[dict] = []
    enrollment_error: str | None = None
    show_enrollment_section = ctx.get("role") in ("compass_admin", "travel_agent")
    if show_enrollment_section:
        user_sub = ctx.get("sub") or user.get("sub")
        if user_sub:
            try:
                enrollments = await list_user_enrollments(user_sub)
            except ManagementError as e:
                enrollment_error = str(e)

    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "user": user,
            "ctx": ctx,
            "id_token": id_token,
            "id_token_claims": user,
            "id_token_header": id_token_header,
            "access_token": access_token,
            "access_token_kind": access_token_kind,
            "access_token_header": access_token_header,
            "access_token_claims": access_token_claims,
            "enrollments": enrollments,
            "enrollment_error": enrollment_error,
            "show_enrollment_section": show_enrollment_section,
        },
    )


# ---------- chat ----------


@app.get("/chat")
async def chat_page(request: Request, response: Response):
    user, session, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    messages = request.session.get("conversation", [])
    visible_tools = [s["function"]["name"] for s in cz_visible_schemas(ctx)] + [
        s["function"]["name"] for s in visible_google_schemas(ctx)
    ]
    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context={"user": user, "ctx": ctx, "messages": messages, "visible_tools": visible_tools},
    )


async def dispatch_any_tool(name: str, args: dict, ctx: dict, refresh_token: str) -> str:
    if name in CZ_TOOLS:
        return await cz_dispatch(name, args, ctx)
    if name in GOOGLE_TOOLS_BY_NAME:
        if not _can_use_google_tools(ctx):
            return json.dumps(
                {
                    "error": (
                        "permission denied — Google tools need one of "
                        f"{', '.join(GOOGLE_TOOL_PERMISSIONS)}"
                    )
                }
            )
        try:
            return await dispatch_google_tool(name, args, refresh_token)
        except TokenVaultError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:
            return json.dumps({"error": f"{type(e).__name__}: {e}"})
    return json.dumps({"error": f"unknown tool: {name}"})


@app.post("/chat/stream")
async def chat_stream(request: Request, response: Response):
    user, session, ctx = await require_login(request, response)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()
    user_message = (body.get("message") or "").strip()
    if not user_message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    access_token, refresh_token = _tokens_from_session(session)
    conversation = request.session.get("conversation", [])

    tool_schemas = cz_visible_schemas(ctx) + visible_google_schemas(ctx)

    messages = (
        [{"role": "system", "content": build_system_prompt(user, ctx)}]
        + conversation
        + [{"role": "user", "content": user_message}]
    )

    async def generate():
        try:
            for _ in range(MAX_TOOL_ITERATIONS):
                kwargs = {"model": LLM_MODEL, "messages": messages, "stream": True}
                if tool_schemas:
                    kwargs["tools"] = tool_schemas
                stream = await openai_client.chat.completions.create(**kwargs)

                content_acc = ""
                tool_calls_acc: dict[int, dict] = {}

                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if getattr(delta, "content", None):
                        content_acc += delta.content
                        yield delta.content
                    for tc in getattr(delta, "tool_calls", None) or []:
                        slot = tool_calls_acc.setdefault(
                            tc.index, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                slot["name"] = tc.function.name
                            if tc.function.arguments:
                                slot["arguments"] += tc.function.arguments

                if not tool_calls_acc:
                    return

                messages.append(
                    {
                        "role": "assistant",
                        "content": content_acc or None,
                        "tool_calls": [
                            {
                                "id": v["id"],
                                "type": "function",
                                "function": {"name": v["name"], "arguments": v["arguments"]},
                            }
                            for v in tool_calls_acc.values()
                        ],
                    }
                )

                for tc in tool_calls_acc.values():
                    name = tc["name"]
                    try:
                        args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    except json.JSONDecodeError:
                        args = {}

                    badge = _tool_status_badge(name)
                    status_lines = [f"\n\n_⏺ Calling **{name}**_{badge}"]
                    if args:
                        for k, v in args.items():
                            status_lines.append(
                                f"_  • `{k}` = {_short_arg(v)}_"
                            )
                    else:
                        status_lines.append("_  • (no arguments)_")
                    required = (
                        CZ_TOOLS.get(name, {}).get("required_scopes")
                        if name in CZ_TOOLS
                        else None
                    )
                    if required:
                        status_lines.append(
                            "_  • required scope"
                            f"{'s' if len(required) > 1 else ''}: "
                            + ", ".join(f"`{s}`" for s in required)
                            + "_"
                        )
                    if name in CIBA_GATED_CHAT_TOOLS:
                        status_lines.append(
                            "_  📲 Push notification sent — approve in the "
                            "Auth0 Guardian app on your phone (waiting up "
                            "to 3 minutes)..._"
                        )
                    yield "\n".join(status_lines) + "\n"

                    start_t = time.monotonic()
                    result = await dispatch_any_tool(name, args, ctx, refresh_token)
                    elapsed_ms = int((time.monotonic() - start_t) * 1000)

                    is_error = False
                    try:
                        parsed = json.loads(result)
                        is_error = isinstance(parsed, dict) and "error" in parsed
                    except Exception:
                        pass
                    summary = _summarize_tool_result(name, result)
                    status_icon = "✗ failed" if is_error else "✓ done"
                    final_line = f"_  {status_icon} in {elapsed_ms} ms"
                    if summary:
                        final_line += f" — {summary}"
                    final_line += "_\n\n"
                    yield final_line

                    messages.append(
                        {"role": "tool", "tool_call_id": tc["id"], "content": result}
                    )

            yield "\n\n[Stopped: tool-call loop hit iteration limit.]"
        except Exception as e:
            print(f"OpenAI API error: {type(e).__name__}: {e}")
            yield f"\n\nError: {type(e).__name__}: {e}"

    return StreamingResponse(generate(), media_type="text/plain")


@app.post("/chat/save")
async def chat_save(request: Request, response: Response):
    user = await _get_user(request, response)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    user_msg = (body.get("user") or "").strip()
    assistant_msg = (body.get("assistant") or "").strip()
    if not user_msg or not assistant_msg:
        return JSONResponse({"error": "missing fields"}, status_code=400)
    conversation = request.session.get("conversation", [])
    conversation.append({"role": "user", "content": user_msg})
    conversation.append({"role": "assistant", "content": assistant_msg})
    request.session["conversation"] = conversation
    return JSONResponse({"ok": True})


@app.post("/chat/clear")
async def chat_clear(request: Request, response: Response):
    user = await _get_user(request, response)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    request.session["conversation"] = []
    return JSONResponse({"ok": True})


# ---------- MFA enrollment (Guardian ticket) ----------


@app.get("/mfa/enroll")
async def mfa_enroll(request: Request, response: Response):
    user, _, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")

    return_to = request.query_params.get("return_to") or "/dashboard"
    user_sub = ctx.get("sub") or user.get("sub")

    ticket: dict | None = None
    error: str | None = None
    if not user_sub:
        error = "no user sub on token — cannot mint enrollment ticket"
    else:
        try:
            ticket = await create_enrollment_ticket(
                user_id=user_sub, send_mail=False
            )
        except ManagementError as e:
            error = str(e)

    return templates.TemplateResponse(
        request=request,
        name="mfa_enroll.html",
        context={
            "user": user,
            "ctx": ctx,
            "ticket": ticket,
            "error": error,
            "return_to": return_to,
        },
    )


# ---------- connections (My Account API) ----------


@app.get("/connections")
async def connections_page(request: Request, response: Response):
    user, session, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    accounts: list[dict] = []
    error: str | None = None
    _, refresh_token = _tokens_from_session(session)
    try:
        token = await mint_my_account_token(refresh_token)
        accounts = await list_accounts(token)
    except MyAccountError as e:
        error = str(e)
    return templates.TemplateResponse(
        request=request,
        name="connections.html",
        context={
            "user": user,
            "ctx": ctx,
            "accounts": accounts,
            "error": request.query_params.get("error") or error,
            "success": request.query_params.get("success"),
        },
    )


@app.post("/connections/connect/{connection}")
async def connections_connect(request: Request, response: Response, connection: str):
    user, session, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    _, refresh_token = _tokens_from_session(session)

    redirect_uri = str(request.url_for("connections_callback"))
    state = secrets.token_urlsafe(24)

    scopes_for_connection = {
        "google-oauth2": ["openid", *GOOGLE_CONNECTION_SCOPES],
    }
    scopes = scopes_for_connection.get(connection)

    try:
        token = await mint_my_account_token(refresh_token)
        result = await initiate_connect(
            my_account_token=token,
            connection=connection,
            redirect_uri=redirect_uri,
            state=state,
            scopes=scopes,
        )
    except MyAccountError as e:
        from urllib.parse import quote_plus

        return RedirectResponse(
            url=f"/connections?error={quote_plus(str(e))}", status_code=303
        )

    request.session["pending_connect"] = {
        "auth_session": result.get("auth_session"),
        "state": state,
        "redirect_uri": redirect_uri,
        "connection": connection,
    }
    ticket = (result.get("connect_params") or {}).get("ticket")
    connect_uri = result.get("connect_uri")
    return RedirectResponse(url=f"{connect_uri}?ticket={ticket}", status_code=303)


@app.get("/connections/callback")
async def connections_callback(request: Request):
    return templates.TemplateResponse(
        request=request, name="connections_callback.html", context={}
    )


@app.post("/connections/complete")
async def connections_complete(request: Request, response: Response):
    user = await _get_user(request, response)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    connect_code = (body.get("connect_code") or "").strip()
    state = (body.get("state") or "").strip()
    if not connect_code:
        return JSONResponse({"error": "missing connect_code"}, status_code=400)
    pending = request.session.get("pending_connect") or {}
    if not pending:
        return JSONResponse({"error": "no pending connect in session"}, status_code=400)
    if state and pending.get("state") and state != pending["state"]:
        return JSONResponse({"error": "state mismatch"}, status_code=400)
    session = await _get_session(request, response) or {}
    _, refresh_token = _tokens_from_session(session)
    try:
        token = await mint_my_account_token(refresh_token)
        await complete_connect(
            my_account_token=token,
            auth_session=pending["auth_session"],
            connect_code=connect_code,
            redirect_uri=pending["redirect_uri"],
        )
    except MyAccountError as e:
        request.session.pop("pending_connect", None)
        return JSONResponse({"error": str(e)}, status_code=400)
    request.session.pop("pending_connect", None)
    return JSONResponse({"ok": True})


@app.post("/connections/disconnect/{account_id}")
async def connections_disconnect(
    request: Request, response: Response, account_id: str
):
    user = await _get_user(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    session = await _get_session(request, response) or {}
    _, refresh_token = _tokens_from_session(session)
    try:
        token = await mint_my_account_token(refresh_token)
        await delete_account(token, account_id)
    except MyAccountError as e:
        from urllib.parse import quote_plus

        return RedirectResponse(
            url=f"/connections?error={quote_plus(str(e))}", status_code=303
        )
    return RedirectResponse(url="/connections?success=disconnected", status_code=303)


# ---------- documents ----------


@app.get("/documents")
async def documents_page(request: Request, response: Response):
    user, _, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    _ensure_seed_documents()
    docs = _docs_visible_to(ctx)
    customer_names = {c["id"]: c["name"] for c in get_customers()}
    company_names = {c["org_name"]: c["display_name"] for c in get_companies()}

    by_kind = {"contract": [], "invoice": [], "uploaded": []}
    for d in docs:
        by_kind.setdefault(d["kind"], []).append(d)
    for kind in by_kind:
        by_kind[kind].sort(key=lambda d: d["created_at"], reverse=True)

    can_upload = has_any_permission(ctx, "manage:companies", "book:trips")

    if has_permission(ctx, "read:all_companies"):
        scope_label = "all organizations"
    elif ctx.get("role") == "travel_agent":
        scope_label = ctx.get("org_name") or "your organization"
    else:
        scope_label = "your invoices"

    return templates.TemplateResponse(
        request=request,
        name="documents.html",
        context={
            "user": user,
            "ctx": ctx,
            "by_kind": by_kind,
            "total_count": len(docs),
            "scope_label": scope_label,
            "can_upload": can_upload,
            "customer_names": customer_names,
            "company_names": company_names,
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success"),
        },
    )


@app.get("/documents/{doc_id}")
async def documents_download(request: Request, response: Response, doc_id: str):
    user, _, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    _ensure_seed_documents()
    doc = get_document(doc_id)
    if not doc or not _user_can_view_doc(ctx, doc):
        return RedirectResponse(url="/documents?error=document+not+found", status_code=303)
    path = documents_dir() / doc["filename"]
    if not path.exists():
        return RedirectResponse(url="/documents?error=file+missing", status_code=303)
    from fastapi.responses import FileResponse

    media_type = "application/pdf"
    if doc["filename"].endswith(".txt"):
        media_type = "text/plain"
    elif doc["filename"].endswith(".docx"):
        media_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    # ?download=1 forces attachment; without it, render inline so the
    # browser opens PDFs/TXT in a new tab.
    download = request.query_params.get("download") in ("1", "true")
    if download:
        return FileResponse(
            path=str(path), media_type=media_type, filename=doc["filename"]
        )
    return FileResponse(
        path=str(path),
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{doc["filename"]}"'
        },
    )


@app.post("/documents/upload")
async def documents_upload(request: Request, response: Response):
    from fastapi import UploadFile

    user, _, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    if not has_any_permission(ctx, "manage:companies", "book:trips"):
        return RedirectResponse(
            url="/documents?error=upload+requires+admin+or+agent+role",
            status_code=303,
        )

    from urllib.parse import quote_plus

    form = await request.form()
    upload: UploadFile | None = form.get("file")
    title = (form.get("title") or "").strip()
    if upload is None or not getattr(upload, "filename", ""):
        return RedirectResponse(
            url="/documents?error=no+file+selected", status_code=303
        )

    import os as _os

    _, ext = _os.path.splitext(upload.filename.lower())
    if ext not in _UPLOAD_ALLOWED_EXT:
        allowed = ",".join(sorted(_UPLOAD_ALLOWED_EXT))
        return RedirectResponse(
            url=f"/documents?error={quote_plus(f'file type {ext} not allowed; pick {allowed}')}",
            status_code=303,
        )

    contents = await upload.read()
    if len(contents) > _UPLOAD_MAX_BYTES:
        return RedirectResponse(
            url="/documents?error=file+too+large+%2810MB+max%29", status_code=303
        )

    safe_name = (
        upload.filename.replace("/", "_").replace("\\", "_").replace("..", "_")
    )
    out = documents_dir() / f"{int(time.time())}-{safe_name}"
    out.write_bytes(contents)

    add_document(
        kind="uploaded",
        title=title or upload.filename,
        filename=out.name,
        org_name=ctx.get("org_name") or "",
        uploaded_by=ctx.get("sub") or user.get("sub", ""),
        size_bytes=out.stat().st_size,
    )
    return RedirectResponse(
        url=f"/documents?success={quote_plus('Uploaded ' + upload.filename)}",
        status_code=303,
    )


# ---------- approval requests (agent dashboard buttons) ----------


@app.post("/approvals/{request_id}/approve")
async def approvals_approve(
    request: Request, response: Response, request_id: str
):
    user, _, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    if not has_permission(ctx, "book:trips"):
        return RedirectResponse(url="/dashboard", status_code=303)

    from urllib.parse import quote_plus

    req = get_approval_request(request_id)
    if not req:
        return RedirectResponse(
            url="/dashboard?error=request+not+found", status_code=303
        )
    if req["status"] != "pending":
        return RedirectResponse(
            url=f"/dashboard?error=request+already+{req['status']}",
            status_code=303,
        )
    if req.get("org_name") != ctx.get("org_name"):
        return RedirectResponse(
            url="/dashboard?error=request+is+outside+your+organization",
            status_code=303,
        )

    customer = get_customer(req["customer_id"])
    binding = (
        f"Approve booking request {request_id} "
        f"for {customer['name'] if customer else req['customer_id']}"
    )
    try:
        await step_up(
            user_sub=ctx.get("sub"),
            binding_message=binding,
            max_seconds=180,
        )
    except CibaNotEnrolledError:
        return RedirectResponse(
            url=f"/mfa/enroll?return_to={quote_plus('/dashboard')}",
            status_code=303,
        )
    except CibaError as e:
        return RedirectResponse(
            url=f"/dashboard?error={quote_plus(f'CIBA step-up failed: {e}')}",
            status_code=303,
        )

    details = req["details"]
    try:
        if req["kind"] == "trip":
            booking = add_trip(
                customer_id=req["customer_id"],
                type=details["type"],
                origin=details["origin"],
                destination=details["destination"],
                depart_date=details["depart_date"],
                return_date=details["return_date"],
                cost=float(details["cost"]),
                currency=details.get("currency", "USD"),
            )
            update_approval_request(
                request_id,
                status="approved",
                trip_id=booking["id"],
                decided_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                decided_by=ctx.get("agent_id") or "",
            )
        elif req["kind"] == "experience":
            booking = add_experience(
                customer_id=req["customer_id"],
                name=details["name"],
                date=details["date"],
                cost=float(details["cost"]),
                trip_id=details.get("trip_id", ""),
                location=details.get("location", ""),
            )
            update_approval_request(
                request_id,
                status="approved",
                experience_id=booking["id"],
                decided_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                decided_by=ctx.get("agent_id") or "",
            )
        else:
            return RedirectResponse(
                url=f"/dashboard?error={quote_plus('unknown request kind: ' + req['kind'])}",
                status_code=303,
            )
    except Exception as e:
        return RedirectResponse(
            url=f"/dashboard?error={quote_plus(f'{type(e).__name__}: {e}')}",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/dashboard?success={quote_plus('Approved request ' + request_id)}",
        status_code=303,
    )


@app.post("/approvals/{request_id}/deny")
async def approvals_deny(
    request: Request, response: Response, request_id: str
):
    user, _, ctx = await require_login(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    if not has_permission(ctx, "book:trips"):
        return RedirectResponse(url="/dashboard", status_code=303)

    from urllib.parse import quote_plus

    req = get_approval_request(request_id)
    if not req:
        return RedirectResponse(
            url="/dashboard?error=request+not+found", status_code=303
        )
    if req["status"] != "pending":
        return RedirectResponse(
            url=f"/dashboard?error=request+already+{req['status']}",
            status_code=303,
        )
    if req.get("org_name") != ctx.get("org_name"):
        return RedirectResponse(
            url="/dashboard?error=request+is+outside+your+organization",
            status_code=303,
        )

    form = await request.form()
    note = (form.get("note") or "").strip()
    update_approval_request(
        request_id,
        status="denied",
        decided_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        decided_by=ctx.get("agent_id") or "",
        decision_note=note,
    )
    return RedirectResponse(
        url=f"/dashboard?success={quote_plus('Denied request ' + request_id)}",
        status_code=303,
    )
