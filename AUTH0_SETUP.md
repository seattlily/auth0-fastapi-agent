# Auth0 Dashboard Setup — Full Walkthrough

Every Auth0 (and Google / GitHub) dashboard step needed to take this app from a fresh checkout to a working end-to-end demo with login, AI chat, calendar, gmail, and connected accounts.

Order matters — later steps depend on values you'll record from earlier ones. Read top-to-bottom, do as you go.

> **Where you'll be**: most steps live at https://manage.auth0.com — pick the tenant (top-left dropdown) you want this app to live in. A few steps go to https://console.cloud.google.com (for the Google federated login) and https://github.com/settings/developers (for GitHub).

---

## Part 1 — Auth0 Application (Regular Web App)

This is the OAuth client your FastAPI app authenticates as.

### 1.1 Create the application

- Auth0 dashboard → **Applications** → **Create Application**.
- Name: anything (e.g., `auth0-fastapi-agent`).
- Type: **Regular Web Applications**. Click **Create**.

### 1.2 Configure URLs

Open the new app's **Settings** tab. Scroll down to **Application URIs**.

- **Allowed Callback URLs** — paste this list, comma-separated:
  ```
  http://localhost:8000/auth/callback,
  http://127.0.0.1:8000/auth/callback,
  http://localhost:8000/connections/callback,
  http://127.0.0.1:8000/connections/callback
  ```
  The `auth0-fastapi` SDK auto-registers the OAuth callback at **`/auth/callback`** (note the `/auth/` prefix). Both `localhost` and `127.0.0.1` are needed because the redirect URI sent to Auth0 is built from whichever host you visit the app at.

- **Allowed Logout URLs**:
  ```
  http://localhost:8000/, http://127.0.0.1:8000/
  ```

- Scroll to the bottom → **Save Changes**. (Easy to miss; the button is at the very bottom of the page.)

### 1.3 Record secrets for `.env`

From the **Settings** → **Basic Information** section at the top of the same page, copy:

- **Domain** → `AUTH0_DOMAIN` (e.g., `your-tenant.us.auth0.com`)
- **Client ID** → `AUTH0_CLIENT_ID`
- **Client Secret** (click "show") → `AUTH0_CLIENT_SECRET`

### 1.4 Refresh token rotation must be **off**

Still on the same Settings page, scroll to **Refresh Token Rotation**. Toggle **off**. Auth0 Token Vault is incompatible with rotation — every Token Vault exchange would silently invalidate the user's refresh token.

### 1.5 (Application-level) JWT signature algorithm

