# Auth0 + FastAPI Agent Template

A small, end-to-end template that pairs **Auth0 login** with an **LLM-powered chat** that can call third-party APIs on the signed-in user's behalf via **Auth0 Token Vault**, plus a **Connected Accounts** page (Auth0 My Account API) so users can attach a Google identity to a non-Google primary login.

Built with FastAPI + Authlib + Jinja templates + the OpenAI SDK. No LangGraph, no React, no `auth0-ai-*` SDK — Auth0's HTTP endpoints are called directly with `httpx`. Fewer moving parts means a clearer mental model for workshop attendees.

## What you get out of the box

- **Auth0 OAuth login** (Authorization Code Flow with OIDC). Universal login screen — works with any IdP you've enabled (Google, GitHub, username/password, etc.).
- **Streaming chat UI** that talks to an OpenAI Chat Completions–compatible LLM. Tokens stream in token-by-token (`fetch` + `ReadableStream`).
- **Profile-aware system prompt** — the signed-in user's ID/access token claims are injected into the system prompt so the model can personalize replies.
- **Token Vault → Google Calendar tool** — the LLM can call `list_upcoming_calendar_events` via OpenAI function calling. The backend exchanges the user's Auth0 refresh token at Token Vault for a Google access token and calls the Calendar API.
- **Connected Accounts UI** at `/connections` — list, connect, and disconnect federated identities through the Auth0 My Account API. A user logged in via GitHub can attach Google for tool use without changing their primary login.
- **Profile inspector** at `/profile` — shows raw ID/access token claims for debugging.

## Quick start

### 1. Install

```bash
git clone https://github.com/<your-org>/auth0-fastapi-agent.git
cd auth0-fastapi-agent/app

python3.11 -m venv venv          # 3.11 or 3.12 recommended; see "Known issues" below
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Copy `.env.example` to `.env`

```bash
cp .env.example .env
```

Fill in:

- `AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET` — from your Auth0 Regular Web Application's Settings page.
- `APP_SECRET_KEY` — generate with `python -c "import secrets; print(secrets.token_hex(32))"`.
- `OPENAI_API_KEY` — your OpenAI key (or any OpenAI-compatible provider).
- *(Optional)* `OPENAI_BASE_URL` — set if using a proxy or alternative provider. Defaults to `https://api.openai.com/v1`.
- *(Optional)* `LLM_MODEL` — defaults to `gpt-4o-mini`. Any function-calling-capable chat model works.
- *(Optional)* `AUTH0_AUDIENCE` — set if you want the access token issued at login to be a JWT for a specific Auth0 API. Leave unset for an opaque token.

### 3. Auth0 dashboard configuration

