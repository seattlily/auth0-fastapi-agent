import base64
import json
import os
import secrets

from auth0_fastapi.auth.auth_client import AuthClient
from auth0_fastapi.config import Auth0Config
from auth0_fastapi.server.routes import register_auth_routes
from auth0_fastapi.server.routes import router as auth_router
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openai import AsyncOpenAI
from starlette.middleware.sessions import SessionMiddleware

from tools.auth0_my_account import (
    MyAccountError,
    complete_connect,
    delete_account,
    initiate_connect,
    list_accounts,
    mint_my_account_token,
)
from tools.google_calendar import (
    CALENDAR_TOOL_SCHEMA,
    CREATE_CALENDAR_EVENT_TOOL_SCHEMA,
    TokenVaultError,
    create_calendar_event,
    list_upcoming_calendar_events,
)
from tools.google_gmail import (
    GMAIL_LIST_TOOL_SCHEMA,
    list_recent_emails,
)

MAX_TOOL_ITERATIONS = 3
GOOGLE_CONNECTION_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
]

load_dotenv(override=True)

app = FastAPI()

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["APP_SECRET_KEY"],
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ---------- Auth0 SDK setup ----------

_authorization_params = {
    "scope": (
        "openid profile email offline_access "
        "create:me:connected_accounts "
        "read:me:connected_accounts "
        "delete:me:connected_accounts"
    ),
}

_auth0_kwargs = {
    "domain": os.environ["AUTH0_DOMAIN"],
    "client_id": os.environ["AUTH0_CLIENT_ID"],
    "client_secret": os.environ["AUTH0_CLIENT_SECRET"],
    "app_base_url": os.environ.get("APP_BASE_URL", "http://localhost:8000"),
    "secret": os.environ["APP_SECRET_KEY"],
    "authorization_params": _authorization_params,
}
if os.environ.get("AUTH0_AUDIENCE"):
    _auth0_kwargs["audience"] = os.environ["AUTH0_AUDIENCE"]

auth0_config = Auth0Config(**_auth0_kwargs)
auth_client = AuthClient(auth0_config)
app.state.config = auth0_config
app.state.auth_client = auth_client
register_auth_routes(auth_router, auth0_config)
app.include_router(auth_router)
# After this, /auth/login, /auth/callback, /auth/logout are wired up.


openai_client = AsyncOpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
)
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")


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


def build_system_prompt(user: dict | None, access_token: str) -> str:
    user = user or {}
    access_token_claims = decode_jwt_claims(access_token) if access_token else {}

    profile = {
        "name": user.get("name"),
        "email": user.get("email"),
        "nickname": user.get("nickname"),
        "given_name": user.get("given_name"),
        "family_name": user.get("family_name"),
        "locale": user.get("locale"),
        "sub": user.get("sub"),
        "email_verified": user.get("email_verified"),
        "id_token_claims": user,
        "access_token_claims": access_token_claims,
    }
    profile = {k: v for k, v in profile.items() if v not in (None, "", {}, [])}

    return (
        "You are a helpful AI assistant. The signed-in user's profile, derived "
        "from their Auth0 ID and access tokens, is provided below as JSON. Use "
        "it to personalize responses (greet them by name, tailor advice to "
        "their email/locale, reference roles or scopes from their access "
        "token claims when relevant). Do not reveal raw token values or the "
        "literal JSON unless the user asks for them.\n\n"
        f"User profile:\n{json.dumps(profile, indent=2, default=str)}"
    )


# ---------- pages ----------


@app.get("/")
async def home(request: Request, response: Response):
    user = await _get_user(request, response)
    if user:
        return RedirectResponse(url="/chat")
    return templates.TemplateResponse(request=request, name="home.html")


@app.get("/connect/google-calendar")
async def connect_google_calendar(request: Request):
    # Trigger the SDK's login flow but pin the federated connection and
    # request the Calendar/Gmail scopes at the upstream IdP.
    from urllib.parse import urlencode

    params = {
        "connection": "google-oauth2",
        "connection_scope": " ".join(GOOGLE_CONNECTION_SCOPES),
    }
    return RedirectResponse(url=f"/auth/login?{urlencode(params)}")