Scroll all the way down → expand **Advanced Settings** → **OAuth** sub-tab → **JSON Web Token (JWT) Signature Algorithm**. Should be `RS256`. (Default; change if it isn't.)

### 1.6 Token Vault + Client Credentials grant types

Same Advanced Settings panel → **Grant Types** sub-tab.

Make sure these are checked:
- **Authorization Code** (default for RWA)
- **Refresh Token** (default; needed for `offline_access`)
- **Token Vault** — labeled exactly like that in newer dashboards, or shown as the long URN `urn:auth0:params:oauth:grant-type:token-exchange:federated-connection-access-token` in older ones.
- **Client Credentials** — needed because the same app acts as the M2M client for Auth0 Management API calls (creating organizations, users, organization members, role assignments, Guardian enrollment tickets). If you'd rather use a dedicated M2M app, set `AUTH0_MGMT_CLIENT_ID` / `AUTH0_MGMT_CLIENT_SECRET` in `.env` and leave Client Credentials off here — but the simpler default reuses this client.
- **CIBA** (`urn:openid:params:grant-type:ciba`) — required for the step-up push notifications used on destructive admin / agent actions.

You can leave **Implicit** and **Password** unchecked — neither is used by this app.

Save Changes.

> **Why this matters**: without the Token Vault grant, every federated tool call (calendar, gmail) returns a 403 with `Grant type ... not allowed for the client.` Without Client Credentials, every Management API call (create/delete org, create/delete agent, generate enrollment ticket) fails with `unauthorized_client`. Without CIBA, every booking / org-create / agent-create returns a "Step-up authentication failed" error before mutating anything.

---

## Part 2 — Custom Auth0 API (the `AUTH0_AUDIENCE`)

This is the API your app's access token is *for*. Without it, Auth0 issues a JWE-encrypted userinfo-only token that can't be inspected. With it, you get a proper RS256-signed JWT with claims.

### 2.1 Create the API

- Auth0 dashboard → **APIs** → **Create API**.
- **Name**: anything (e.g., `AI Agent API`).
- **Identifier**: a unique URL string. Doesn't have to resolve. Use something memorable like `https://ai-agent-api.example.com`. Whatever you pick, this is the value of `AUTH0_AUDIENCE` in `.env`.
- **JSON Web Token (JWT) Profile**: **Auth0** (default) is fine.
- **Signing Algorithm**: **RS256** (default).
- Click **Create**.

### 2.2 Authorize the application to use this API

- Click the newly created API.
- Tab labeled either **Application Access** or **Machine to Machine Applications** (Auth0 has been migrating between names — both surface the same control).
- Find your `auth0-fastapi-agent` Regular Web App in the list and **toggle it on**.
- If you're prompted for an access type, choose **User-Delegated Access** (or both User-Delegated and Client Access if you want flexibility — the user-delegated path is what the login flow uses).
- Save / Authorize.

### 2.3 (Optional) Add this audience to `.env`

```
AUTH0_AUDIENCE=https://ai-agent-api.example.com
```

If you skip this, login still works but the access token will be opaque (JWE) and `/profile` won't show its claims. The chat, calendar, gmail, and connected-accounts features all work fine without `AUTH0_AUDIENCE` — it's primarily for visibility into the access token.

---

## Part 3 — My Account API + Connected Accounts

The Connected Accounts page (`/connections`) talks to Auth0's **My Account API**, which has its own audience and scope set.

### 3.1 Activate the My Account API

- Auth0 dashboard → **APIs**.
- You'll see a banner offering to activate the **Auth0 My Account API**. Click **Activate**.
- (If you don't see the banner, the My Account API is already activated for your tenant.)

### 3.2 Authorize your application + grant scopes

- APIs → **Auth0 My Account API** → **Application Access** tab (or whatever your dashboard calls the application-grant tab).
- Find `auth0-fastapi-agent` → click the chevron / edit icon to expand.
- Check these three scopes:
  - `create:me:connected_accounts`
  - `read:me:connected_accounts`
  - `delete:me:connected_accounts`
- Save.

### 3.3 Allow Skipping User Consent

- Auth0 My Account API → **Settings** tab → scroll to **Access Settings** → toggle **Allow Skipping User Consent** to **on**.

This avoids an extra Auth0-side consent screen during the Connected Accounts flow.

### 3.4 Multi-Resource Refresh Token (MRRT)

- Auth0 dashboard → **Applications → auth0-fastapi-agent → Settings**.
- Scroll to **Multi-Resource Refresh Token** (it's a section, not a tab).
- Toggle **Multi-Resource Refresh Token** **on**.
- Make sure **Auth0 My Account API** is checked in the resource list.
- Save Changes.

> **Why this matters**: MRRT is what lets the same refresh token be exchanged for tokens scoped to multiple Auth0 audiences (your custom API *and* the My Account API). Without MRRT, the connected-accounts flow fails with `invalid_grant` or `invalid_scope`.

---

## Part 4 — Google as a federated connection

For Token Vault to mint Google access tokens (Calendar, Gmail), Auth0 needs a working Google social connection backed by your own Google Cloud OAuth client.

### 4.1 Create a Google OAuth client (Google Cloud Console)

- https://console.cloud.google.com → pick or create a project.
- Left nav → **APIs & Services → Credentials**.
- **Create Credentials → OAuth client ID**.
- Application type: **Web application**.
- Name: anything (e.g., `Auth0 federated client`).
- **Authorized redirect URIs** — add this **exact** URL:
  ```
  https://YOUR-TENANT.auth0.com/login/callback
  ```
  Replace `YOUR-TENANT.auth0.com` with your `AUTH0_DOMAIN` from §1.3. (No trailing slash. Must be `https`.)
- Create. Copy the **Client ID** and **Client Secret** that appear.

### 4.2 OAuth consent screen — add scopes

Same project in Google Cloud Console:

- **APIs & Services → OAuth consent screen** (or in the new IAM UI, "Branding").
- Click **Edit App** → **Scopes** → **Add or Remove Scopes**.
- Add:
  - `https://www.googleapis.com/auth/calendar.events` (read + write events)
  - `https://www.googleapis.com/auth/gmail.readonly` (read messages)
- Save.

If your consent screen is in **Testing** mode, also add your own Google account to the **Test users** list (same page, scroll down). Otherwise Google will reject the consent flow with "this app isn't verified".

### 4.3 Enable the Gmail and Calendar APIs

In the same Google Cloud project, enable both APIs:

- https://console.cloud.google.com/apis/library/gmail.googleapis.com → **Enable**
- https://console.cloud.google.com/apis/library/calendar-json.googleapis.com → **Enable**

(Calendar should already be on for most projects; Gmail typically needs to be enabled explicitly.)

### 4.4 Plug Google into Auth0

- Auth0 dashboard → **Authentication → Social → Google**.
- Toggle the connection on.
- **Client ID**: paste the value from §4.1.
- **Client Secret**: paste the value from §4.1.
- **Permissions** tab — select what gets requested at first consent. This app handles scope granting via `connection_scope` in code, so the dashboard's permission checkboxes can stay at defaults (`profile`, `email`).
- Save.
- **Applications** tab on the same Google connection screen → make sure `auth0-fastapi-agent` is toggled on.

> **Why your own Google OAuth client?** Auth0 ships a "developer keys" mode for the Google connection that's fine for prototyping but doesn't work with Token Vault — the federated tokens you need are only issued for connections backed by your own OAuth client.

---

## Part 5 — GitHub as a federated connection (optional)

Skip this if you only need Google login. Otherwise:

### 5.1 Create a GitHub OAuth app

- https://github.com/settings/developers → **OAuth Apps** → **New OAuth App**.
- **Application name**: anything.
- **Homepage URL**: `http://localhost:8000` (or wherever your prod URL will live).
- **Authorization callback URL**: `https://YOUR-TENANT.auth0.com/login/callback` (same Auth0 callback as Google, with your tenant domain).
- Register application.
- Copy the **Client ID**. Click **Generate a new client secret** and copy that too.

### 5.2 Plug GitHub into Auth0

- Auth0 dashboard → **Authentication → Social → GitHub**.
- Toggle on.
- **Client ID** and **Client Secret**: paste from §5.1.
- **Permissions** tab — uncheck everything except `email` and `read:user`. The default GitHub social-connection config is alarming (`admin:org`, `delete_repo`, `repo`, `notifications`...) and unnecessary for a chat-demo login.
- Save.
- **Applications** tab → toggle `auth0-fastapi-agent` on.

---

## Part 6 — Auth0 Management API M2M scopes

The CompassZero admin tools (and the `/mfa/enroll` page) call the Auth0 Management API with a client-credentials token minted from this same Regular Web App (or a dedicated M2M client if you set `AUTH0_MGMT_CLIENT_ID` / `_SECRET`).

### 6.1 Authorize the application

- Auth0 dashboard → **Applications** → **APIs**, then pick the **Auth0 Management API** (it's there by default — you don't create it).
- **Machine to Machine Applications** tab → find your app (`auth0-fastapi-agent` or whatever you named it) → toggle **Authorized** on.
- Click the down-arrow on the right to expand the scope list.

### 6.2 Grant these scopes

Tick **all** of the following:

| Scope | Used by |
|---|---|
| `create:organizations` | `create_auth0_organization` chat tool / `POST /companies` form |
| `read:organizations` | dashboard reconcile, drill-down |
| `delete:organizations` | `delete_auth0_organization` chat tool / `POST /companies/{id}/delete` |
| `read:organization_members` | `/companies/{id}` member list |
| `create:organization_members` | `create_travel_agent` |
| `delete:organization_members` | `delete_travel_agent` |
| `create:organization_member_roles` | `create_travel_agent` (assigns the `travel_agent` Role) |
| `create:users` | `create_travel_agent` (creates the Auth0 user in the database connection) |
| `read:users` | `delete_travel_agent` (looks the user up by email) |
| `delete:users` | `delete_travel_agent` (removes the Auth0 user) |
| `read:roles` | `create_travel_agent` (resolves the `travel_agent` Role ID once, cached) |
| `create:guardian_enrollment_tickets` | `/mfa/enroll` mints a one-shot Guardian ticket |
| `read:guardian_enrollments` | dashboard "needs enrollment" nudge + `/profile` enrollment table |

Click **Update**.

> **Why this matters**: every chat tool that mutates Auth0 catches `ManagementError` and returns the raw Auth0 error JSON to the LLM. Missing a scope shows up to the user as "Auth0 Management API ... failed (403): insufficient scope" — quick to spot, but easier to set up correctly first.

> **Dedicated M2M alternative**: if you'd rather not give the Regular Web App these scopes, create a separate **Machine to Machine** application, authorize **it** to the Management API with the scopes above, and set `AUTH0_MGMT_CLIENT_ID` and `AUTH0_MGMT_CLIENT_SECRET` in `.env`. `tools/auth0_management.py:_client_credentials()` falls back to those when set.

---

## Part 7 — Verification checklist

Before testing the app at runtime, walk this list. Most "it doesn't work" reports come down to one of these being missed:

| ✓ | Check |
|---|---|
| ☐ | App's Allowed Callback URLs include all 4 (`/auth/callback` and `/connections/callback`, on both `localhost` and `127.0.0.1`) |
| ☐ | App's **Refresh Token Rotation** is **off** |
| ☐ | App's **Token Vault** grant type is enabled (Advanced → Grant Types) |
| ☐ | A custom Auth0 API exists with `RS256` and your application is authorized for it (User-Delegated Access) |
| ☐ | `AUTH0_AUDIENCE` in `.env` matches the API's Identifier exactly |
| ☐ | My Account API is activated, your app is authorized, all 3 connected_accounts scopes are checked |
| ☐ | MRRT is enabled on the application, including the My Account API |
| ☐ | Google OAuth client has `https://YOUR-TENANT.auth0.com/login/callback` in its Authorized redirect URIs |
| ☐ | Google OAuth consent screen includes `calendar.events` and `gmail.readonly`; Test users list includes your account if in Testing mode |
| ☐ | Gmail API and Calendar API are both **enabled** on the Google Cloud project |
| ☐ | Auth0 Google social connection has *your* Client ID + Secret (not the dev keys) and your app is toggled on under its Applications tab |
| ☐ | (If using GitHub) Auth0 GitHub social connection has narrow scopes (`email` + `read:user`); your app is toggled on |
| ☐ | Auth0 Management API → M2M tab: your app (or a dedicated M2M) is authorized for the 13 scopes listed in Part 6 |
| ☐ | App's grant types include **Client Credentials** (Management API) and **CIBA** (step-up) in addition to Authorization Code, Refresh Token, and Token Vault |

---

## Part 8 — How tokens flow through the app

For each of these, the user must have logged in (so a session exists) and a refresh token is in `request.session["refresh_token"]`.

### 7.1 ID Token (returned at login)

- **Issued by**: Auth0, at the `/oauth/token` exchange after the OAuth callback.
- **Format**: signed JWT (RS256), payload is plaintext base64-encoded JSON.
- **Carries**: user identity claims (`sub`, `email`, `name`, `picture`, `iss`, `aud`, `iat`, `exp`, `nonce`).
- **App uses it for**: rendering the user's name/picture, populating the system prompt, the `/profile` page's "ID Token Claims" table.
- **Stored at**: `request.session["id_token_claims"]` (decoded).

### 7.2 Access Token (returned at login)

- **Issued by**: Auth0, in the same `/oauth/token` exchange.
- **Format**: signed JWT (RS256) **if** `AUTH0_AUDIENCE` is set to a custom API. Without an audience, Auth0 returns an opaque or JWE-encrypted userinfo-only token instead.
- **Carries**: `iss`, `aud=<your-AUTH0_AUDIENCE>`, `sub`, `iat`, `exp`, `azp`, `scope`. Plus any custom claims you add via Auth0 Actions.
- **App uses it for**: technically nothing right now — the app holds it for inspection at `/profile`. Tools mint their own tokens (see below).
- **Stored at**: `request.session["access_token"]` (raw).

### 7.3 Refresh Token (returned at login when `offline_access` scope is requested)

- **Issued by**: Auth0, when the OAuth scope includes `offline_access`. This template requests it by default.
- **Format**: opaque string. Never decode; just send it back.
- **Property**: **non-rotating** (because Token Vault is incompatible with rotation). The same refresh token is good for the lifetime of the user's grant.
- **App uses it for**: minting on-demand tokens for both the My Account API (Connected Accounts) and Token Vault (Google federated tokens).
- **Stored at**: `request.session["refresh_token"]`.

### 7.4 My Account API access token (minted on demand)

- **Issued by**: Auth0, via a `/oauth/token` call with `grant_type=refresh_token`, `audience=https://YOUR-TENANT.auth0.com/me/`, `scope=*:me:connected_accounts`.
- **Format**: opaque to the app (used as a Bearer token only). The app never inspects it.
- **App uses it for**: every call to `/me/v1/connected-accounts/*` — listing, creating, completing, and deleting connected accounts. Code lives in `tools/auth0_my_account.py`.
- **Lifetime**: short-lived (a few minutes); minted fresh per request.

### 7.5 Federated access token (Token Vault — minted on demand)

- **Issued by**: Auth0, via a `/oauth/token` call with `grant_type=urn:auth0:params:oauth:grant-type:token-exchange:federated-connection-access-token`, `subject_token=<refresh_token>`, `subject_token_type=refresh_token`, `connection=google-oauth2`.
- **Format**: a real Google access token (or whatever the federated connection's IdP issues).
- **App uses it for**: every call from a tool to the third-party API — `googleapiclient.discovery.build("calendar", ...)`, `build("gmail", ...)`, etc. Code lives in `tools/google_calendar.py:get_federated_access_token()`.
- **Lifetime**: ~1 hour; minted fresh per tool invocation.

---

## Common errors and where to look

| Error | Layer | Fix |
|---|---|---|
| Auth0 page "Be careful! redirect_uri is not associated with this application" | Auth0 (your RWA) | Allowed Callback URLs (§1.2) — must include all 4 URLs and you must have clicked Save Changes at the bottom. |
| Google's `redirect_uri_mismatch` error page | Google Cloud Console | Authorized redirect URIs on the OAuth client (§4.1) — add `https://YOUR-TENANT.auth0.com/login/callback`. |
| GitHub "The redirect_uri MUST match the registered callback URL" | github.com/settings/developers | Authorization callback URL on the GitHub OAuth app (§5.1). |
| `Grant type 'urn:auth0:params:...' not allowed for the client` (in Auth0 logs) | Auth0 (your RWA) | Token Vault grant (§1.6). |
| `unknown or invalid refresh token` on My Account API call | Auth0 (your RWA) | Refresh-token rotation must be off (§1.4); also log out + back in to refresh the token. |
| `invalid_scope` on My Account API mint | Auth0 (your RWA) | Connected Accounts scopes must be checked (§3.2) and MRRT must be on (§3.4); log out + back in. |
| `consent_required` from Google during Token Vault | Google account | Revoke the OAuth app at https://myaccount.google.com/permissions, then reconnect. Google caches prior consent. |
| GitHub consent screen asks for terrifying permissions | Auth0 GitHub connection | Trim Permissions on the Auth0 GitHub social connection (§5.2). |
| /profile shows empty Access Token Claims | App or Auth0 API | If token starts with `eyJhbGciOiJSUzI1NiIs...` and claims are still empty, reload — JWS tokens always decode. If it starts with `eyJhbGciOiJkaXIiLCJ...` (5 segments), the token is JWE — `AUTH0_AUDIENCE` is missing or pointing at an internal Auth0 audience. Use a custom API identifier. |

---

## TL;DR — the minimum order

If you're rushing through:

1. Auth0: Create RWA → set the 4 callback URLs → disable refresh-token rotation → enable Token Vault grant → save.
2. Auth0: Create custom API (e.g., `https://ai-agent-api.example.com`) → authorize your RWA → grab the Identifier into `.env` as `AUTH0_AUDIENCE`.
3. Auth0: Activate My Account API → authorize your RWA + check the 3 connected-accounts scopes → MRRT on (include My Account API) → save.
4. Google Cloud: Create OAuth client → add `https://YOUR-TENANT.auth0.com/login/callback` → consent screen scopes for `calendar.events` + `gmail.readonly` → enable Gmail API + Calendar API → put your client_id/secret into Auth0's Google social connection → toggle the connection on for your RWA.
5. (Optional) GitHub: OAuth app → callback URL → put client_id/secret into Auth0 GitHub connection → narrow scopes → toggle on.
6. Run the app, log in, visit `/connections`, connect Google, ask the chat about your calendar.
