# Implementation Reference

A deep dive into how this template works — for workshop attendees who want to understand *why* things are wired the way they are, not just *what* the code does.

For setup and quick start, see [README.md](./README.md). This document focuses on architecture, every non-obvious decision, and a curated list of footguns to avoid.

---

## 1. What the app does

A single-process FastAPI web app that:

1. Logs users in via **Auth0** (Authorization Code Flow with OIDC). Multiple IdPs supported on the universal-login page; an **enterprise SSO connection** (Okta) can also be hot-swapped in for the staff/admin path via the `ADMIN_CONNECTION_NAME` env var.
2. Reads the user's **Auth0 RBAC `permissions` claim** and **Auth0 Organizations `org_name` / `org_id` claims** off the access token, then maps to one of three CompassZero roles: `compass_admin`, `travel_agent`, `customer`.
3. Renders **role-aware Jinja2 dashboards** plus a **streaming chat UI** where the LLM's tool list is filtered to whatever the user's permissions allow.
4. Calls real **Auth0 Management API** endpoints from chat tools to create/delete Auth0 Organizations, create/delete users, assign roles, and add/remove organization members.
5. Triggers **CIBA step-up** (push notifications via Guardian) for destructive actions — admin org management, agent management, trip booking/cancellation, and approving customer booking requests.
6. Calls **Google Calendar / Gmail** on the user's behalf via **Auth0 Token Vault**, using OpenAI function calling.
7. Exposes a **Connected Accounts** page (using the Auth0 **My Account API**) where a user logged in with one IdP can attach a Google identity for Token Vault use, without changing their primary login.
8. Generates **mock contract / invoice PDFs** with a stdlib-only PDF writer and serves them through a permission-checked download route.
9. Implements a **customer → travel-agent booking-approval workflow**: customers submit `request_trip` / `request_experience` chat tools; the request shows up on the agent's dashboard; the agent approves (with CIBA) or denies inline.

