# Passkey pairing

## Background

WhatsApp's **“Shortcake”** companion-linking flow (rolled out since June 2026)
requires a WebAuthn assertion for accounts that use a passkey. Headless clients
cannot sign the server challenge: the assertion must come from a real
authenticator in the account owner's browser. This is the same mechanism
described in the Whapi article
[“WhatsApp Passkey Update 2026”](https://whapi.cloud/blog/understanding-whatsapp-new-passkey-security-webauthn-explained)
(the article also advertises a commercial service; only the protocol
description is relevant here).

This repository ships a small Go worker (`tools/pair`) that uses the same
`whatsmeow` version as `neonize` 0.10.0. The web UI starts it on demand, relays
the WebAuthn challenge and assertion, and adopts the freshly linked device into
`data/wa_session.db`. The UI never asks you to handle a token or a second port.

> **Important:** a passkey must still exist on a device you can use. If it was
> deleted everywhere but is still active on WhatsApp's servers, no client can
> link the account. Test the official WhatsApp Web first and repair the passkey
> before using this flow.

## 1. Build the browser extension

The extension is public-domain code and is not audited. Review it, build it
yourself, and use a dedicated Chrome profile - not your everyday profile.

```bash
git clone https://github.com/takeflow-oficial/wa-passkey-connector
cd wa-passkey-connector
npm install
```

Edit `manifest.config.ts` and set `APP_HOSTS` to the origin of the **dashboard**
(not the pairing worker). Match patterns ignore ports, so one entry covers
`:8024` and the standalone `:8090`:

```ts
const APP_HOSTS = [
  'https://soccer.anerfo.org/*',
  'http://192.168.178.17/*',
  'http://localhost/*',
];
```

Mirror the same list in `APP_HOST_PATTERNS` in `src/background/index.ts`.
Then build and load the extension:

```bash
npm run build        # output: dist/
```

In Chrome: `chrome://extensions` -> enable **Developer mode** -> **Load
unpacked** -> select `dist/`. Keep this Chrome profile open for the next step.

## 2. Use the flow in the web UI

1. Open the dashboard. When the account is not connected, the panel
   **“Mit Passkey anmelden”** appears above the QR code.
2. The panel reports **“Erweiterung erkannt”** when the extension is loaded on
   this origin. If it does not, see [Troubleshooting](#troubleshooting).
3. Click **“Passkey-Kopplung starten”**.
4. Confirm the Windows Hello / Touch ID / security-key prompt. The extension
   opens `web.whatsapp.com` in a tab for the assertion.
5. If WhatsApp shows a code, confirm the new device on the phone. When the
   worker reports `skipHandoff`, no phone confirmation is required and the code
   is informational only.
6. The UI adopts the new session. In Docker the container restarts
   automatically; afterwards the dashboard shows **Verbunden**. Without Docker,
   restart `wa.py serve` manually.

The QR panel remains available for accounts without a passkey. If the daemon
loses its connection (for example after the old device was removed), the web UI
keeps running and shows the passkey panel, so the account can be re-linked
without restarting or deleting the container.

## 3. What happens under the hood

- `POST /pair/start` starts `wa-pair` on `127.0.0.1:<random port>` with a random
  `WA_PAIR_TOKEN` and a fresh `data/wa_session_new.db`.
- `/pair/challenge`, `/pair/response` and `/pair/status` are proxied to the
  worker. The worker binds to localhost only; the browser never sees the token.
- On success `POST /pair/finish` stops the worker and moves the new database
  over `data/wa_session.db`. The old database is kept as
  `data/wa_session.db.bak-<timestamp>`.
- In Docker, `docker-entrypoint.sh` restarts `wa.py serve` after the adoption
  (exit code 75). The new process opens the new session and connects.
- Diagnostics: `data/pair-worker.log` (contains the internal worker URL/token;
  keep it private) and `docker compose logs wa`.

The worker is pinned to the `whatsmeow` version used by `neonize` 0.10.0. If you
upgrade `neonize`, rebuild the worker too.

## 4. Docker

The main `wa` image builds `tools/pair` in a Go builder stage and copies the
`wa-pair` binary into the runtime image. No extra service is required:

```bash
WA_UID=$(id -u) WA_GID=$(id -g) docker compose up -d --build
```

`WA_AUTO_RESTART=1` is set by default in the image and compose file. It enables
the automatic restart after session adoption. Set it to `0` if you prefer to
restart the container yourself.

The optional `pair` service (`docker compose --profile pair ...`) is only a
standalone fallback for when the web UI is unavailable. The normal flow uses
the `wa` service only.

### Local development without Docker

The UI flow needs the Go binary in `PATH` or in `WA_PAIR_BIN`:

```bash
cd tools/pair
go build -o ../../bin/wa-pair .
cd ../..
WA_PAIR_BIN="$PWD/bin/wa-pair" .venv/bin/python wa.py serve
```

Requires Go 1.26+ and a C compiler (CGO is needed for SQLite). Without
`WA_AUTO_RESTART=1` the UI shows “Bitte den Dienst neu starten”; restart
`wa.py serve` after pairing.

## 5. Manual fallback (no web UI)

If the app is not running, the standalone worker can be used instead:

```bash
docker compose stop wa
docker compose --profile pair build pair
docker compose --profile pair run --rm --service-ports pair
```

Open the printed URL (`http://<nas-ip>:8090/?token=...`) in the Chrome profile
with the extension and complete the passkey steps. Add `http://<nas-ip>/*` to
`APP_HOSTS` if it is not already covered. Then stop the worker and hand over the
database:

```bash
docker compose stop wa
rm -f data/wa_session.db data/wa_session.db-wal data/wa_session.db-shm

mv data/wa_session_new.db data/wa_session.db
mv data/wa_session_new.db-wal data/wa_session.db-wal 2>/dev/null || true
mv data/wa_session_new.db-shm data/wa_session.db-shm 2>/dev/null || true

docker compose up -d wa
```

The local variant works the same way:

```bash
cd tools/pair
go run . --db ../../data/wa_session_new.db --addr 127.0.0.1:8090
```

## 6. Security notes

- The pairing API uses the app's normal session/OIDC login. The worker token is
  server-side only, and the worker listens on `127.0.0.1`.
- The WebAuthn assertion is a one-shot secret. The app relays it but does not
  store it; do not log or copy it.
- The extension is unvetted public-domain software. Build it from source, use a
  separate Chrome profile, and remove it when done.
- This automates a `navigator.credentials.get` on `web.whatsapp.com` and may
  violate WhatsApp's Terms of Service. Use it only with the account owner's
  consent; the usual ban risk of unofficial clients applies.
- `data/wa_session.db` is a credential. Keep it private (`chmod 600`), never
  commit it, and remember that `data/*` is already git-ignored.
- If the account is locked out because the passkey is gone but still registered
  server-side, this tool cannot help. Restore the passkey in WhatsApp/Google/
  Apple/Windows first.

## Troubleshooting

- **“Erweiterung nicht gefunden”** - the extension is not loaded, or its
  `APP_HOSTS` / `APP_HOST_PATTERNS` do not match the dashboard origin. Use the
  exact scheme (HTTP vs HTTPS) and hostname; match patterns ignore ports.
  Rebuild the extension, reload it in `chrome://extensions`, and reload the
  page. Check the page console for `CONNECTOR_READY`.
- **`pairing worker 'wa-pair' not found`** - the image was not rebuilt with the
  Go stage, or local development has no binary. Rebuild the image or set
  `WA_PAIR_BIN`; see [Local development](#local-development-without-docker).
- **`passkey challenge failed`** - the worker is not connected yet, or the
  account has no usable passkey. Check `data/pair-worker.log`; retry after a few
  seconds. A failed challenge is single-use, so click again for a fresh one.
- **No passkey prompt / `NotAllowedError`** - there is no usable passkey for
  `whatsapp.com` in this browser/OS profile. Test the official WhatsApp Web and
  repair the passkey first.
- **Code is shown but pairing does not finish** - approve the new device on the
  phone. Make sure the phone is online and uses the same account.
- **`PairPasskeyError` with `continuation=true`** - the assertion was accepted
  but the continuation failed. Retry with a fresh challenge (the UI restarts
  the worker on the next attempt).
- **“pairing is not complete yet”** - wait for the confirmation/connection
  before finishing; the UI does this automatically.
- **No automatic restart after pairing** - make sure the container uses the
  provided entrypoint and `WA_AUTO_RESTART=1`. Check `docker compose logs wa`.
  Without Docker, restart `wa.py serve` manually.
- **Daemon still unpaired after the manual fallback** - check that the `-wal`/
  `-shm` files were moved too, that the file is owned by `WA_UID`/`WA_GID`, and
  inspect `docker compose logs wa`.
- **Worker store errors** - use a fresh `data/wa_session_new.db`; the UI deletes
  stale files on every new attempt.
