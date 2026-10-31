import os
import time
import unittest
from unittest import mock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import auth

ISSUER = "https://auth.example.com"
AUDIENCE = "whatsapp-agent"


def _key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, public_key


class _FakeSigningKey:
    def __init__(self, key):
        self.key = key


class _FakeJWKClient:
    def __init__(self, key):
        self.key = key

    def get_signing_key_from_jwt(self, _token):
        return _FakeSigningKey(self.key)


class TokenValidatorTests(unittest.TestCase):
    def setUp(self):
        self.private_key, self.public_key = _key_pair()
        self.config = auth.OIDCConfig(issuer=ISSUER, audience=AUDIENCE)
        self.validator = auth.TokenValidator(self.config)
        self.validator._jwks_client = _FakeJWKClient(self.public_key)

    def _token(self, **overrides):
        payload = {
            "sub": "user-1",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": int(time.time()) + 300,
            "iat": int(time.time()),
            "preferred_username": "anerfo",
        }
        payload.update(overrides)
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    def test_valid_token_is_accepted(self):
        claims = self.validator.validate(self._token())
        self.assertEqual(claims["sub"], "user-1")
        self.assertEqual(claims["preferred_username"], "anerfo")

    def test_expired_token_is_rejected(self):
        token = self._token(exp=int(time.time()) - 3600)
        with self.assertRaises(auth.InvalidToken):
            self.validator.validate(token)

    def test_wrong_audience_is_rejected(self):
        token = self._token(aud="some-other-client")
        with self.assertRaises(auth.InvalidToken):
            self.validator.validate(token)

    def test_wrong_issuer_is_rejected(self):
        token = self._token(iss="https://evil.example.com")
        with self.assertRaises(auth.InvalidToken):
            self.validator.validate(token)

    def test_garbage_token_is_rejected(self):
        with self.assertRaises(auth.InvalidToken):
            self.validator.validate("not-a-jwt")


class AuthGateTests(unittest.TestCase):
    def setUp(self):
        self.config = auth.OIDCConfig(issuer=ISSUER, audience=AUDIENCE)

        class Validator:
            def validate(self, token):
                if token == "good":
                    return {"sub": "user-1"}
                raise auth.InvalidToken("bad token")

        self.gate = auth.AuthGate(self.config, Validator())

    def test_missing_header_raises(self):
        with self.assertRaises(auth.MissingToken):
            self.gate.check({})

    def test_wrong_scheme_raises(self):
        with self.assertRaises(auth.MissingToken):
            self.gate.check({"Authorization": "Basic abc"})

    def test_valid_bearer_token_returns_claims(self):
        claims = self.gate.check({"Authorization": "Bearer good"})
        self.assertEqual(claims["sub"], "user-1")

    def test_invalid_bearer_token_raises(self):
        with self.assertRaises(auth.InvalidToken):
            self.gate.check({"Authorization": "Bearer bad"})

    def test_disabled_gate_allows_everything(self):
        config = auth.OIDCConfig(issuer="", audience="", disabled=True)
        gate = auth.AuthGate(config)
        self.assertEqual(gate.check({}), {})


class PKCETests(unittest.TestCase):
    def test_verifier_and_challenge(self):
        import base64
        import hashlib

        verifier, challenge = auth.new_pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        self.assertEqual(challenge, expected)


class OIDCLoginTests(unittest.TestCase):
    def setUp(self):
        self.config = auth.OIDCConfig(
            issuer=ISSUER,
            audience=AUDIENCE,
            client_id=AUDIENCE,
            scopes="openid profile email",
        )
        self.login = auth.OIDCLogin(self.config)
        self.login._discovery = {
            "authorization_endpoint": ISSUER + "/api/oidc/authorization",
            "token_endpoint": ISSUER + "/api/oidc/token",
            "end_session_endpoint": ISSUER + "/api/oidc/end-session",
        }

    def test_authorization_url_contains_required_parameters(self):
        url = self.login.authorization_url(
            state="state-1",
            nonce="nonce-1",
            code_challenge="challenge-1",
            redirect_uri="https://app.example.com/auth/callback",
        )
        self.assertTrue(url.startswith(ISSUER + "/api/oidc/authorization?"))
        self.assertIn("response_type=code", url)
        self.assertIn("client_id=whatsapp-agent", url)
        self.assertIn("state=state-1", url)
        self.assertIn("nonce=nonce-1", url)
        self.assertIn("code_challenge=challenge-1", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("scope=openid+profile+email", url)

    def test_logout_url_includes_hints(self):
        url = self.login.logout_url(
            id_token="id-token",
            post_logout_redirect_uri="https://app.example.com/login",
        )
        self.assertTrue(url.startswith(ISSUER + "/api/oidc/end-session?"))
        self.assertIn("id_token_hint=id-token", url)


class BuildAuthFromEnvTests(unittest.TestCase):
    def test_not_configured_returns_none(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(auth.build_auth_from_env())

    def test_partial_config_fails_closed(self):
        with mock.patch.dict(os.environ, {"OIDC_ISSUER": ISSUER}, clear=True):
            gate = auth.build_auth_from_env()
            self.assertIsNotNone(gate)
            self.assertTrue(gate.enabled)
            with self.assertRaises(auth.MissingToken):
                gate.check({})

    def test_configured_returns_gate(self):
        env = {"OIDC_ISSUER": ISSUER, "OIDC_CLIENT_ID": AUDIENCE}
        with mock.patch.dict(os.environ, env, clear=True):
            gate = auth.build_auth_from_env()
            self.assertIsNotNone(gate)
            self.assertTrue(gate.enabled)
            self.assertEqual(gate.config.audience, AUDIENCE)

    def test_disabled_returns_none(self):
        env = {
            "OIDC_ISSUER": ISSUER,
            "OIDC_CLIENT_ID": AUDIENCE,
            "AUTH_DISABLED": "1",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(auth.build_auth_from_env())


if __name__ == "__main__":
    unittest.main()
