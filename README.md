# CompassZero

A B2B travel-management demo that pairs **Auth0 RBAC + Organizations** with an **AI assistant whose tools are gated by the signed-in user's permissions**. Three roles, three dashboards, one chat that does different things depending on who's asking.

Built as a workshop template — single FastAPI process, official `auth0-fastapi` SDK for login, custom hand-written CSS, mock in-memory data.

## What you get

- **Three roles** wired through Auth0 Roles + a CompassZero API with 11 scopes:
  - **CompassZero admin** — every organization, every customer, every trip; manages organizations and travel agents (creates/deletes Auth0 orgs and agent users via the Management API). Generates contract PDFs for orgs. Cannot add customers — that's the travel agent's job.
  - **Travel agent** — sees only their own organization's customers and bookings; books trips/experiences directly, adds new customers to their org, and approves or denies pending requests from their customers via the dashboard.
  - **Customer** — searches flights/experiences, lists their own trips, adds them to their Google Calendar, and submits booking requests. Cannot book directly — every request goes to their travel agent for approval.
- **Auth0 Organizations** as the multi-tenant boundary — each customer organization is an Auth0 Organization and the user's `org_name` claim drives the data filter.
- **AI chat** with permission-gated tools — the model's tool list is filtered to whatever the user has scope for, so a customer never sees `book_trip` and an admin never sees `create_customer`. Backed by `gpt-4o-mini` (or any function-calling-capable chat model).
- **Documents** at `/documents` — auto-generated CompassZero ↔ org service contracts (one per org) and per-trip invoices. Admins see everything, agents see their org, customers see only their own invoices. Admins + agents can upload PDF/DOCX/TXT. Admins can ask the chat to generate a new contract on demand.
- **Customer → agent approval workflow** — customers run `request_trip` / `request_experience`, the request lands on the agent's dashboard, the agent approves with a CIBA push (using the existing booking flow) or denies inline.
- **Auth0 step-up via CIBA** — destructive admin actions (org create/delete, agent create/delete) and any approval that turns into a real booking trigger a push notification to the user's Guardian-enrolled device.
- **Calendar / Gmail tools via Token Vault** — anyone whose role has at least `read:my_trips` can add their trip to their own Google Calendar or summarize travel email. Customers, agents, and admins all get this.
- **Profile inspector** at `/profile` — shows your role, permissions, organization, raw token claims, and Guardian-MFA enrollment status.

## Architecture (one process)

```
┌──────────────────────────────────┐
│  CompassZero (FastAPI :8000)     │
│                                   │
│  • auth0-fastapi (web SDK)       │  ← login / session / refresh tokens
│  • permissions.py + Roles        │  ← reads `permissions` claim from access token
│  • Jinja templates per role      │  ← role-aware dashboards & lists
│  • tools/compasszero.py          │  ← chat tools, each requires a scope
│  • tools/auth0_management.py     │  ← Management API (orgs, users, roles)
│  • tools/auth0_ciba.py           │  ← CIBA step-up for destructive actions
│  • tools/auth0_my_account.py     │  ← My Account API (Connected Accounts)
│  • tools/google_calendar.py +    │
│    google_gmail.py + Token Vault │  ← Calendar/Gmail tools (any role w/ trips)
│  • tools/documents.py            │  ← stdlib PDF writer (contracts + invoices)
│  • mock_data.py                  │  ← in-memory orgs/customers/trips/docs/requests
│  • app/documents/                │  ← generated PDF cache (gitignored)
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
| **admin** (`compass_admin` role) | KPI cards across all organizations, full `/companies`, full `/customers`, full `/trips`, full `/documents`. Chat: "create a new organization called Wayne Industries", "add a travel agent at acme-inc named Pat Smith email pat@acme.example", "generate a contract for wayne-industries". CIBA push fires on org create/delete and agent create/delete. |
| **travel agent** (`travel_agent` role + member of an Auth0 Org) | Their organization's budget + recent bookings. **Pending approvals** card on the dashboard — Approve fires CIBA, Deny is inline. `/customers` and `/documents` filtered to their org. Chat: "book a flight from JFK to LHR for Jane next month, $1200", "what pending approvals do I have?". |
| **customer** (`customer` role + member of an Auth0 Org + `app_metadata.customer_id`) | Just their trips and their invoices. Chat: "search flights JFK to LHR on 2026-08-12", "request that flight as a trip for me, $850" — submits a request, then the agent approves on their dashboard. Calendar tool also works: "add my next trip to my Google Calendar". |

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
    ├── documents/                  # generated PDF cache (gitignored)
    ├── tools/
    │   ├── compasszero.py          # CompassZero chat tools (~20 of them)
    │   ├── auth0_management.py     # Management API: orgs, users, roles, members
    │   ├── auth0_ciba.py           # CIBA step-up
    │   ├── auth0_my_account.py     # My Account API (Connected Accounts)
    │   ├── documents.py            # stdlib PDF writer + contract/invoice templates
    │   ├── google_calendar.py      # Token Vault → Calendar
    │   └── google_gmail.py         # Token Vault → Gmail
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
        ├── documents.html
        ├── chat.html
        ├── _chat_widget.html
        ├── profile.html
        ├── mfa_enroll.html
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
