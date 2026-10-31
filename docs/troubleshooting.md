# Troubleshooting

- **QR expired / daemon never connects** — restart `wa.py serve` and scan the
  refreshed `data/qr.png`.
- **`session was logged out`** — the phone removed the linked device. Delete
  `data/wa_session.db` and pair again.
- **`group ... not found`** — run `wa.py groups` and use the exact name or JID.
- **`cannot reach daemon`** — start `wa.py serve` first; check `data/daemon.log`.
- **`not connected after 30s`** — check the daemon log and whether the phone
  still has an internet connection.
- **Calendar shows “URL did not return iCalendar data”** — use the public or
  secret Google Calendar address ending in `/basic.ics`, not the calendar web
  page.
- **Votes stay empty** — the daemon must be connected while the group votes; a
  poll can only be decrypted while the same WhatsApp session/database is
  paired. Polls sent before the history feature cannot be recovered.
- **Login fails after a proxy move / redeploy** — clear cookies for the app
  domain and make sure `SESSION_SECRET` is set and stable.
- **Authelia rejects the login** — the client must use the same
  `token_endpoint_auth_method` (`client_secret_basic` or
  `client_secret_post`) as `OIDC_TOKEN_AUTH_METHOD`, and the redirect URI must
  match exactly.