Stack: FastAPI · `auth0-fastapi` (Auth0's official web-app SDK; replaces Authlib) · Starlette `SessionMiddleware` · Jinja2 · OpenAI Python SDK · `httpx` · `google-api-python-client`. PDF generation is hand-rolled in Python's stdlib (no external dep).

The SDK auto-registers `/auth/login`, `/auth/callback`, and `/auth/logout` via `register_auth_routes`. Tools (Token Vault, My Account API, Google APIs) are still called directly from the same process via `httpx`.

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
    ├── main.py                        # FastAPI app, all routes, role-aware dashboard, CIBA endpoints
    ├── mock_data.py                   # in-memory orgs/customers/trips/docs/approval requests
    ├── permissions.py                 # access-token claims → user_context (role, scopes, org)
    ├── documents/                     # generated PDFs (gitignored, lazy-seeded)
    ├── tools/
    │   ├── __init__.py
    │   ├── compasszero.py             # role-gated chat tools (search/book/request/manage)
    │   ├── auth0_management.py        # Management API: orgs, users, roles, member ops
    │   ├── auth0_ciba.py              # CIBA step-up (Backchannel Auth + polling)
    │   ├── auth0_my_account.py        # My Account API: connect/list/delete connected accounts
    │   ├── documents.py               # stdlib PDF writer + contract/invoice templates
    │   ├── google_calendar.py         # Token Vault exchange + Calendar API
    │   └── google_gmail.py            # Token Vault exchange + Gmail API
    ├── static/style.css
    └── templates/
        ├── base.html                  # nav, brand, role badge
        ├── home.html
        ├── dashboard.html             # role-aware: admin / agent (+ pending approvals) / customer
        ├── companies.html             # /companies — orgs list + create form
        ├── company_detail.html        # /companies/{id} — drill-down
        ├── customers.html
        ├── trips.html
        ├── trip_detail.html
        ├── documents.html             # role-scoped contracts / invoices / uploads
        ├── chat.html                  # streaming chat full-page
        ├── _chat_widget.html          # reusable chat partial (chips, MD render, streaming)
        ├── profile.html               # raw token claims + Guardian enrollment status
        ├── mfa_enroll.html            # one-shot Guardian enrollment ticket
        ├── connections.html           # connected-accounts management UI
        └── connections_callback.html  # JS shim that reads connect_code from URL fragment
```

---

## 3. Route map

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Home; redirects logged-in users to `/dashboard` |
| GET | `/auth/login` | Universal Auth0 login (any IdP) — auto-registered by SDK |
| GET | `/auth/callback` | OAuth callback — auto-registered by SDK |
| GET | `/auth/logout` | Clears session + Auth0 logout — auto-registered by SDK |
| GET | `/connect/google-calendar` | Redirects to `/auth/login?connection=google-oauth2&connection_scope=...` for federated Calendar/Gmail consent at first login |
| GET | `/dashboard` | Role-aware dashboard. Travel agent branch surfaces pending approval requests for the agent's org. |
| GET | `/profile` | Renders ID/access token claims + Guardian enrollment status |
| GET, POST | `/companies` *(admin)* | Organization list / create — calls Auth0 Management API to create real Auth0 Organizations |
| POST | `/companies/{id}/delete` *(admin)* | Delete an Auth0 Organization (CIBA-gated) |
| GET | `/companies/{id}` | Organization drill-down (members, agents, customers, trips) |
| GET | `/customers` | Customer list, scope-filtered to user's role |
| GET | `/trips`, `/trips/{id}` | Trip list / detail, scope-filtered |
| GET | `/documents` | Role-scoped documents page (contracts / invoices / uploads). Lazy-seeds missing PDFs. |
| GET | `/documents/{id}` | Inline view of a document (PDF/TXT/DOCX). Permission-checked. Add `?download=1` to force attachment. |
| POST | `/documents/upload` *(admin or agent)* | Multipart file upload (PDF/DOCX/TXT, ≤10 MB) |
| POST | `/approvals/{id}/approve` *(agent)* | Approve a customer's pending request — triggers CIBA, then `add_trip` / `add_experience` |
| POST | `/approvals/{id}/deny` *(agent)* | Deny a pending request inline |
| GET | `/mfa/enroll` | Mints a Guardian enrollment ticket so users can register a CIBA-capable factor |
| GET | `/chat` | Full-page chat |
| POST | `/chat/stream` | Streaming chat with tool-calling loop (filters `tool_schemas` by role) |
| POST | `/chat/save` | Persists user+assistant turn to session after the stream closes |
| POST | `/chat/clear` | Wipes the session conversation |
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

### 4.5 Role-aware chat tool surface

`tools/compasszero.py` exposes a `TOOLS` registry where each tool entry has:

```python
"create_travel_agent": {
    "required_scopes": ("manage:companies",),
    "fn": create_travel_agent,
    "schema": {...},  # OpenAI function-tool JSON
}
```

`visible_schemas(ctx)` filters by `all(s in perms for s in required_scopes)` so the model literally never sees tools the user can't call. `dispatch(name, args, ctx)` re-checks server-side and falls back to per-tool `require_any(...)` for tools that need OR semantics across permissions (e.g. `search_flights` is visible whenever a role has `book:trips` OR `read:my_trips`).

Tool catalog by role:

| Role | Tools visible to the LLM |
|---|---|
| `compass_admin` | every list_* / get_* + `create_auth0_organization`, `delete_auth0_organization`, `create_company` (local mock), `create_travel_agent`, `delete_travel_agent`, `generate_contract`, plus all booking tools |
| `travel_agent` | `list_company_trips`, `list_my_customers`, `book_trip`, `book_experience`, `book_customer_experience`, `cancel_trip`, `create_my_customer`, `search_flights`, `search_experiences`, `get_trip_details` |
| `customer` | `list_my_trips`, `get_trip_details`, `search_flights`, `search_experiences`, `request_trip`, `request_experience` (no direct booking) |

Calendar / Gmail tools (`list_upcoming_calendar_events`, `create_calendar_event`, `list_recent_emails`) are gated separately in `main.py` via `_can_use_google_tools(ctx)` — `has_any_permission(ctx, "book:trips", "read:my_trips")`. All three roles qualify.

### 4.6 CIBA step-up for destructive actions

Six chat tools and the dashboard's Approve button trigger Auth0 CIBA (Backchannel Authentication) before mutating state:

```
book_trip · book_customer_experience · cancel_trip
create_auth0_organization · create_travel_agent · delete_auth0_organization
delete_travel_agent · POST /approvals/{id}/approve
```

`tools/auth0_ciba.py` posts to `/bc-authorize`, polls `/oauth/token` with `grant_type=urn:openid:params:grant-type:ciba`, and surfaces a tailored error if the user isn't enrolled. `binding_message` is human-readable (e.g. "Approve booking flight JFK to LHR 2026-08-12") and shown verbatim on the user's Guardian device. The chat-stream UI flashes a "📲 Push notification sent — approve in the Auth0 Guardian app on your phone (waiting up to 3 minutes)..." marker before dispatch so the operator knows where the latency is coming from.

If a user has no Guardian factor enrolled, `step_up` raises `CibaNotEnrolledError`. Chat tools surface a deep link to `/mfa/enroll`; dashboard endpoints redirect there with `return_to=`.

### 4.7 Auth0 Management API for org / agent lifecycle

`tools/auth0_management.py` wraps a small, focused slice of `/api/v2`:

| Helper | Endpoint | Used by |
|---|---|---|
| `create_organization` / `delete_organization` / `get_organization_by_name` / `list_organizations` / `list_organization_members` | `/api/v2/organizations*` | admin org CRUD; `reconcile_companies_with_auth0` for the local↔Auth0 mirror |
| `create_database_user` / `delete_user` / `find_user_by_email` | `/api/v2/users*` | `create_travel_agent` / `delete_travel_agent` |
| `add_organization_member` / `remove_organization_member` | `/api/v2/organizations/{id}/members*` | agent lifecycle |
| `assign_organization_member_roles` | `/api/v2/organizations/{id}/members/{uid}/roles` | assigns `travel_agent` role |
| `find_role_by_name` / `get_role_id` | `/api/v2/roles?name_filter=` (cached) | one-time `travel_agent` role lookup |
| `create_enrollment_ticket` / `list_user_enrollments` | `/api/v2/guardian/*` | `/mfa/enroll` page + dashboard "needs enrollment" nudge |

All calls go through `_get_management_token()` which mints a client-credentials token against the Management API audience and caches it for the token lifetime. Required M2M scopes are listed in `AUTH0_SETUP.md`.

`reconcile_companies_with_auth0()` runs at the top of the admin's `/dashboard` and `/companies` routes. It pulls the live org list from Auth0 and reconciles into the local `COMPANIES` mock — adding new orgs, removing deleted ones, and refreshing display names. Rate-limited via `SYNC_TTL_SECONDS = 120` and an `asyncio.Lock` so concurrent requests don't double-sync.

### 4.8 Documents (mock contracts + invoices, stdlib PDF writer)

`tools/documents.py` is a hand-rolled PDF 1.4 writer — no `fpdf`, `reportlab`, or other dependency. `write_pdf(path, blocks)` emits a one-page Letter PDF with two built-in fonts (Helvetica and Helvetica-Bold), word-wrapping each block to fit the page width by character count (we don't bundle Helvetica AFM metrics, so `_WRAP_BY_SIZE` is a coarse map by font size).

Two templates:
- `generate_contract_pdf(org_name, display_name)` → `app/documents/contract-{slug}.pdf` — mock services agreement.
- `generate_invoice_pdf(trip, customer, company)` → `app/documents/invoice-{trip_id}.pdf` — itemized invoice.

`main.py:_ensure_seed_documents()` runs at the top of `/documents` and `/dashboard`. It iterates `COMPANIES` and `TRIPS`, generating any missing PDFs and appending DOCUMENTS rows. Idempotent — checks DOCUMENTS by `(kind, key)` first.

**Why lazy seed** instead of hooking `add_company` / `add_trip`: keeps `mock_data.py` pure (no I/O), means newly created orgs/trips get docs the next time anyone loads the dashboard or visits `/documents` (no startup latency).

**Why serve through a route** (`GET /documents/{id}`) instead of mounting `app/documents/` as a static dir: we need the role check. `_user_can_view_doc(ctx, doc)` mirrors the page filter:

| Role | Can view |
|---|---|
| `compass_admin` | all docs |
| `travel_agent` | docs where `org_name == ctx.org_name` |
| `customer` | invoices where `customer_id == ctx.customer_id` |

Without `?download=1`, the route emits `Content-Disposition: inline` so PDFs/TXT open in a new tab. With `?download=1` (or by passing `filename=` to `FileResponse`), the browser downloads.

**Admin-only contract generation chat tool** `generate_contract` calls `generate_contract_pdf` and `add_document`. No CIBA — generation is low-risk. The Auth0 org must already exist locally (seeded by `reconcile_companies_with_auth0` after `create_auth0_organization`).

Uploads (`POST /documents/upload`): multipart form, extension-allowlist `{.pdf, .docx, .txt}`, 10 MB cap. Saved with a timestamped prefix to dedupe filenames. Scoped to the uploader's `org_name`.

### 4.9 Customer → travel-agent booking-approval workflow

The premise: customers can search but not book directly. They run `request_trip` / `request_experience` (chat tools gated on `read:my_trips`), which append a row to `APPROVAL_REQUESTS`:

```python
{
  "id": "req_007",
  "kind": "trip" | "experience",
  "status": "pending" | "approved" | "denied",
  "customer_id": "cu_jane",
  "org_name": "northwind-corp",   # the agent's org — derived from ctx.org_name
  "details": {...booking args...},
  "trip_id": "" | "tr_013",        # populated when approved
  "experience_id": "" | "ex_007",
  "created_at": "...",
  "decided_at": "...",
  "decided_by": "ag_alex",
  "decision_note": "",
}
```

The travel-agent dashboard branch pulls `get_approval_requests(org_name=org, status="pending")` and renders a card above "Recent bookings". Each row has **Approve** and **Deny** POST forms.

`POST /approvals/{id}/approve` (gated on `book:trips`):
1. Look up the request; ensure `status == "pending"` and `org_name == ctx.org_name`.
2. CIBA step-up against the agent's enrolled device, with binding `Approve booking request {id} for {customer.name}`.
3. Call `add_trip(...)` or `add_experience(...)` from the stored `details`.
4. `update_approval_request(id, status="approved", trip_id=…, decided_at=…, decided_by=ctx.agent_id)`.
5. Redirect back to `/dashboard?success=...`.

`POST /approvals/{id}/deny` is simpler — no CIBA, just status flip with optional `note`.

**No chat tools** for the agent's approve/deny side. The dashboard buttons are the only entry points; the system prompt explicitly steers the LLM away from impersonating that flow.

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
| APIs → **Auth0 Management API** → Machine to Machine Applications → your app | Authorize the app and grant: `create:organizations`, `read:organizations`, `delete:organizations`, `read:organization_members`, `create:organization_members`, `delete:organization_members`, `create:organization_member_roles`, `create:users`, `read:users`, `delete:users`, `read:roles`, `create:guardian_enrollment_tickets`, `read:guardian_enrollments`. Without these the chat tools that hit `/api/v2` fall back to error JSON. |

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

### 8.11 Documents are lazy-seeded — newly created orgs/trips don't get PDFs immediately

`_ensure_seed_documents()` only runs at the top of `/dashboard` and `/documents`. If you create an org via the chat (`create_auth0_organization`) and then immediately open `/documents`, you'll see the new contract — but if you book a trip via the chat and inspect `DOCUMENTS` programmatically without revisiting either of those routes, the invoice won't exist yet. Trigger by reloading `/documents`. Same for the `app/documents/` directory: it's gitignored and recreated on first call to `documents_dir()`.

### 8.12 Python 3.14 + corporate zero-trust agents

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