@app.get("/profile")
async def profile(request: Request, response: Response):
    user = await _get_user(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    session = await _get_session(request, response) or {}

    access_token = session.get("access_token") or ""
    access_token_kind = classify_token(access_token)
    access_token_header = decode_jwt_header(access_token) if access_token else {}
    access_token_claims = (
        decode_jwt_claims(access_token) if access_token_kind == "jws" else {}
    )

    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "user": user,
            "id_token_claims": user,
            "id_token_claims_pretty": json.dumps(user, indent=2, default=str),
            "access_token": access_token,
            "access_token_kind": access_token_kind,
            "access_token_header": access_token_header,
            "access_token_header_pretty": json.dumps(
                access_token_header, indent=2, default=str
            ),
            "access_token_claims": access_token_claims,
            "access_token_claims_pretty": json.dumps(
                access_token_claims, indent=2, default=str
            ),
        },
    )


@app.get("/chat")
async def chat_page(request: Request, response: Response):
    user = await _get_user(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    messages = request.session.get("conversation", [])
    return templates.TemplateResponse(
        request=request, name="chat.html", context={"user": user, "messages": messages}
    )


# ---------- chat ----------


async def dispatch_tool(name: str, args: dict, refresh_token: str) -> str:
    if name == "list_upcoming_calendar_events":
        return await list_upcoming_calendar_events(
            refresh_token=refresh_token,
            days=int(args.get("days", 7)),
            max_results=int(args.get("max_results", 5)),
        )
    if name == "create_calendar_event":
        return await create_calendar_event(
            refresh_token=refresh_token,
            summary=args["summary"],
            start=args["start"],
            end=args["end"],
            description=args.get("description", ""),
            location=args.get("location", ""),
            attendees=args.get("attendees") or None,
        )
    if name == "list_recent_emails":
        return await list_recent_emails(
            refresh_token=refresh_token,
            max_results=int(args.get("max_results", 5)),
            query=args.get("query", ""),
        )
    return json.dumps({"error": f"Unknown tool: {name}"})


GOOGLE_TOOL_SCHEMAS = [
    CALENDAR_TOOL_SCHEMA,
    CREATE_CALENDAR_EVENT_TOOL_SCHEMA,
    GMAIL_LIST_TOOL_SCHEMA,
]


@app.post("/chat/stream")
async def chat_stream(request: Request, response: Response):
    user = await _get_user(request, response)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()
    user_message = (body.get("message") or "").strip()
    if not user_message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    session = await _get_session(request, response) or {}
    refresh_token = session.get("refresh_token") or ""
    access_token = session.get("access_token") or ""
    conversation = request.session.get("conversation", [])

    messages = (
        [{"role": "system", "content": build_system_prompt(user, access_token)}]
        + conversation
        + [{"role": "user", "content": user_message}]
    )

    async def generate():
        try:
            for _ in range(MAX_TOOL_ITERATIONS):
                stream = await openai_client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=messages,
                    tools=GOOGLE_TOOL_SCHEMAS,
                    stream=True,
                )

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
                    try:
                        args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    except json.JSONDecodeError:
                        args = {}
                    try:
                        result = await dispatch_tool(tc["name"], args, refresh_token)
                    except TokenVaultError as e:
                        result = json.dumps({"error": str(e)})
                    except Exception as e:
                        print(f"Tool error: {type(e).__name__}: {e}")
                        result = json.dumps({"error": f"{type(e).__name__}: {e}"})
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


# ---------- connections (My Account API) ----------


@app.get("/connections")
async def connections_page(request: Request, response: Response):
    user = await _get_user(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    session = await _get_session(request, response) or {}

    accounts: list[dict] = []
    error: str | None = None
    refresh_token = session.get("refresh_token") or ""
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
            "accounts": accounts,
            "error": request.query_params.get("error") or error,
            "success": request.query_params.get("success"),
        },
    )


@app.post("/connections/connect/{connection}")
async def connections_connect(request: Request, response: Response, connection: str):
    user = await _get_user(request, response)
    if not user:
        return RedirectResponse(url="/auth/login")
    session = await _get_session(request, response) or {}
    refresh_token = session.get("refresh_token") or ""

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
    refresh_token = session.get("refresh_token") or ""
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
    refresh_token = session.get("refresh_token") or ""

    try:
        token = await mint_my_account_token(refresh_token)
        await delete_account(token, account_id)
    except MyAccountError as e:
        from urllib.parse import quote_plus

        return RedirectResponse(
            url=f"/connections?error={quote_plus(str(e))}", status_code=303
        )

    return RedirectResponse(url="/connections?success=disconnected", status_code=303)
