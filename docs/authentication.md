# Authentication (OIDC / Authelia)

The app uses Authelia (or any OIDC provider) for login:

- **Browser pages** use the OIDC authorization-code flow with PKCE. Visiting a
  page while signed out redirects to `/login`, which sends you to Authelia.
  After login you return to `/auth/callback`, the ID token is validated, and
  the user is stored in a signed session cookie. The top bar shows the user and
  an **Abmelden** link (`/logout`, with RP-initiated logout when the provider
  offers an `end_session_endpoint`).
- **JSON API** calls (`/api/...`) are bearer-only and **never** use the session.
  Send `Authorization: Bearer <jwt>`; otherwise they return `401` with
  `WWW-Authenticate: Bearer`.
- Protected pages without a session redirect to `/login`; `/health` and
  `/static/` are exempt.

## Environment variables

Docker Compose forwards these from `.env`:

| Variable | Meaning |
| --- | --- |
| `OIDC_ISSUER` | Provider issuer URL, e.g. `https://auth.example.com` |
| `OIDC_CLIENT_ID` | OIDC client id; used as audience if `OIDC_AUDIENCE` is unset |
| `OIDC_CLIENT_SECRET` | Client secret (omit for a public PKCE-only client) |
| `OIDC_REDIRECT_URI` | Exact redirect URI registered in Authelia; defaults to `<app>/auth/callback` |
| `OIDC_AUDIENCE` | Expected `aud` claim for API bearer tokens (optional override) |
| `OIDC_SCOPES` | Requested scopes (default `openid profile email`) |
| `OIDC_TOKEN_AUTH_METHOD` | `client_secret_basic` (default) or `client_secret_post`; must match Authelia |
| `OIDC_LOGOUT_URL` | Provider logout page when there is no `end_session_endpoint` (Authelia: `https://<authelia>/logout`) |
| `OIDC_JWKS_URI` | JWKS URL; discovered from the issuer if unset |
| `OIDC_ALGORITHMS` | Accepted signing algorithms (default `RS256,ES256,PS256`) |
| `OIDC_LEEWAY` | Allowed clock skew in seconds (default `30`) |
| `SESSION_SECRET` | Secret used to sign the session cookie (set this!) |
| `SESSION_COOKIE_SECURE` | Set to `1` when serving over HTTPS |
| `AUTH_DISABLED` | Set to `1` to turn authentication off explicitly |

Register `<app-base-url>/auth/callback` as the redirect URI in Authelia.
Authentication is enforced once `OIDC_ISSUER` and `OIDC_CLIENT_ID` are set.
Without them the daemon logs a warning and serves without auth (useful for
local development); set `AUTH_DISABLED=1` to silence that on purpose.

If only part of the OIDC configuration is present, the app **fails closed**:
all protected requests are rejected instead of silently disabling auth.

## Authelia client

Generate a client secret and hash it for Authelia:

```bash
authelia crypto hash generate pbkdf2 --variant sha512 --iterations 310000 --password '<PLAINTEXT>'
```

Put the digest in Authelia and the plaintext in `.env`:

```yaml
identity_providers:
  oidc:
    clients:
      - client_id: 'soccer'
        client_name: 'Soccer Training Bot'
        client_secret: '$pbkdf2-sha512$310000$...'
        public: false
        authorization_policy: 'two_factor'
        require_pkce: true
        pkce_challenge_method: 'S256'
        redirect_uris:
          - 'https://soccer.example.com/auth/callback'
        scopes:
          - 'openid'
          - 'profile'
          - 'email'
          - 'groups'
        audience:
          - 'soccer'
        access_token_signed_response_alg: 'RS256'
        id_token_signed_response_alg: 'RS256'
        token_endpoint_auth_method: 'client_secret_post'
```

Matching `.env`:

```env
OIDC_ISSUER=https://auth.example.com
OIDC_CLIENT_ID=soccer
OIDC_CLIENT_SECRET=<PLAINTEXT>
OIDC_REDIRECT_URI=https://soccer.example.com/auth/callback
OIDC_AUDIENCE=soccer
OIDC_SCOPES="openid profile email groups"
OIDC_TOKEN_AUTH_METHOD=client_secret_post
SESSION_SECRET=<long-random-value>
SESSION_COOKIE_SECURE=1
```

The session cookie is marked `Secure` automatically when the redirect URI uses
HTTPS. Keep `SESSION_SECRET` stable so sessions survive restarts.
