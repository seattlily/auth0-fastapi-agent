# Implementation Reference

A deep dive into how this template works — for workshop attendees who want to understand *why* things are wired the way they are, not just *what* the code does.

For setup and quick start, see [README.md](./README.md). This document focuses on architecture, every non-obvious decision, and a curated list of footguns to avoid.

---

## 1. What the app does

A single-process FastAPI web app that:

1. Logs users in via **Auth0** (Authorization Code Flow with OIDC). Multiple IdPs supported on the universal-login page.
2. Renders a **chat UI** (Jinja2 templates) where the LLM streams replies token-by-token.
3. Personalizes the LLM's responses by injecting the signed-in user's **Auth0 ID/access token claims** into the system prompt.
4. Lets the LLM call **Google Calendar** on the user's behalf via **Auth0 Token Vault**, using OpenAI function calling.
5. Exposes a **Connected Accounts** page (using the Auth0 **My Account API**) where a user logged in with one IdP can attach a Google identity for Token Vault use, without changing their primary login.

Stack: FastAPI · Authlib · Starlette `SessionMiddleware` · Jinja2 · OpenAI Python SDK · `httpx` · `google-api-python-client`.

---

## 2. File layout

```
.
├── README.md                          # workshop quick-start
├── IMPLEMENTATION.md                  # this file
├── .gitignore
└── app/
    ├── .env                           # secrets — never committed (in .gitignore)
    ├── .env.example                   # template
    ├── requirements.txt
    ├── main.py                        # FastAPI app, all routes
    ├── tools/
    │   ├── __init__.py
    │   ├── google_calendar.py         # Token Vault exchange + Calendar API
    │   └── auth0_my_account.py        # My Account API: connect/list/delete connected accounts
    ├── static/style.css
    └── templates/
        ├── home.html
        ├── chat.html                  # streaming UI + Connections nav link
        ├── profile.html               # raw token claims viewer
        ├── connections.html           # connected-accounts management UI
        └── connections_callback.html  # JS shim that reads connect_code from URL fragment
```

---

## 3. Route map

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Home; redirects logged-in users to `/chat` |
| GET | `/login` | Universal Auth0 login (any IdP) |
| GET | `/connect/google-calendar` | Auth0 login pinned to `connection=google-oauth2` with Calendar scope at consent |
| GET | `/callback` | OAuth callback; stores ID token claims, access token, refresh token in session |
| GET | `/logout` | Clears session and redirects through Auth0's `/v2/logout` |
| GET | `/profile` | Renders ID/access token claims |
| GET | `/chat` | Chat page; renders prior conversation from session |
| POST | `/chat` | Non-streaming form-submit fallback (no JS) |
| POST | `/chat/stream` | Streaming chat with tool-calling loop |
| POST | `/chat/save` | Persists user+assistant turn to session after the stream closes |
| GET | `/connections` | Lists currently connected accounts; "Connect Google Account" button |
| POST | `/connections/connect/{connection}` | Initiates a Connected Accounts flow |
| GET | `/connections/callback` | Renders the JS shim that reads `connect_code` from `window.location.hash` |
| POST | `/connections/complete` | Server completes the connect (`auth_session` + `connect_code` → My Account API) |
| POST | `/connections/disconnect/{account_id}` | Removes a connected account |

---

## 4. Feature deep dive

### 4.1 Streaming chat

`POST /chat/stream` returns a `StreamingResponse(media_type="text/plain")`. The client uses `fetch()` + `ReadableStream.getReader()` to display tokens as they arrive.

A non-streaming form-submit `POST /chat` is kept as a no-JS fallback.

**Why two endpoints (`/chat/stream` for the stream, `/chat/save` to persist):** Starlette's `SessionMiddleware` writes the session cookie via `Set-Cookie` on `http.response.start`, which fires *before* any streamed body bytes. Mutations to `request.session` inside the stream generator never make it back to the browser. So `/chat/stream` is read-only against the session, and the client calls `/chat/save` after the stream closes to persist the turn.

### 4.2 Profile-aware system prompt

