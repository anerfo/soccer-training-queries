import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

import wa
from auth import AuthGate, OIDCConfig
from web import create_app

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_JWK = json.loads(
    jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key())
)
PUBLIC_JWK["kid"] = "test-key"
PUBLIC_JWK["alg"] = "RS256"
PUBLIC_JWK["use"] = "sig"

SHARED = {"issuer": "", "nonce": ""}


class _Provider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        issuer = SHARED["issuer"]
        path = urlparse(self.path).path
        if path == "/.well-known/openid-configuration":
            self._json(
                {
                    "issuer": issuer,
                    "authorization_endpoint": issuer + "/authorize",
                    "token_endpoint": issuer + "/token",
                    "jwks_uri": issuer + "/jwks.json",
                    "userinfo_endpoint": issuer + "/userinfo",
                }
            )
        elif path == "/jwks.json":
            self._json({"keys": [PUBLIC_JWK]})
        elif path == "/userinfo":
            if self.headers.get("Authorization") != "Bearer access-token":
                self.send_error(401)
                return
            self._json(
                {
                    "sub": "user-1",
                    "preferred_username": "anerfo",
                    "name": "Anerfo",
                    "email": "anerfo@gmx.net",
                    "groups": ["admins"],
                }
            )
        else:
            self.send_error(404)

    def do_POST(self):
        issuer = SHARED["issuer"]
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode())
        if path != "/token":
            self.send_error(404)
            return
        if form.get("code", [""])[0] != "test-code":
            self.send_error(400)
            return
        now = int(time.time())
        id_token = jwt.encode(
            {
                "iss": issuer,
                "aud": "whatsapp-agent",
                "sub": "user-1",
                "nonce": SHARED["nonce"],
                "iat": now,
                "exp": now + 3600,
            },
            PRIVATE_KEY,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        self._json(
            {
                "access_token": "access-token",
                "token_type": "Bearer",
                "id_token": id_token,
                "expires_in": 3600,
            }
        )


class FakeService:
    def __init__(self):
        self.connected = threading.Event()
        self.connected.set()

    def status(self):
        return {
            "connected": True,
            "account": None,
            "failure": None,
            "qr_available": False,
        }


class OIDCLoginFlowTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        port = self.server.server_address[1]
        self.issuer = f"http://127.0.0.1:{port}"
        SHARED["issuer"] = self.issuer
        SHARED["nonce"] = ""

        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.ctx = {
            "poll_history_path": base / "history.json",
            "load_poll_history": wa.load_poll_history,
            "contacts_path": base / "contacts.json",
            "load_contacts": wa.load_contacts,
        }
        config = OIDCConfig(
            issuer=self.issuer,
            audience="whatsapp-agent",
            client_id="whatsapp-agent",
            redirect_uri="http://localhost/auth/callback",
            scopes="openid profile email",
        )
        self.app = create_app(FakeService(), self.ctx, AuthGate(config))
        self.client = self.app.test_client()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def test_full_login_flow_then_api_still_needs_token(self):
        response = self.client.get("/votes")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

        response = self.client.get("/login")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].startswith(self.issuer + "/authorize")
        )
        with self.client.session_transaction() as sess:
            state = sess["_oidc_state"]
            SHARED["nonce"] = sess["_oidc_nonce"]

        response = self.client.get(
            f"/auth/callback?code=test-code&state={state}"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

        with self.client.session_transaction() as sess:
            self.assertEqual(sess["user"]["sub"], "user-1")
            self.assertEqual(sess["user"]["name"], "Anerfo")
            self.assertEqual(sess["user"]["email"], "anerfo@gmx.net")

        # The browser session now authorizes pages.
        self.assertEqual(self.client.get("/votes").status_code, 200)

        # The API is bearer-only and must not accept the session.
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/api/status",
                headers={"Authorization": "Bearer not-a-jwt"},
            ).status_code,
            401,
        )

    def test_logout_falls_back_to_provider_portal(self):
        response = self.client.get("/logout")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].startswith(
                self.issuer + "/logout?rd="
            )
        )

    def test_logout_redirects_to_configured_logout_url(self):
        config = OIDCConfig(
            issuer=self.issuer,
            audience="whatsapp-agent",
            client_id="whatsapp-agent",
            redirect_uri="http://localhost/auth/callback",
            logout_url="https://auth.example.com/logout",
        )
        client = create_app(
            FakeService(), self.ctx, AuthGate(config)
        ).test_client()
        response = client.get("/logout")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"], "https://auth.example.com/logout"
        )

    def test_callback_rejects_wrong_state(self):
        self.client.get("/login")
        response = self.client.get(
            "/auth/callback?code=test-code&state=wrong"
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("Login fehlgeschlagen", response.get_data(as_text=True))
        with self.client.session_transaction() as sess:
            self.assertNotIn("user", sess)


if __name__ == "__main__":
    unittest.main()
