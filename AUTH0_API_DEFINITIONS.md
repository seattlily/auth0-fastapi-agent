# Auth0 API + RBAC + Organizations Setup for CompassZero

CompassZero's permission model is driven entirely from Auth0:

- A custom **Auth0 API** defines the scopes (permissions).
- Three **Auth0 Roles** bundle those permissions for the three user types.
- Auth0 **Organizations** represent the customer organizations (one Auth0 Organization per CompassZero customer org). The app calls the Management API to create / delete these at runtime.
- A small **post-login Action** copies user-record IDs into custom claims.
- **CIBA** (push-notification step-up via Guardian) gates every destructive action.

The Python app reads `permissions`, `org_id`, `org_name`, plus two custom claims, off the access/ID token. It does **not** know the role assignments — those live in the dashboard.

This file is the spec the workshop attendee follows. Generic Auth0 onboarding (creating the application, registering callback URLs, Token Vault grant, **Management API M2M scopes**, MRRT, etc.) lives in [`AUTH0_SETUP.md`](./AUTH0_SETUP.md). Read that first — Part 6 in particular covers the M2M scopes the admin chat tools need — then come back here.

---

## 1. The CompassZero API

### 1.1 Create the API

- Auth0 dashboard → **APIs** → **Create API**.
- **Name**: `CompassZero API`
- **Identifier**: `https://compasszero.api`
- **JWT Profile**: Auth0 (default)
- **Signing Algorithm**: RS256 (default)
- Click **Create**.

### 1.2 Settings tab

- **Allow Offline Access**: **ON** — required for refresh tokens (Connected Accounts, Calendar/Gmail tools).
- **Token Lifetime**: leave at default (86400s).
- Save.

### 1.3 RBAC Settings (very important)

- Same API, **Settings** tab → scroll down to **RBAC Settings**.
- **Enable RBAC**: **ON**.
- **Add Permissions in the Access Token**: **ON**.
- Save.

> Without these two toggles, the `permissions` claim won't appear on the access token and every chat tool / route will fall back to `unknown` role.

### 1.4 Permissions tab — define every scope

| Scope | Description |
|---|---|
| `read:my_trips` | View your own bookings (customer). Also gates: customer-side `request_trip` / `request_experience` chat tools, and Calendar/Gmail Token Vault tools (so customers can add their own trips to their own Calendar). |
| `read:company_trips` | View bookings inside your organization (agent) |
| `read:all_trips` | View bookings across all organizations (admin) |
| `read:my_company` | View your own organization (agent, customer) |
| `read:all_companies` | View all organizations (admin) |
| `read:my_customers` | View customers your agency manages (agent) |
| `read:all_customers` | View all customers (admin) |
| `book:trips` | Create/modify trips (agent, admin). Also gates the agent's `POST /approvals/{id}/{approve,deny}` dashboard endpoints. |
| `book:experiences` | Create/modify experiences (agent, admin) |
| `manage:companies` | Manage organizations (admin). Gates `create_auth0_organization` / `delete_auth0_organization` / `create_travel_agent` / `delete_travel_agent` / `generate_contract` chat tools. |
| `manage:agents` | Reserved (admin) — currently same effective surface as `manage:companies` for the role-detection logic in `permissions.py`. Kept in case you want to split agent CRUD from org CRUD later. |

Add each one (Permission name + description). Save.

> **Note**: there is **no `book:*` permission for customers**. The "customer requests, agent approves" workflow is enforced at the chat-tool layer: customers' `request_trip` / `request_experience` tools are gated on `read:my_trips`; only the agent's dashboard buttons can convert a request into a real booking via `book:trips`.

### 1.5 Application Access tab

Authorize your CompassZero RWA for this API. Set the access type to **User-Delegated** (or Both User-Delegated and Client). Save.

### 1.6 `.env`

```
AUTH0_AUDIENCE=https://compasszero.api
```

---

## 2. Roles

Auth0 dashboard → **User Management → Roles** → **Create Role** for each of:

### `compass_admin`