`build_system_prompt(request)` reads:
- `user` (Authlib's `token["userinfo"]` from the ID token)
- `id_token_claims`
- `access_token` → base64-decoded payload (no signature validation; we are the audience)

Filters empty values, dumps as JSON, and embeds in the system prompt with an instruction telling the model to personalize but not echo raw tokens.

### 4.3 Auth0 Token Vault → Google Calendar tool

Goal: let the chat answer "what's on my calendar this week?" by calling the Google Calendar API on the user's behalf, with the Google access token minted by Auth0 (not stored by the app, not requested at login as a static OAuth scope).

The Auth0 official guide (https://auth0.com/ai/docs/get-started/call-others-apis-on-users-behalf) prescribes a LangGraph + React stack with the `auth0-ai-langchain` SDK. This template **adapts** to a single-process FastAPI + Jinja stack by calling Token Vault's HTTP endpoint directly with `httpx`. No `auth0-ai-*` SDK; no LangGraph; no React.

#### 4.3.1 OAuth changes for Token Vault

- `offline_access` is in Authlib's `client_kwargs.scope` so the token response includes a `refresh_token`.
- `/callback` stores `request.session["refresh_token"] = token.get("refresh_token", "")`.
- `/connect/google-calendar` pins `connection="google-oauth2"` and passes `connection_scope="https://www.googleapis.com/auth/calendar.readonly"` to `authorize_redirect`. Used so users who pick Google at login pre-grant the Calendar scope. The generic `/login` does **not** carry `connection_scope` — that param bleeds into other IdPs as a literal scope (URLs aren't valid GitHub scopes) and breaks the universal-login picker.

#### 4.3.2 Token Vault refresh-token exchange (`tools/google_calendar.py`)

```
POST https://{AUTH0_DOMAIN}/oauth/token
Content-Type: application/json
{
  "grant_type":          "urn:auth0:params:oauth:grant-type:token-exchange:federated-connection-access-token",
  "subject_token_type":  "urn:ietf:params:oauth:token-type:refresh_token",
  "subject_token":       "<user's Auth0 refresh token>",
  "requested_token_type":"http://auth0.com/oauth/token-type/federated-connection-access-token",
  "connection":          "google-oauth2",
  "client_id":           "...",
  "client_secret":       "..."
}
```

Response: `{ "access_token": "<google_access_token>", "scope": "...", "expires_in": 1377, "token_type": "Bearer", ... }`.

No `audience` parameter. No `scope` parameter (scopes are determined by what was granted to the federated connection at consent time). Auth0 requires the application to have the Token Vault grant enabled, refresh-token rotation off, MRRT enabled for the My Account API, and the Google federated connection wired up with a real Google OAuth client.

Wrapper function: `get_federated_access_token(refresh_token, connection)` returns the Google access token or raises `TokenVaultError` with Auth0's `error_description` on 4xx.

#### 4.3.3 Calendar API call

`list_upcoming_calendar_events(refresh_token, days=7, max_results=5)`:
1. Mints a Google access token via `get_federated_access_token`.
2. Builds `google.oauth2.credentials.Credentials(google_access_token)`.
3. `googleapiclient.discovery.build("calendar","v3", credentials=...)`.
4. Calls `events().list(calendarId="primary", timeMin=..., timeMax=..., singleEvents=True, orderBy="startTime").execute()`.
5. Returns `json.dumps([{summary, start, end, location} ...])`.

#### 4.3.4 Tool-calling loop in `/chat/stream`

A small loop (default cap 3 iterations to bound the worst case):

```
for _ in range(MAX_TOOL_ITERATIONS):
    open Chat Completions stream with tools=[CALENDAR_TOOL_SCHEMA]
    accumulate text deltas → yield to client
    accumulate tool_call deltas (per tc.index slot) → DO NOT yield
    when stream closes:
        if no tool_calls: return
        append assistant message {role: "assistant", content, tool_calls: [...]}
        for each tool_call:
            execute via dispatch_tool(name, args, refresh_token)
            append {role: "tool", tool_call_id, content: <result json>}
```

Tool args arrive across many deltas — accumulated by `tc.index` slot (`{"id": "", "name": "", "arguments": ""}`). Errors during tool dispatch are caught and surfaced as `{"error": "..."}` JSON in the tool result so the model can react gracefully.

### 4.4 Connected Accounts UI

Goal: a `/connections` page where a user already logged in with any IdP can link a Google identity for Token Vault use — without re-logging-in or losing their primary identity. Backed by Auth0's **My Account API** at `https://{AUTH0_DOMAIN}/me/v1/connected-accounts/*`.

#### 4.4.1 OAuth scope expansion

Three additional scopes are in the initial Authlib `scope`:

```
openid profile email offline_access
create:me:connected_accounts
read:me:connected_accounts
delete:me:connected_accounts
```

These permit the user's refresh token (with MRRT) to be exchanged for an access token *for the My Account API audience* with those scopes.

#### 4.4.2 The four-call dance

```
mint My Account token   POST /oauth/token            grant_type=refresh_token, audience=https://{TENANT}/me/, scope=...connected_accounts
initiate connect        POST /me/v1/connected-accounts/connect       → { auth_session, connect_uri, connect_params.ticket, expires_in }
redirect browser        302 to {connect_uri}?ticket={ticket}         → user authenticates at IdP
callback (browser)      GET {redirect_uri}#connect_code=...&state=…  → JS reads fragment, POSTs to /connections/complete
complete connect        POST /me/v1/connected-accounts/complete      → { id, connection, scopes, created_at, access_type }
```

`auth_session`, `state`, `redirect_uri`, and `connection` are stashed in `request.session["pending_connect"]` between the initiate and the complete. State is verified before completing.

#### 4.4.3 The URL fragment quirk

Auth0 returns `connect_code` and `state` in the **URL fragment** (`#connect_code=…`). Fragments don't reach the server. So `/connections/callback` is a server-rendered page whose only job is to ship a tiny script to the browser:

```js
const params = new URLSearchParams(window.location.hash.slice(1));
const connect_code = params.get('connect_code');
const state = params.get('state');
fetch('/connections/complete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({connect_code, state}),
}).then(r => r.ok
    ? location.replace('/connections?success=connected')
    : r.json().then(j => location.replace('/connections?error=' + encodeURIComponent(j.error || 'unknown'))));
```

#### 4.4.4 List/disconnect

`/connections` GET:
- Mints My Account token
- `GET /me/v1/connected-accounts/accounts`
- Renders each row with connection, scopes, created_at, and a "Disconnect" form.

`POST /connections/disconnect/{account_id}`:
- Mints My Account token
- `DELETE /me/v1/connected-accounts/accounts/{account_id}`
- 303 back to `/connections?success=disconnected`

Per Auth0 docs, this clears the access/refresh tokens from the Token Vault but does **not** revoke them at the IdP; the user can revoke at e.g. https://myaccount.google.com/permissions.

#### 4.4.5 No change needed to the calendar tool

`tools/google_calendar.py` calls Token Vault with `connection="google-oauth2"` and the user's refresh token. Token Vault transparently uses whichever Google identity is connected — primary login or connected account.

---

## 5. Auth0 dashboard configuration (one-time)

These cannot be automated from code. Without them, runtime calls fail with descriptive `error_description`s; with them, everything Just Works.

| Where | What |
|---|---|
| Applications → your app → **Settings** | Allowed Callback URLs include all of: `http://localhost:8000/callback`, `http://127.0.0.1:8000/callback`, `http://localhost:8000/connections/callback`, `http://127.0.0.1:8000/connections/callback` |
| Applications → your app → **Settings** | Allowed Logout URLs: `http://localhost:8000/`, `http://127.0.0.1:8000/` |
| Applications → your app → **Advanced → Grant Types** | Enable `urn:auth0:params:oauth:grant-type:token-exchange:federated-connection-access-token` (Token Vault) |
| Applications → your app → **Settings** | Disable **Allow Refresh Token Rotation** (Token Vault is incompatible with rotation) |
| APIs → **My Account API** | Activate it |
| APIs → My Account API → **Application Access** | Authorize this app; check `create:me:connected_accounts`, `read:me:connected_accounts`, `delete:me:connected_accounts` |
| APIs → My Account API → **Settings → Access Settings** | Enable "Allow Skipping User Consent" |
| Applications → your app → **Multi-Resource Refresh Token** | Enable MRRT; include the My Account API |
| Authentication → **Social → Google** | Configure with a real Google OAuth client (not Auth0's dev keys); enable the connection for the app |
| Authentication → **Social → GitHub** *(if used)* | Configure with your own GitHub OAuth client; permissions tab → keep only `email` and `read:user` |

---

## 6. External service configuration

### 6.1 Google Cloud Console

For the OAuth client registered in Auth0's Google social connection:

- **Authorized redirect URIs** must include `https://{YOUR_AUTH0_DOMAIN}/login/callback`.
- **OAuth consent screen** must list `https://www.googleapis.com/auth/calendar.readonly` (and any other scopes you want to grant). If the consent screen is in **Testing** mode, your Google account must be on the **Test users** list.

### 6.2 GitHub OAuth (https://github.com/settings/developers, if using)

- **Authorization callback URL** = `https://{YOUR_AUTH0_DOMAIN}/login/callback`.
- Limit the Auth0 GitHub connection's "Permissions" to `email` + `read:user` for a chat demo. The default Auth0 config requests an alarming list (`admin:org`, `delete_repo`, etc.) that maps to "every checkbox at GitHub's consent screen".

---

## 7. Token / scope reference

| Where token lives | What's in it | How we get it |
|---|---|---|
| `request.session["access_token"]` | Audience: `AUTH0_AUDIENCE` (or opaque if unset); scopes per app's API config | Authlib `authorize_access_token` after login |
| `request.session["refresh_token"]` | Auth0 refresh token, **non-rotating**, **MRRT-eligible** | Same; requires `offline_access` in scope |
| Mint-on-demand: My Account API access token | Audience: `https://{AUTH0_DOMAIN}/me/`; scopes: `create/read/delete:me:connected_accounts` | Refresh-token exchange with audience override |
| Mint-on-demand: Google access token | Issued by Token Vault for `connection=google-oauth2` | Federated-connection grant exchange in `tools/google_calendar.py` |

---

## 8. Gotchas and lessons learned

### 8.1 Streaming responses + cookie-based sessions don't mix

Already covered in §4.1. Don't try to mutate `request.session` inside a `StreamingResponse` generator and expect the cookie to land. Split into a read-only stream + a separate writer endpoint.

### 8.2 `connection_scope` on universal `/login` leaks to other IdPs

If you put `connection_scope="https://www.googleapis.com/auth/calendar.readonly"` on a universal-login `/login` and the user picks GitHub (or any non-Google IdP), Auth0 forwards it to that IdP as a literal scope. Non-Google IdPs reject (URLs aren't valid scopes there) and login is broken for that IdP. **Only pass `connection_scope` when you also pin `connection`**, or split into separate routes (`/login` generic, `/connect/google-calendar` pinned).

### 8.3 `connect_code` is in the URL **fragment**, not query string

Already detailed in §4.4.3. The callback handler must be HTML+JS, not a plain redirect-receiving server route, because servers never see fragments.

### 8.4 `redirect_uri_mismatch` errors come from three different actors

When debugging "redirect_uri" errors, identify which actor is rejecting:

| Error wording / page | Actor | Where to fix |
|---|---|---|
| Google OAuth error page with "redirect_uri_mismatch" | Google | Google Cloud Console → Credentials → OAuth client → Authorized redirect URIs |
| GitHub "The redirect_uri MUST match the registered callback URL" | GitHub | https://github.com/settings/developers → OAuth app → Authorization callback URL |
| Auth0 page titled "Be careful!" — "redirect_uri is not associated with this application" | Auth0 | Auth0 dashboard → Applications → your app → Allowed Callback URLs |

The `redirect_uri` query parameter on the URL where you see the error tells you which URI is being rejected. Always copy it from the address bar.

### 8.5 Hostname mismatch (`localhost` vs `127.0.0.1`)

`request.url_for("callback")` returns a URL based on the host the request used. If you visit the app as `http://localhost:8000/login`, the callback URI sent to Auth0 will be `http://localhost:8000/callback`. If you bookmark `127.0.0.1:8000`, you get `127.0.0.1`. Auth0's allowlist must contain both for either browser entry to work.

### 8.6 Disabling Refresh Token Rotation is required

Counterintuitive but required: Token Vault is incompatible with refresh-token rotation. With rotation on, every Token Vault exchange would invalidate the user's refresh token, which would then fail the next exchange. Disable rotation **per app** in the Auth0 dashboard.

### 8.7 GitHub social connection comes pre-configured wide-open

The default Auth0 GitHub social connection requests an enormous scope list (`admin:org`, `delete_repo`, `repo`, `admin:public_key`, `notifications`, etc.). Trim to `email` + `read:user` for a login-only flow before any real user sees that scary consent page.

### 8.8 New OAuth clients need their own callback URI registered

Creating a fresh GitHub OAuth app and pointing Auth0 at its `client_id` doesn't automatically give it the Auth0 callback URI. Set **Authorization callback URL** on the GitHub OAuth app to `https://{AUTH0_DOMAIN}/login/callback` exactly. Same dance per Google OAuth client.

### 8.9 Refresh tokens issued before scope changes don't carry the new scopes

When you add scopes to the Authlib `scope` (e.g., the connected_accounts scopes), users with an existing session/refresh token won't have those scopes in their refresh-token grant. The first My Account API mint will return `invalid_scope`. **Fix: have the user log out and back in** to get a fresh refresh token with the new scope set.

### 8.10 `uvicorn --reload` only watches `*.py`

It does not restart on `.env` edits, and module-import-time reads of `os.environ[...]` (in `oauth.register(...)`, `app.add_middleware(...)`) are captured at process start. **Restart the process when changing env vars.**

### 8.11 Python 3.14 + corporate zero-trust agents

Some corporate zero-trust network agents (Prisma Access, ZScaler, Cato) intercept TLS at a level that breaks Python 3.14's stdlib `socket` module — every connection fails with `OSError: [Errno 9] Bad file descriptor`. `curl` works, `pip` doesn't.

**Workaround**: rebuild the venv with Python 3.11 (or 3.12/3.13). On those versions, plain `pip` may *still* fail under the agent — use `uv pip install -r requirements.txt` instead. `uv` (https://github.com/astral-sh/uv) ships its own native HTTP client that bypasses the issue.

---

## 9. End-to-end verification checklist

### Smoke test

1. `curl http://127.0.0.1:8000/` → 200.
2. Hit `/login` → bounces to Auth0 universal login.

### Auth path

3. Click any IdP → consent → land on `/chat`.
4. `/profile` shows ID token claims with your `name`, `email`, `sub`.

### Profile-aware chat

5. Ask the chat: *"What's my email?"* — model should respond with your email.

### Token Vault calendar (primary Google login route)

6. Log out → click `http://127.0.0.1:8000/connect/google-calendar` → Google consent (Calendar scope visible) → `/chat`.
7. Ask: *"What's on my calendar this week?"* — model invokes `list_upcoming_calendar_events`, server mints a Google token via Token Vault, events stream back as natural language.

### Connected Accounts (non-Google primary login)

8. Log out → log in via a non-Google IdP → `/connections`.
9. Click **Connect Google Account** → Google consent → callback page flashes → `/connections?success=connected`. Google row appears in the list.
10. Ask the chat about the calendar — Token Vault now finds the connected Google identity and the tool succeeds.
11. Click **Disconnect** → row disappears. Re-asking the chat returns a Token Vault error with `error_description` indicating no connected account.

---

## 10. Future improvements (deferred)

- **Step-up consent UX** when the model wants Google scopes the user hasn't granted. Current behavior surfaces a Token Vault error in chat; better UX is a `TokenVaultInterruptHandler`-style component that links to `/connect/google-calendar` with the additional scope.
- **Server-side session store** (Redis) instead of cookie sessions — `SessionMiddleware`'s ~4KB cookie limit becomes a problem with longer conversations.
- **CSRF protection** on `/chat/save`, `/chat/stream`, `/connections/complete`. Bearer or custom-header check.
- **Additional connections** (Slack, Calendly, GitHub-as-tool). The `connection` is path-parameterized in `/connections/connect/{connection}`; just add the scope mapping in `connections_connect` and a button in `connections.html`.
- **Tool surface beyond Calendar**: Gmail, Drive, etc. Each new tool follows the same pattern as `tools/google_calendar.py` — scope hint at first consent, Token Vault exchange, third-party API client.