This is the part that tends to trip workshop attendees up. The following must be configured in [your Auth0 dashboard](https://manage.auth0.com) **before** runtime calls will succeed:

#### Application settings (Applications → your Regular Web App → Settings)

- **Allowed Callback URLs** — comma-separated:
  ```
  http://localhost:8000/callback,
  http://127.0.0.1:8000/callback,
  http://localhost:8000/connections/callback,
  http://127.0.0.1:8000/connections/callback
  ```
- **Allowed Logout URLs**:
  ```
  http://localhost:8000/, http://127.0.0.1:8000/
  ```

#### Application settings — Token Vault

- **Advanced → Grant Types**: enable `urn:auth0:params:oauth:grant-type:token-exchange:federated-connection-access-token` (the "Token Vault" grant).
- **Settings**: disable **Allow Refresh Token Rotation**. Token Vault is incompatible with rotation.

#### My Account API (APIs → My Account API)

- **Activate** the API.
- **Application Access** tab: authorize this app, check the three Connected Accounts scopes:
  - `create:me:connected_accounts`
  - `read:me:connected_accounts`
  - `delete:me:connected_accounts`
- **Settings → Access Settings**: enable "Allow Skipping User Consent".

#### Multi-Resource Refresh Token (MRRT)

- **Application → Multi-Resource Refresh Token**: enable MRRT for the My Account API.

#### Federated connection (Authentication → Social → Google)

- Configure with your **own** Google OAuth client (don't ship Auth0's dev keys to production).
- In the Google Cloud Console for that OAuth client, add this URL to **Authorized redirect URIs**:
  ```
  https://{YOUR_AUTH0_DOMAIN}/login/callback
  ```
- Add `https://www.googleapis.com/auth/calendar.readonly` to the OAuth consent screen scopes. If the consent screen is in **Testing** mode, add your Google account as a Test user.

### 4. Run

```bash
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 → Login → walk through any IdP → land on `/chat`.

## Try the features

| Want to... | Do this |
|---|---|
| Inspect token claims | Open `/profile` |
| Use Calendar via Google primary login | `/login`, pick Google, accept Calendar at consent. Then ask: *"What's on my calendar this week?"* |
| Use Calendar via non-Google primary login | `/login`, pick GitHub (or any IdP). Then visit `/connections` → "Connect Google Account" → consent → Calendar tool now works in chat. |
| Disconnect a federated identity | `/connections` → click "Disconnect" on the row |

## Architecture, deep dive, and gotchas

See [`IMPLEMENTATION.md`](./IMPLEMENTATION.md) for:
- Complete route map and request flow
- Token Vault HTTP exchange spec (verbatim from Auth0 docs)
- The four-call My Account API Connected Accounts flow
- The URL-fragment quirk on the connect callback
- A 10-item gotcha list for common runtime errors

## Known issues

### `redirect_uri_mismatch` errors

Three different actors can throw a "redirect_uri" error and the wording is similar but the fix differs. Always copy the URL bar at the moment of the error — the host tells you which actor:

| Error host | Where to fix |
|---|---|
| Google OAuth error page | Google Cloud Console → your OAuth client → Authorized redirect URIs. Add `https://{AUTH0_DOMAIN}/login/callback` |
| github.com OAuth error | github.com/settings/developers → your OAuth app → Authorization callback URL. Set to `https://{AUTH0_DOMAIN}/login/callback` |
| Auth0 "Be careful!" page | Auth0 → Applications → your app → Allowed Callback URLs. Add the local URLs from Step 3 |

### `localhost` vs `127.0.0.1`

Whichever host you visit the app at is what gets baked into the redirect URI sent to Auth0. Both must be on Auth0's Allowed Callback URLs list, otherwise login fails depending on which URL you bookmarked.

### Refresh tokens issued before scope changes don't carry the new scopes

If you add a new scope (e.g., the connected_accounts scopes), users with a stale session will get `invalid_scope` on the first My Account API mint. **Have them log out and log back in** to get a fresh refresh token.

### Python 3.14 + corporate zero-trust agents (Prisma Access, ZScaler)

Python 3.14's stdlib socket layer can fail with `OSError: [Errno 9] Bad file descriptor` on TLS connections under intercepting agents. `pip` then can't reach pypi.

**Workaround**: use Python 3.11 or 3.12 for the venv, and install with `uv pip install` (https://github.com/astral-sh/uv) — `uv` ships its own native HTTP client.

## Project layout

```
.
├── README.md                      # this file
├── IMPLEMENTATION.md              # deep-dive reference
└── app/
    ├── .env.example
    ├── requirements.txt
    ├── main.py                    # FastAPI app, all routes
    ├── tools/
    │   ├── google_calendar.py     # Token Vault exchange + Calendar API
    │   └── auth0_my_account.py    # My Account API: connect/list/delete
    ├── static/style.css
    └── templates/
        ├── home.html
        ├── chat.html
        ├── profile.html
        ├── connections.html
        └── connections_callback.html
```

## License

MIT — feel free to fork, modify, or use as a workshop starter.

## Contributing

This is a workshop template; PRs that improve clarity or fix issues are welcome. Please don't add features that go beyond a "single-process FastAPI demo" — the value is in being readable in one sitting.