Description: *Full administrative access to CompassZero.*

Permissions (click **Add Permissions**, pick the CompassZero API, select all 11):
- `read:my_trips`, `read:company_trips`, `read:all_trips`
- `read:my_company`, `read:all_companies`
- `read:my_customers`, `read:all_customers`
- `book:trips`, `book:experiences`
- `manage:companies`, `manage:agents`

### `travel_agent`

Description: *Manages bookings for a single company.*

Permissions:
- `read:my_company`
- `read:company_trips`
- `read:my_customers`
- `book:trips`
- `book:experiences`

### `customer`

Description: *Reads their own bookings.*

Permissions:
- `read:my_trips`
- `read:my_company`

---

## 3. Auth0 Organizations

Each customer organization is an Auth0 Organization. The mock data ships with 3 — match these org names exactly. Admins can create / delete additional ones at runtime via the chat tools (`create_auth0_organization`, `delete_auth0_organization`); the local `COMPANIES` list reconciles with Auth0 every time the admin opens `/dashboard` or `/companies`.

### 3.1 Enable Organizations on the application

- Auth0 dashboard → **Applications → CompassZero RWA → Settings**.
- Scroll to **Organizations** section.
- **Type of Users**: *Both User Types* (so admins without an org can still log in).
- **Login Flow**: *Prompt for Credentials* (the easiest UX — Auth0 will route business users via their org automatically).
- Save Changes.

### 3.2 Create the three orgs

- Auth0 dashboard → **Organizations → Create Organization**.

For each:

| `Name` (slug) | `Display Name` |
|---|---|
| `northwind-corp` | Northwind Corp |
| `acme-inc` | Acme Inc |
| `globex-ltd` | Globex Ltd |

For each org → **Connections** tab → enable **Username-Password-Authentication** (and any social connections you want available there).

For each org → **Applications** tab → enable the CompassZero RWA.

> Each org's `Name` must match exactly the value in `app/mock_data.py` → `COMPANIES[i]["org_name"]`. The Python code joins on this string.

---

## 4. Test users (6 of them)

Auth0 dashboard → **User Management → Users → Create User**. Use Username-Password-Authentication. Suggested logins:

| Email | Role | Org membership | `app_metadata` |
|---|---|---|---|
| `admin@compasszero.com` | `compass_admin` | none | (empty) |
| `alex@compasszero.com` | `travel_agent` | `northwind-corp` | `{"agent_id": "ag_alex"}` |
| `camila@compasszero.com` | `travel_agent` | `acme-inc` | `{"agent_id": "ag_camila"}` |
| `jane@northwind.example` | `customer` | `northwind-corp` | `{"customer_id": "cu_jane"}` |
| `marco@acme.example` | `customer` | `acme-inc` | `{"customer_id": "cu_marco"}` |
| `oscar@globex.example` | `customer` | `globex-ltd` | `{"customer_id": "cu_oscar"}` |

For each user:

