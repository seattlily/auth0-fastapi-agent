# CompassZero

A B2B travel-management demo that pairs **Auth0 RBAC + Organizations** with an **AI assistant whose tools are gated by the signed-in user's permissions**. Three roles, three dashboards, one chat that does different things depending on who's asking.

Built as a workshop template — single FastAPI process, official `auth0-fastapi` SDK for login, custom hand-written CSS, mock in-memory data.

## What you get

- **Three roles** wired through Auth0 Roles + a CompassZero API with 11 scopes:
  - **CompassZero admin** — every company, every customer, every trip; can create companies and customers.
  - **Travel agent** — sees only their own travel agency's customers and bookings; can book trips and experiences for them.
  - **Customer** — sees only their own bookings; the AI assistant lists trips and experiences but can't write anything.
- **Auth0 Organizations** as the multi-tenant boundary — each travel-agency company is an Auth0 Organization and the user's `org_name` claim drives the data filter.
- **AI chat** with permission-gated tools — the model's tool list is filtered to whatever the user has scope for, so a customer never even sees `book_trip`. Backed by `gpt-4o-mini` (or any function-calling-capable chat model).
- **Calendar / Gmail tools via Token Vault** — agents and admins can ask the assistant to add a trip to their calendar or summarize itineraries from email. Same Connected Accounts flow as before, gated to `book:trips` permission.
- **Profile inspector** at `/profile` — shows your role, permissions, organization, and raw token claims.

## Architecture (one process)

```
┌──────────────────────────────────┐
│  CompassZero (FastAPI :8000)     │
│                                   │
│  • auth0-fastapi (web SDK)       │  ← login / session / refresh tokens
│  • permissions.py + Roles        │  ← reads `permissions` claim from access token
│  • Jinja templates per role      │  ← role-aware dashboards & lists
│  • tools/compasszero.py          │  ← chat tools, each requires a scope
│  • tools/google_calendar.py +    │
│    google_gmail.py + Token Vault │  ← Calendar/Gmail tools (agents+admins only)
│  • mock_data.py                  │  ← in-memory companies/customers/trips
└──────────────────────────────────┘
```

No separate backend, no React, no LangGraph. The chat tools are Python functions with permission checks; the model only ever sees tools the user has scope for.

## Quick start

### 0. Auth0 dashboard — generic onboarding

Read [`AUTH0_SETUP.md`](./AUTH0_SETUP.md) end-to-end first. It walks through the Regular Web App, callback URLs, Token Vault grant, refresh-token-rotation off, MRRT, and Google/GitHub social connections.

### 0.5. Auth0 dashboard — CompassZero specifics

Read [`AUTH0_API_DEFINITIONS.md`](./AUTH0_API_DEFINITIONS.md). It defines:
- The CompassZero API and its 11 scopes
- The 3 Roles (`compass_admin`, `travel_agent`, `customer`)
- The 3 Auth0 Organizations
- Test users + `app_metadata`
- The post-login Action that propagates `customer_id` / `agent_id` claims

### 1. Run the app

```bash
cd app
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill in AUTH0_DOMAIN, AUTH0_CLIENT_ID, AUTH0_CLIENT_SECRET,
# APP_SECRET_KEY (python3 -c "import secrets; print(secrets.token_hex(32))"),
# OPENAI_API_KEY. AUTH0_AUDIENCE defaults to https://compasszero.api.

uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 → Sign in → land on `/dashboard`.

### 2. Try each role

| Login as | What you should see |
|---|---|
| **admin** (`compass_admin` role) | KPI cards across all companies, full `/companies`, full `/customers`, full `/trips`. Chat: "list all customers", "create a new company called Wayne Industries with budget 500k". |
| **travel agent** (`travel_agent` role + member of an Auth0 Org) | Their company's budget + recent bookings. `/customers` filtered to their org. Chat: "book a flight from JFK to LHR for Jane next month, $1200". |
| **customer** (`customer` role + member of an Auth0 Org + `app_metadata.customer_id`) | Just their trips. The chat refuses booking and only sees `list_my_trips`. |

## Project layout

```
.
├── README.md                       # this file
├── IMPLEMENTATION.md               # deep-dive reference
├── AUTH0_SETUP.md                  # Auth0 dashboard onboarding (generic)
├── AUTH0_API_DEFINITIONS.md        # CompassZero-specific Auth0 setup
├── .gitignore
└── app/
    ├── .env.example
    ├── requirements.txt
    ├── main.py                     # routes + chat tool dispatch
    ├── mock_data.py                # in-memory data
    ├── permissions.py              # token → user_context helper
    ├── tools/
    │   ├── compasszero.py          # CompassZero chat tools (10 of them)
    │   ├── google_calendar.py      # Token Vault → Calendar
    │   ├── google_gmail.py         # Token Vault → Gmail
    │   └── auth0_my_account.py     # My Account API (Connected Accounts)
    ├── static/style.css
    └── templates/
        ├── base.html
        ├── home.html
        ├── dashboard.html
        ├── companies.html
        ├── company_detail.html
        ├── customers.html
        ├── trips.html
        ├── trip_detail.html
        ├── chat.html
        ├── profile.html
        ├── connections.html
        └── connections_callback.html
```

## Known issues

### `redirect_uri_mismatch` errors

Three different actors throw this; the wording is similar but the fix differs. See `IMPLEMENTATION.md` §8 for the diagnostic table.

### Refresh tokens issued before scope changes don't carry the new scopes

Common pitfall when you change Auth0 Roles or add a permission — the user's stale session has an old refresh token. **Log out and log back in.**

### `Allow Offline Access` on the API must be ON

Without it the access token is issued but no refresh token is — and Connected Accounts / chat tools that need Token Vault fail with "no refresh token in session".

## License

MIT — fork it, modify it, use it as a workshop starter.
