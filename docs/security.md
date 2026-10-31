# Security

## Built-in protections

- **OIDC** tokens are validated (signature, issuer, audience, expiry, nonce) and
  the session cookie is `HttpOnly` + `SameSite=Lax`. With an HTTPS redirect URI
  the cookie is marked `Secure` automatically.
- **Fail closed:** incomplete OIDC configuration rejects protected requests
  instead of disabling authentication.
- **API:** `/api/...` accepts only a valid bearer token and never falls back to
  the browser session.
- **SSRF protection:** calendar URLs must be `http(s)`; private, loopback,
  link-local, reserved and metadata addresses are rejected. Redirects are
  re-validated hop by hop. Use `CALENDAR_ALLOW_PRIVATE=1` only for intentional
  local feeds.
- **Open redirects:** the post-login `next` parameter only accepts local paths
  (no `//`, backslashes or control characters).
- **Headers:** CSP, `X-Frame-Options`, `X-Content-Type-Options`,
  `Referrer-Policy` and `Permissions-Policy`; HSTS is added when the session
  cookie is `Secure`.
- **Request size limit:** 2 MB (`MAX_CONTENT_LENGTH`).

## Auditing

```bash
make security    # Bandit + pip-audit
make quality     # includes the security target
```

The dependency audit covers `requirements.txt`. Keep the base image and the
bundled `pip`/`setuptools` updated; the Dockerfile upgrades them during the
build.

## Operational notes

- The web UI can send messages. Keep it bound to localhost or put it behind
  Authelia/TLS; do not expose it unprotected.
- `data/` contains the WhatsApp session and poll secrets. Restrict its
  permissions and exclude it from backups you do not trust.
- Prefer a secondary WhatsApp number for the bot.
- Cron/CLI commands need the daemon to be running; they communicate over a
  local Unix socket.
- Regular dependency updates are recommended (`pip-audit`, Dependabot/Renovate).

## Residual risks

- DNS rebinding during calendar fetches is theoretically possible because
  validation and the subsequent request resolve separately. For higher
  assurance, pin the resolved IP or use an egress proxy.
- CSRF is mitigated by `SameSite=Lax` but the app does not use per-form CSRF
  tokens. `/logout` is a `GET` request (logout CSRF, low severity).
- The base image is not pinned by digest.