1. Set a password (note it for testing).
2. Open the user → **Roles** tab → **Assign Roles** → pick the appropriate role. *(For admin, this is enough — they don't log in via an org.)*
3. Open the user → **Details** → click pencil next to **app_metadata** → paste the JSON (`{"agent_id": "ag_alex"}` etc.) → save.
4. For the agents and customers (not the admin), open the relevant Organization → **Members** tab → **Add Members** → add this user.
5. **Important — assign the role *again* at the org-member level:** still on the Organization → Members tab, click the user's row → in the member detail panel find **Roles** → **Assign Roles** → pick the same role. **Without this step, the user's `permissions` array on the access token will be empty when they log in via an org**, even though they have the user-level role.

---

## 5. Post-login Action — propagate `customer_id` / `agent_id` to claims

`org_id` and `org_name` are placed on the token automatically by Auth0 Organizations. The `permissions` array is placed on the token automatically by RBAC. We just need a tiny Action to expose `app_metadata.agent_id` and `app_metadata.customer_id`:

- Auth0 dashboard → **Actions → Library → Custom → Build Custom**.
- Name: `CompassZero — Token Enrichment`
- Trigger: **Login / Post Login**

Paste this code:

```js
exports.onExecutePostLogin = async (event, api) => {
  const ns = "https://compasszero.app/";
  const meta = event.user.app_metadata || {};

  if (meta.customer_id) {
    api.accessToken.setCustomClaim(ns + "customer_id", meta.customer_id);
    api.idToken.setCustomClaim(ns + "customer_id", meta.customer_id);
  }
  if (meta.agent_id) {
    api.accessToken.setCustomClaim(ns + "agent_id", meta.agent_id);
    api.idToken.setCustomClaim(ns + "agent_id", meta.agent_id);
  }

  // Auth0 puts `org_id` on the token automatically when the user logs
  // in via an Organization, but `org_name` isn't always present. Set
  // it explicitly so the app can join on the slug.
  if (event.organization && event.organization.name) {
    api.accessToken.setCustomClaim(ns + "org_name", event.organization.name);
    api.idToken.setCustomClaim(ns + "org_name", event.organization.name);
  }
};
```

Click **Deploy**. Then **Actions → Triggers → Post Login** → drag your action from the right sidebar into the flow between Start and Complete → **Apply**.

---

## 6. Verify

After all of the above:

1. Restart your local app.
2. Log in as `jane@northwind.example` (customer). She auto-logs into the Northwind org. `/profile` should show:
   - `org_name: northwind-corp`
   - `permissions: ["read:my_trips", "read:my_company"]`
   - `https://compasszero.app/customer_id: cu_jane`
3. `/dashboard` shows Jane's bookings.
4. `/documents` shows only her invoices.
5. Chat: "Search flights JFK to LHR on 2026-08-12" → returns options. "Request that flight, $850 round trip 2026-08-19" → submits a pending request. `/companies` redirects (no permission).
6. Log out, log in as `alex@compasszero.com` (Northwind agent). Org prompt shows Northwind. `/dashboard` shows the budget, recent bookings, **and a Pending approvals card with Jane's request**. Click **Approve** → CIBA push fires (you must be enrolled — see step 8); approving creates the trip and Jane sees it on her `/trips`.
7. Log out, log in as `admin@compasszero.com`. No org prompt. `/dashboard` shows global KPIs. `/companies` lists all 3 (or more, after you create some). Chat: "Add a travel agent at acme-inc named Pat Smith email pat@acme.example" → CIBA push, real Auth0 user is created and added to the org with the `travel_agent` role. Chat: "Generate a CompassZero contract for acme-inc" → new PDF appears on `/documents`.
8. **First-time MFA enrollment** — admins and agents both need a Guardian factor for CIBA. The dashboard surfaces a one-shot enrollment nudge; clicking it opens `/mfa/enroll` which mints a Guardian ticket and renders a QR. Scan it with the Auth0 Guardian app on your phone.

---

## Summary checklist

- [ ] CompassZero API created with `https://compasszero.api`
- [ ] RBAC enabled + permissions added to access token
- [ ] All 11 permissions defined on the API
- [ ] Three Roles created and permissions assigned (the `travel_agent` Role name must be exact — `create_travel_agent` resolves it by name via `find_role_by_name`)
- [ ] Organizations enabled on the application
- [ ] Three Organizations created with matching `org_name` (admins can add more at runtime)
- [ ] Six test users created with role + (where relevant) org membership + `app_metadata`
- [ ] Post-login Action deployed and added to the Login flow
- [ ] Admin and agent test users have at least one **Guardian factor enrolled** (or are ready to enroll via `/mfa/enroll`) — required for CIBA
- [ ] Application has the **Management API M2M scopes** authorized per [`AUTH0_SETUP.md` Part 6](./AUTH0_SETUP.md#part-6--auth0-management-api-m2m-scopes)
- [ ] `AUTH0_AUDIENCE=https://compasszero.api` in `.env`
- [ ] Restart uvicorn, log in, verify `/profile`
